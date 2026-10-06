"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Read input_context via state.get("input_context", {}) — read-only [C1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — ERPValidateNode
# Inner DomainWorkflowGraph node 4: cross-validate extracted fields against
# caller-supplied ERP reference data.  Deterministic — no live ERP API in v1.
#
# The caller sends the reference data as
# input_context["erp_reference"]  ({sku_master, supplier_master,
# total_tolerance}).  It does NOT arrive here on that channel: this node runs in
# the INNER graph, and the framework invokes a nested graph with the request
# string only, forwarding no structured parameters.  It arrives as the validated
# caller contract, seeded into inner state by DomainWorkflowGraph's
# initial-state hook from the context bridge (src/graph/context_bridge.py).
#
# Input state keys:
#   extracted_data:     dict — canonical fields from FieldExtractNode
#   doc_type:           str
#   out_of_scope:       bool — if True, short-circuit pass-through
#   caller_contract:    dict — validated reference codes + tolerance
#   erp_runtime_config: dict — tuning forwarded from config/config.yaml
#
# Output state keys (partial dict — only changed keys):
#   erp_validation_result: dict — validation outcome (schema below)
#   status:                AgentStatus.SUCCESS or AgentStatus.ERROR
#
# erp_validation_result schema:
#   {
#     "sku_matches":      [{"sku","matched","erp_sku"}],
#     "supplier_match":   bool | None,
#     "date_in_range":    bool | None,
#     "total_check":      bool | None,
#     "validation_flags": [str],
#     "overall_valid":    bool,
#   }
#
# Rules:
#   - Validation mismatches are NOT errors — they populate validation_flags
#     and set overall_valid=False.  Only system/infra failures → ERROR.
#   - When reference data is absent, the corresponding check is SKIPPED
#     (value set to None), not failed.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Last-resort tolerance for the line-total vs. total_amount reconciliation,
# used only when neither the caller nor config/config.yaml supplies one.
_DEFAULT_TOLERANCE = 0.01

# Upper bound on rendered validation flags. One flag per unmatched line item
# means a document's line count decides the size of the delivered payload; the
# cap keeps that bounded and the count is reported alongside so nothing is
# silently dropped.
MAX_RENDERED_FLAGS = 50

# Codes rendered into a validation flag are restricted to the alphabet the
# parser can actually produce for them. A code that does not match is reported
# by position instead, so no free-form document text reaches a rendered string.
_INERT_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")


def _safe_code(code: str, index: int) -> str:
    """Render a document-derived code, or its position when it is not inert."""
    return code if _INERT_CODE_RE.match(code) else f"line item #{index}"


def _tolerance(state: Dict[str, Any], contract: Dict[str, Any]) -> float:
    """Resolve the reconciliation tolerance: caller → config → default.

    The caller's value has already been parsed as a finite, bounded number by
    src/services/caller_contract.py; the config value has already been validated
    at graph-compile time. Neither can be NaN here, which is what makes the
    comparison it feeds trustworthy.
    """
    caller = contract.get("total_tolerance")
    if isinstance(caller, (int, float)) and not isinstance(caller, bool):
        return float(caller)
    configured = (state.get("erp_runtime_config") or {}).get("total_tolerance")
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        return float(configured)
    return _DEFAULT_TOLERANCE


