"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — InputValidateNode
# Inner DomainWorkflowGraph node 1: document intake validation.
# Detects the document type from header signatures, builds lightweight
# intake metadata, and flags out-of-scope (unknown / unparseable) inputs.
# Emits an S-4 audit trace.
#
# Input state keys:
#   user_input:      str — raw document content or file path (already
#                          S-1-sanitized upstream by the outer PreProcessNode)
#   validated_input: str — sanitized document text (preferred if present)
#
# Output state keys (partial dict — only changed keys):
#   doc_type:        str  — invoice | purchase_order | delivery_receipt | inventory_report
#   doc_metadata:    dict — {"source": str, "char_len": int}
#   validated_input: str  — cleaned/trimmed document text
#   out_of_scope:    bool — True if doc type cannot be determined
#   status:          AgentStatus.SUCCESS or AgentStatus.ERROR
#   error_log:       list[str] — appended on system/infra failure only
#
# ADR — out-of-scope rule:
#   Unknown / unparseable doc type = out_of_scope=True + status=SUCCESS.
#   ERROR is reserved for system/infrastructure failures (empty input),
#   NOT for unrecognised document types.
#
# S-1 note: account/transaction-field masking for Denshi-Chobo-Hozon-Ho
#   compliance is handled upstream in PreProcessNode (outer backbone).
#   InputValidateNode receives already-sanitized input — do NOT re-sanitize.
# S-4: emit_trace_event used as the free-function import (never self.).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Doc-type detection signatures
# ---------------------------------------------------------------------------
#
# Each entry maps a canonical doc_type to the case-insensitive header /
# keyword patterns that identify it.  Order matters: the more-specific
# inventory pattern (INV-STOCK) is checked before the generic invoice
# pattern (INV-) so an inventory report is never mis-detected as an invoice.

_DOC_TYPE_PATTERNS: List[Tuple[str, List["re.Pattern[str]"]]] = [
    (
        "inventory_report",
        [
            re.compile(r"在庫", re.IGNORECASE),
            re.compile(r"\binventory\s+report\b", re.IGNORECASE),
            re.compile(r"\bINV-STOCK\b", re.IGNORECASE),
        ],
    ),
    (
        "invoice",
        [
            re.compile(r"請求書"),
            re.compile(r"\binvoice\b", re.IGNORECASE),
            re.compile(r"\bINV-\d", re.IGNORECASE),
        ],
    ),
    (
        "purchase_order",
        [
            re.compile(r"発注書"),
            re.compile(r"\bpurchase\s+order\b", re.IGNORECASE),
            re.compile(r"\bPO-\d", re.IGNORECASE),
        ],
    ),
    (
        "delivery_receipt",
        [
            re.compile(r"納品書"),
            re.compile(r"\bdelivery\s+receipt\b", re.IGNORECASE),
            re.compile(r"\bDN-\d", re.IGNORECASE),
        ],
    ),
]


def _detect_doc_type(text: str) -> Optional[str]:
    """Return the canonical doc_type for *text*, or None if undetermined.

    Inventory is matched first so that the INV-STOCK signature takes
    precedence over the generic invoice INV- signature.
    """
    for doc_type, patterns in _DOC_TYPE_PATTERNS:
        for pattern in patterns:
            if pattern.search(text):
                return str(doc_type)
    return None


def _source_hint(text: str) -> str:
    """Derive a coarse source hint for audit metadata.

    Returns "path" when the input looks like a filesystem path / URL,
    otherwise "text".  Never echoes raw document content.
    """
    head = text.strip()[:256]
    if head.startswith(("/", "./", "../", "file://", "http://", "https://")):
        return "path"
    if re.match(r"^[A-Za-z]:[\\/]", head):  # windows drive path
        return "path"
    return "text"


class InputValidateNode(FunctionNode):
    """Validate the incoming document and detect its type.

    First node in the inner DomainWorkflowGraph for RET-C2-334.

    Validation rules:
    - ``user_input`` / ``validated_input`` must be a non-empty string.
    - The document type is detected from header / keyword signatures.
    - If no doc_type matches → out_of_scope=True, status=SUCCESS (ADR).
    - Empty input → status=ERROR (system/infra failure).

    Output (partial dict — only changed keys):
        doc_type, doc_metadata, validated_input, out_of_scope, status, error_log.
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
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Resolve and non-empty-check the raw document text.
        # ------------------------------------------------------------------
        raw_text = state.get("validated_input")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raw_text = state.get("user_input", "")

        if not isinstance(raw_text, str) or not raw_text.strip():
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log
                + ["InputValidateNode: document input is empty or missing — " "nothing to validate"],
            }

        cleaned = raw_text.strip()

        # ------------------------------------------------------------------
        # 2. Detect the document type from header / keyword signatures.
        # ------------------------------------------------------------------
        doc_type = _detect_doc_type(cleaned)

        # ------------------------------------------------------------------
        # 3. Build intake metadata (no raw content echoed).
        # ------------------------------------------------------------------
        doc_metadata: Dict[str, Any] = {
            "source": _source_hint(cleaned),
            "char_len": len(cleaned),
        }

        # ------------------------------------------------------------------
        # 4. Out-of-scope handling (ADR: SUCCESS, not ERROR).
        # ------------------------------------------------------------------
        if doc_type is None:
            logger.info(
                "InputValidateNode: doc type undetermined — out_of_scope " "(char_len=%d)",
                doc_metadata["char_len"],
            )
            emit_trace_event(
                "input_out_of_scope",
                {
                    "doc_type": None,
                    "char_len": doc_metadata["char_len"],
                    "source": doc_metadata["source"],
                },
                state,
            )
            return {
                "doc_type": None,
                "doc_metadata": doc_metadata,
                "validated_input": cleaned,
                "out_of_scope": True,
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        # ------------------------------------------------------------------
        # 5. In-scope: emit the S-4 audit trace and return the partial delta.
        # ------------------------------------------------------------------
        logger.info(
            "InputValidateNode: detected doc_type=%s char_len=%d source=%s",
            doc_type,
            doc_metadata["char_len"],
            doc_metadata["source"],
        )
        emit_trace_event(
            "input_validated",
            {
                "doc_type": doc_type,
                "char_len": doc_metadata["char_len"],
                "source": doc_metadata["source"],
            },
            state,
        )

        return {
            "doc_type": doc_type,
            "doc_metadata": doc_metadata,
            "validated_input": cleaned,
            "out_of_scope": False,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
