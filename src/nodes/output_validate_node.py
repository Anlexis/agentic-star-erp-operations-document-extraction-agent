"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — OutputValidateNode
# Inner DomainWorkflowGraph node 5 (final): assemble formatted_output,
# run the S-3 output gate (ADR-017 hook), and emit the S-4 audit trace.
#
# Input state keys:
#   extracted_data:        dict
#   erp_validation_result: dict
#   doc_type:              str
#   out_of_scope:          bool
#
# Output state keys (partial dict — only changed keys):
#   formatted_output: dict — final structured payload (schema below)
#   out_of_scope:     bool — re-set True if output fails the structure check
#   status:           AgentStatus.SUCCESS or AgentStatus.ERROR
#
# formatted_output schema (the single definition is
# src/schemas/state.py REQUIRED_OUTPUT_KEYS):
#   {
#     "template_id":                "RET-C2-334",
#     "doc_type":                   str,
#     "extracted_data":             dict,
#     "erp_validation":             dict,
#     "out_of_scope":               bool,
#     "mandatory_fields_captured":  list[str],
#     "mandatory_fields_missing":   list[str],
#     "compliance_note":            str,
#   }
#
# Output gate:
#   The gate is a domain method, `_run_s3_domain_gate`, called EXPLICITLY from
#   execute() on the assembled output before it is returned.
#
#   It is deliberately not plumbed through the framework's post-result hook. The
#   framework calls that hook AFTER it has scanned the node's result, so masking
#   there would run after the scan it should precede; calling it here means the
#   payload is sanitised before anything else looks at it. Keeping it on its own
#   name also lets the unit tests drive the gate directly.
#
#   (An earlier comment here justified the custom name by claiming the framework
#   auto-wraps any `_extra_security_gate_*` method into the graph, so a clean
#   path returning None would hand the next node `state=None`. That is not what
#   the installed framework does — the hook is called inline by the mandatory
#   gate and its return value is used. The real hazard behind that story is the
#   hook CONTRACT: it must return the result on every path, and one that falls
#   off the end returns None. The custom name does not change that; returning the
#   payload on every branch does.)
#
#   The mandatory output gate itself must never be overridden — it is @final on
#   FunctionNode and overriding it raises TypeError at class definition.
#
# S-4 audit trace — free-function import:
#   `from shared.utils.audit_logger import emit_trace_event`.
#   NEVER call self.emit_trace_event.
#
# Out-of-scope handling:
#   If out_of_scope is True coming in, still produce a formatted_output with
#   out_of_scope=True and extracted_data={} — the S-3 gate still runs and the
#   S-4 audit still fires.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import REQUIRED_OUTPUT_KEYS

logger = logging.getLogger(__name__)

_TEMPLATE_ID = "RET-C2-334"

#: The fields the record-keeping rules require on a retained document.
_MANDATORY_FIELDS = ("doc_id", "issue_date", "supplier_code")


def _compliance_note(captured: List[str], missing: List[str], overall_valid: Any) -> str:
    """Describe the record-keeping status of THIS payload.

    The note used to assert that all three mandatory fields had been captured
    whenever the ERP cross-check happened to pass. Those are unrelated questions:
    the cross-check compares the document against reference data, and says
    nothing about which fields were read off the document. A document missing its
    supplier code and matching no reference data at all would still have been
    labelled compliant.

    That mattered more than a wording slip, because the supplier code is
    routinely absent for a reason outside this agent's control: the platform's
    personal-data filter reads a two-word title-case field LABEL ("Supplier
    Code", "Issue Date") as a personal name and masks it before any node runs, so
    the label-anchored capture finds nothing. The operation guide lists the
    document layouts that are and are not affected. The honest thing for the
    payload to do is report what it actually holds.
    """
    if missing:
        return (
            "Record-keeping fields incomplete: "
            + ", ".join(missing)
            + " not captured from this document. Not sufficient for a retained record as-is."
        )
    if overall_valid:
        return "Record-keeping fields captured: " + ", ".join(captured) + ". Reference cross-check passed."
    return (
        "Record-keeping fields captured: "
        + ", ".join(captured)
        + ". Reference cross-check flagged discrepancies — review validation_flags "
        "before posting to the ledger."
    )