def _normalize_ref_codes(raw: Any) -> Optional[Set[str]]:
    """Normalize a reference-code list to an upper-cased set, or None if absent.

    An EMPTY list is a caller saying "I have no codes for this", which is not the
    same as omitting the field, and it must not silently turn the check off — an
    empty master matches nothing, so every code is reported unmatched. Only an
    absent field skips the check.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return None
    return {str(code).strip().upper() for code in raw if str(code).strip()}


class ERPValidateNode(FunctionNode):
    """Cross-validate extracted fields against caller-injected ERP reference data.

    Fourth node in the inner DomainWorkflowGraph for RET-C2-334.
    Deterministic — no live ERP API in v1; the caller supplies the reference
    data per request, which keeps the agent stateless and testable.

    Behaviour:
    - If ``out_of_scope`` is True → no-op pass-through (status=SUCCESS).
    - SKU validation: each line_item.sku vs the contract's sku_master.
    - Supplier validation: supplier_code vs the contract's supplier_master.
    - Total check: sum(line_total) vs total_amount, within the resolved
      tolerance (caller → config/config.yaml → default).
    - Mismatches populate validation_flags; overall_valid=False (NOT an error).
    - An ABSENT reference field skips that check (None). An EMPTY list does not:
      it is a caller stating there are no valid codes, so everything is
      unmatched.

    Output (partial dict — only changed keys): erp_validation_result, status.
    """

    # The manifest declares this agent's entry contract as VERIFIED_EXTERNAL.
    # A node that demands a HIGHER level than the agent publishes refuses the
    # agent's own declared callers at that node, and the trust order is
    # ANONYMOUS < VERIFIED_EXTERNAL < INTERNAL — so an INTERNAL node is
    # unreachable for every caller admitted at the published level.
    # tests/integration/test_manifest_identity_alignment.py holds this to the
    # manifest by reading both sides from their files.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # ------------------------------------------------------------------
        # 1. Out-of-scope short-circuit (no-op pass-through).
        # ------------------------------------------------------------------
        if state.get("out_of_scope") is True:
            return {"status": AgentStatus.SUCCESS}

        extracted_data = state.get("extracted_data")
        if not isinstance(extracted_data, dict):
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or [])
                + ["ERPValidateNode: extracted_data is missing or not a dict; " "FieldExtractNode must run first"],
            }

        # The reference data arrives as the VALIDATED caller contract, seeded by
        # the inner graph's initial-state hook from the context bridge. It cannot
        # be read from state["input_context"] here: the framework invokes a
        # nested graph with the request STRING only and forwards no structured
        # parameters, so input_context inside this graph is always empty and both
        # cross-checks below would be skipped on every request.
        contract = state.get("caller_contract") or {}
        sku_master = _normalize_ref_codes(contract.get("sku_master"))
        supplier_master = _normalize_ref_codes(contract.get("supplier_master"))
        tolerance = _tolerance(state, contract)

        validation_flags: List[str] = []
        line_items = extracted_data.get("line_items") or []

        # ------------------------------------------------------------------
        # 2. SKU validation (skipped entirely when sku_master is absent).
        # ------------------------------------------------------------------
        sku_matches: List[Dict[str, Any]] = []
        unmatched_skus = 0
        if sku_master is not None:
            for index, item in enumerate(line_items, start=1):
                sku = str(item.get("sku", "")).strip().upper()
                matched = sku in sku_master
                sku_matches.append(
                    {
                        "sku": sku,
                        "matched": matched,
                        "erp_sku": sku if matched else None,
                    }
                )
                if not matched:
                    unmatched_skus += 1
                    if len(validation_flags) < MAX_RENDERED_FLAGS:
                        validation_flags.append(f"SKU {_safe_code(sku, index)} not found in ERP SKU master")
        else:
            # Reference not provided — record the SKUs as unverified (matched=None).
            for item in line_items:
                sku = str(item.get("sku", "")).strip().upper()
                sku_matches.append({"sku": sku, "matched": None, "erp_sku": None})

        # ------------------------------------------------------------------
        # 3. Supplier validation (skipped when supplier_master is absent).
        # ------------------------------------------------------------------
        supplier_code = extracted_data.get("supplier_code")
        if supplier_master is not None:
            supplier_match: Optional[bool] = bool(
                supplier_code and str(supplier_code).strip().upper() in supplier_master
            )
            if not supplier_match:
                rendered = _safe_code(str(supplier_code or "").strip().upper(), 0)
                validation_flags.append(
                    "Supplier code not present on the document"
                    if not supplier_code
                    else f"Supplier code {rendered} not found in ERP supplier master"
                )
        else:
            supplier_match = None

        # ------------------------------------------------------------------
        # 4. Total reconciliation check (sum of line_total vs total_amount).
        # ------------------------------------------------------------------
        total_amount = extracted_data.get("total_amount")
        line_totals = [item.get("line_total") for item in line_items if item.get("line_total") is not None]
        # Every value here has already been through the finite parser in
        # FieldExtractNode, so neither `summed` nor `total_amount` can be NaN or
        # an infinity. That matters: a NaN compares False against every bound, so
        # an unchecked NaN would make this reconciliation report a mismatch it
        # cannot explain — or, with the comparison written the other way round,
        # silently report a match. The finite check belongs upstream, at the
        # point the string became a number; this is where it pays off.
        if total_amount is not None and line_totals:
            summed = sum(line_totals)
            total_check: Optional[bool] = abs(summed - total_amount) <= tolerance
            if not total_check:
                # Amounts are document-derived numbers, not caller strings, and
                # they are the whole content of this finding — a flag that
                # withheld them would not be actionable.
                validation_flags.append(
                    f"Line-item total {summed} does not match document total " f"{total_amount} (tolerance {tolerance})"
                )
        else:
            total_check = None

        # ------------------------------------------------------------------
        # 5. Date-in-range check (informational; None in v1 — no range source).
        # ------------------------------------------------------------------
        date_in_range: Optional[bool] = None

        # ------------------------------------------------------------------
        # 6. Overall validity: True only when no check actively failed.
        #    Skipped checks (None) do not count against validity.
        # ------------------------------------------------------------------
        overall_valid = len(validation_flags) == 0

        erp_validation_result: Dict[str, Any] = {
            "sku_matches": sku_matches,
            "supplier_match": supplier_match,
            "date_in_range": date_in_range,
            "total_check": total_check,
            "validation_flags": validation_flags,
            # The rendered flag list is capped; this count is not, so a reader
            # can tell a document with 3 unmatched SKUs from one with 300 even
            # though both render at most MAX_RENDERED_FLAGS lines.
            "unmatched_sku_count": unmatched_skus,
            "tolerance_applied": tolerance,
            "overall_valid": overall_valid,
        }

        logger.info(
            "ERPValidateNode: overall_valid=%s flags=%d sku_checked=%s supplier_checked=%s",
            overall_valid,
            len(validation_flags),
            sku_master is not None,
            supplier_master is not None,
        )
        emit_trace_event(
            "erp_validated",
            {
                "doc_type": state.get("doc_type"),
                "overall_valid": overall_valid,
                "flag_count": len(validation_flags),
                "sku_reference_present": sku_master is not None,
                "supplier_reference_present": supplier_master is not None,
            },
            state,
        )

        return {
            "erp_validation_result": erp_validation_result,
            "status": AgentStatus.SUCCESS,
        }