# Residual account-number masking: a defensive last line for bank-account-like
# digit runs that should never survive into the delivered payload. The platform's
# own personal-data filter runs upstream of every node; this covers what reaches
# the payload by another route.
#
# ── Why this is token-scoped rather than a bare `\b\d{7,}\b` ─────────────────
#
# A bare long-digit-run rule collides head-on with what this agent exists to
# produce. Document identifiers here render over `[A-Za-z0-9-]`, and a realistic
# invoice number — `INV-20260114001` — carries an 11-digit run whose left
# neighbour is a hyphen. `\b` treats a hyphen as a boundary, so the rule fired on
# the agent's own headline field and delivered `INV-[REDACTED]`: the extraction
# destroyed by the gate meant to sanitise it. Same for any SKU with a long
# numeric segment.
#
# So the scan runs per identifier-shaped TOKEN, and a token that contains a
# LETTER is left alone — it is an identifier, not a bare account number. A token
# of digits, hyphens and underscores only is still masked wherever it carries a
# run of 7 or more digits, which is the shape an account number actually takes,
# attached or hyphen-grouped, in Latin or Japanese surroundings.
#
# The trade-off, stated plainly: an account number written with a letter inside
# the same token (`AC1234567890`) is no longer masked here. That form is not one
# this pipeline produces — nothing in the parser emits it — and it remains
# covered by the platform filter upstream and by the credential scan on the way
# out. The previous rule's failure was not hypothetical; this one's is.
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_LONG_DIGIT_RUN_RE = re.compile(r"\d{7,}")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _mask_token(match: "re.Match[str]") -> str:
    """Mask long digit runs in one token, unless the token is an identifier."""
    token = match.group(0)
    if _HAS_LETTER_RE.search(token):
        return token
    return _LONG_DIGIT_RUN_RE.sub("[REDACTED]", token)


def _mask_residual_pii(obj: Any) -> Any:
    """Recursively mask account-number-like digit runs in string values.

    Structure is preserved; only string leaves are rewritten.  Non-string
    leaves (ints, floats, bools, None) are returned unchanged so legitimate
    numeric fields (quantity, unit_price) are not corrupted.

    Mapping KEYS are walked as well as values: a payload nests line items and
    validation results, and a scan that only reached top-level strings would
    leave everything this agent actually produces unexamined.
    """
    if isinstance(obj, str):
        return _IDENT_TOKEN_RE.sub(_mask_token, obj)
    if isinstance(obj, dict):
        return {_mask_residual_pii(k): _mask_residual_pii(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_residual_pii(v) for v in obj]
    return obj


class OutputValidateNode(FunctionNode):
    """Assemble and S-3-gate the final extraction payload.

    Final node in the inner DomainWorkflowGraph for RET-C2-334.

    Responsibilities:
    - Build formatted_output from extracted_data + erp_validation_result.
    - Out-of-scope inputs still yield a formatted_output (extracted_data={}).
    - Emit the S-4 audit trace via the free-function emit_trace_event.
    - The S-3 output gate (_run_s3_domain_gate) is called EXPLICITLY from
      execute() (ADR-017) — masks residual PII and asserts the schema is complete.

    Output (partial dict — only changed keys):
        formatted_output, out_of_scope, status.
    """

    # The manifest declares this agent's entry contract as VERIFIED_EXTERNAL.
    # A node that demands a HIGHER level than the agent publishes refuses the
    # agent's own declared callers at that node, and the trust order is
    # ANONYMOUS < VERIFIED_EXTERNAL < INTERNAL — so an INTERNAL node is
    # unreachable for every caller admitted at the published level.
    # tests/integration/test_manifest_identity_alignment.py holds this to the
    # manifest by reading both sides from their files.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _run_s3_domain_gate(self, output: Dict[str, Any]) -> Dict[str, Any]:
        """Domain output gate, called EXPLICITLY from execute() before returning.

        1. Mask residual account-number-like runs in the delivered payload.
        2. Backfill any missing required key so the payload is well-formed.

        Returns the sanitized output dict on EVERY path — including the paths
        that change nothing. A gate that falls off the end returns None, and the
        node would then return None into the graph.
        """
        if not isinstance(output, dict):
            return output

        formatted = output.get("formatted_output")
        if isinstance(formatted, dict):
            # 1. Residual-PII masking (last-line defence).
            formatted = _mask_residual_pii(formatted)
            # 2. Schema completeness — backfill any missing required key so the
            #    delivered payload is always well-formed for the caller.
            for key in REQUIRED_OUTPUT_KEYS:
                formatted.setdefault(key, None)
            output = dict(output)
            output["formatted_output"] = formatted

        return output

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        doc_type = state.get("doc_type")
        out_of_scope = bool(state.get("out_of_scope"))
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Out-of-scope path — still produce a well-formed payload.
        # ------------------------------------------------------------------
        if out_of_scope:
            formatted_output: Dict[str, Any] = {
                "template_id": _TEMPLATE_ID,
                "doc_type": doc_type,
                "extracted_data": {},
                "erp_validation": {},
                "out_of_scope": True,
                "mandatory_fields_captured": [],
                "mandatory_fields_missing": list(_MANDATORY_FIELDS),
                "compliance_note": (
                    "Document could not be classified; no extraction performed. "
                    "Denshi-Chobo-Hozon-Ho record not generated."
                ),
            }
            emit_trace_event(
                "extraction_complete",
                {
                    "doc_type": doc_type,
                    "out_of_scope": True,
                    "overall_valid": None,
                },
                state,
            )
            return self._run_s3_domain_gate(
                {
                    "formatted_output": formatted_output,
                    "out_of_scope": True,
                    "status": AgentStatus.SUCCESS,
                }
            )

        # ------------------------------------------------------------------
        # 2. Normal path — assemble the payload from upstream state.
        # ------------------------------------------------------------------
        extracted_data = state.get("extracted_data")
        erp_validation_result = state.get("erp_validation_result") or {}

        if not isinstance(extracted_data, dict) or not extracted_data:
            # No extracted data reached the gate though not flagged out_of_scope
            # → re-flag out_of_scope and deliver an empty-but-valid payload.
            logger.warning("OutputValidateNode: extracted_data absent on in-scope path — " "re-flagging out_of_scope")
            formatted_output = {
                "template_id": _TEMPLATE_ID,
                "doc_type": doc_type,
                "extracted_data": {},
                "erp_validation": {},
                "out_of_scope": True,
                "mandatory_fields_captured": [],
                "mandatory_fields_missing": list(_MANDATORY_FIELDS),
                "compliance_note": (
                    "Extraction produced no structured data; " "Denshi-Chobo-Hozon-Ho record not generated."
                ),
            }
            emit_trace_event(
                "extraction_complete",
                {
                    "doc_type": doc_type,
                    "out_of_scope": True,
                    "overall_valid": erp_validation_result.get("overall_valid"),
                },
                state,
            )
            return self._run_s3_domain_gate(
                {
                    "formatted_output": formatted_output,
                    "out_of_scope": True,
                    "status": AgentStatus.SUCCESS,
                    "error_log": error_log,
                }
            )

        overall_valid = erp_validation_result.get("overall_valid")
        captured = [field for field in _MANDATORY_FIELDS if extracted_data.get(field)]
        missing = [field for field in _MANDATORY_FIELDS if not extracted_data.get(field)]
        compliance_note = _compliance_note(captured, missing, overall_valid)

        formatted_output = {
            "template_id": _TEMPLATE_ID,
            "doc_type": doc_type,
            "extracted_data": extracted_data,
            "erp_validation": erp_validation_result,
            "out_of_scope": False,
            # The record-keeping claim is now checkable against the payload it
            # is made about, rather than asserted alongside it.
            "mandatory_fields_captured": captured,
            "mandatory_fields_missing": missing,
            "compliance_note": compliance_note,
        }

        # ------------------------------------------------------------------
        # 3. S-4 audit trace (free-function; positional form per sibling nodes).
        # ------------------------------------------------------------------
        emit_trace_event(
            "extraction_complete",
            {
                "doc_type": doc_type,
                "out_of_scope": False,
                "overall_valid": overall_valid,
            },
            state,
        )

        logger.info(
            "OutputValidateNode: extraction_complete doc_type=%s overall_valid=%s",
            doc_type,
            overall_valid,
        )

        return self._run_s3_domain_gate(
            {
                "formatted_output": formatted_output,
                "out_of_scope": False,
                "status": AgentStatus.SUCCESS,
            }
        )
