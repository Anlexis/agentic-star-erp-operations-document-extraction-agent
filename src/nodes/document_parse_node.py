"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — DocumentParseNode
# Inner DomainWorkflowGraph node 2: convert raw document text into
# doc-type-specific raw key-value pairs.  Pure deterministic parsing
# (regex / line-splitting / keyword anchoring) — NO LLM in v1.
# langextract-style.  # production wires the real extractor here.
#
# Input state keys:
#   validated_input: str  — sanitized document text
#   doc_type:        str  — detected document type (from InputValidateNode)
#   doc_metadata:    dict
#   out_of_scope:    bool — if True, short-circuit pass-through
#
# Output state keys (partial dict — only changed keys):
#   parsed_fields:   dict — raw key-value pairs from document structure
#   status:          AgentStatus.SUCCESS or AgentStatus.ERROR
#
# Notes:
#   - parsed_fields keys are doc-type-specific at this stage; FieldExtractNode
#     normalizes them to the canonical ERP schema.
#   - Parsers are private methods (_parse_invoice, _parse_purchase_order, ...)
#     for testability.
#   - Malformed document → ERROR + error_log.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared extraction patterns (langextract-style anchors)
# ---------------------------------------------------------------------------
#
# v1 deterministic field anchors.  # production wires the real extractor here
# (e.g. a langextract pipeline with learned field schemas).

# Document identifiers per doc type.
_DOC_ID_RE = {
    "invoice": re.compile(r"\b(INV-[A-Z0-9\-]+)\b", re.IGNORECASE),
    "purchase_order": re.compile(r"\b(PO-[A-Z0-9\-]+)\b", re.IGNORECASE),
    "delivery_receipt": re.compile(r"\b(DN-[A-Z0-9\-]+)\b", re.IGNORECASE),
    "inventory_report": re.compile(r"\b(INV-STOCK-[A-Z0-9\-]+)\b", re.IGNORECASE),
}

# Date anchors: "Issue Date: 2024-01-15", "発行日: 2024/01/15", "Due 2024-02-15".
_ISSUE_DATE_RE = re.compile(
    r"(?:issue\s*date|invoice\s*date|order\s*date|delivery\s*date|as[-\s]*of|発行日|発注日|納品日|基準日)\s*[:：]?\s*"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
    re.IGNORECASE,
)
_DUE_DATE_RE = re.compile(
    r"(?:due\s*date|due|payment\s*due|支払期日|納期)\s*[:：]?\s*" r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
    re.IGNORECASE,
)
_ANY_DATE_RE = re.compile(r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b")

# Supplier / vendor code: "Supplier Code: SUP-001", "取引先コード: SUP-001".
_SUPPLIER_CODE_RE = re.compile(
    r"(?:supplier\s*code|vendor\s*code|seller\s*code|取引先コード|仕入先コード|サプライヤコード)\s*[:：]?\s*"
    r"([A-Z0-9][A-Z0-9\-]{1,})",
    re.IGNORECASE,
)
_SUPPLIER_NAME_RE = re.compile(
    r"(?:supplier|vendor|seller|取引先|仕入先)\s*(?:name)?\s*[:：]\s*(.+)",
    re.IGNORECASE,
)

# Total amount: "Total: 12,300", "合計: ¥12,300", "Total Amount 12300 JPY".
_TOTAL_RE = re.compile(
    r"(?:grand\s*total|total\s*amount|total|合計|総額|請求金額)\s*[:：]?\s*" r"[¥￥$]?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Line item: "SKU-123 | 10 | pcs | 100 | 1000" or "SKU-123  10 pcs 100 1000".
# Anchored on a SKU-like token; tolerant of whitespace / pipe separators.
_LINE_ITEM_RE = re.compile(
    r"^\s*(?P<sku>[A-Z0-9][A-Z0-9\-]{2,})"
    r"\s*[|\t,]?\s*(?P<qty>\d+(?:\.\d+)?)"
    r"\s*[|\t,]?\s*(?P<unit>[A-Za-z個本箱台枚kgKG]+)?"
    r"\s*[|\t,]?\s*(?P<price>[\d,]+(?:\.\d+)?)?"
    r"\s*[|\t,]?\s*(?P<total>[\d,]+(?:\.\d+)?)?\s*$"
)


def _search_one(pattern: "re.Pattern[str]", text: str) -> Optional[str]:
    """Return the first capture group of *pattern* in *text*, or None."""
    m = pattern.search(text)
    return m.group(1).strip() if m else None


#: Upper bound on a rendered supplier name. The anchor captures to end of line,
#: so without a cap one long line becomes an unbounded string in the delivered
#: payload.
MAX_SUPPLIER_NAME_CHARS = 128

#: Fallback line-item cap, used when no tuning reached this node.
DEFAULT_MAX_LINE_ITEMS = 1000


def _supplier_name(text: str) -> Optional[str]:
    """Capture the supplier name, bounded.

    This is the one free-text field the parser takes from the document, so it is
    the one that needs a length bound; every other captured field is matched
    against a code- or date-shaped pattern that bounds itself.
    """
    raw = _search_one(_SUPPLIER_NAME_RE, text)
    if raw is None:
        return None
    return raw[:MAX_SUPPLIER_NAME_CHARS]


def _line_item_limit(state: Dict[str, Any]) -> int:
    """Resolve the line-item cap from the tuning seeded into inner state.

    The value is validated at graph-compile time, so anything unusable here means
    no tuning reached this node at all — in which case the module default applies
    rather than an unbounded parse.
    """
    configured = (state.get("erp_runtime_config") or {}).get("max_line_items")
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    return DEFAULT_MAX_LINE_ITEMS


def _parse_line_items(text: str, limit: int) -> List[Dict[str, Any]]:
    """Extract raw line items, one per matching line, up to *limit*.

    Each returned dict carries the RAW captured strings; FieldExtractNode
    handles numeric / SKU normalization.  Lines that do not match the
    line-item anchor are skipped (header / total rows etc.).

    The document is caller-supplied and every line item renders into the
    delivered payload, so the count is bounded by configuration rather than by
    the size of the input.
    """
    items: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if len(items) >= limit:
            break
        m = _LINE_ITEM_RE.match(line)
        if not m:
            continue
        gd = m.groupdict()
        # Require at least a sku + quantity to count as a line item.
        if not gd.get("sku") or gd.get("qty") is None:
            continue
        items.append(
            {
                "sku": gd.get("sku"),
                "quantity": gd.get("qty"),
                "unit": gd.get("unit"),
                "unit_price": gd.get("price"),
                "line_total": gd.get("total"),
            }
        )
    return items


class DocumentParseNode(FunctionNode):
    """Parse raw document text into doc-type-specific key-value pairs.

    Second node in the inner DomainWorkflowGraph for RET-C2-334.
    Deterministic (regex / line-anchored) parsing — NO LLM in v1.

    Behaviour:
    - If ``out_of_scope`` is True → no-op pass-through (status=SUCCESS).
    - Dispatches to a doc-type-specific private parser.
    - Malformed / unparseable document → status=ERROR + error_log.

    Output (partial dict — only changed keys): parsed_fields, status.
    """

    # The manifest declares this agent's entry contract as VERIFIED_EXTERNAL.
    # A node that demands a HIGHER level than the agent publishes refuses the
    # agent's own declared callers at that node, and the trust order is
    # ANONYMOUS < VERIFIED_EXTERNAL < INTERNAL — so an INTERNAL node is
    # unreachable for every caller admitted at the published level.
    # tests/integration/test_manifest_identity_alignment.py holds this to the
    # manifest by reading both sides from their files.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # ── doc-type-specific parsers ──────────────────────────────────────────

    def _parse_invoice(self, text: str, limit: int) -> Dict[str, Any]:
        """Parse invoice line items, totals, vendor info, dates."""
        return {
            "doc_id": _search_one(_DOC_ID_RE["invoice"], text),
            "issue_date": _search_one(_ISSUE_DATE_RE, text) or _search_one(_ANY_DATE_RE, text),
            "due_date": _search_one(_DUE_DATE_RE, text),
            "supplier_code": _search_one(_SUPPLIER_CODE_RE, text),
            "supplier_name": _supplier_name(text),
            "total_amount": _search_one(_TOTAL_RE, text),
            "line_items": _parse_line_items(text, limit),
        }

    def _parse_purchase_order(self, text: str, limit: int) -> Dict[str, Any]:
        """Parse PO number, line items, order date, delivery date, buyer/seller."""
        return {
            "doc_id": _search_one(_DOC_ID_RE["purchase_order"], text),
            "issue_date": _search_one(_ISSUE_DATE_RE, text) or _search_one(_ANY_DATE_RE, text),
            "due_date": _search_one(_DUE_DATE_RE, text),
            "supplier_code": _search_one(_SUPPLIER_CODE_RE, text),
            "supplier_name": _supplier_name(text),
            "total_amount": _search_one(_TOTAL_RE, text),
            "line_items": _parse_line_items(text, limit),
        }

    def _parse_delivery_receipt(self, text: str, limit: int) -> Dict[str, Any]:
        """Parse DN number, items, quantity, delivery date, receiver."""
        return {
            "doc_id": _search_one(_DOC_ID_RE["delivery_receipt"], text),
            "issue_date": _search_one(_ISSUE_DATE_RE, text) or _search_one(_ANY_DATE_RE, text),
            "due_date": None,
            "supplier_code": _search_one(_SUPPLIER_CODE_RE, text),
            "supplier_name": _supplier_name(text),
            "total_amount": _search_one(_TOTAL_RE, text),
            "line_items": _parse_line_items(text, limit),
        }

    def _parse_inventory_report(self, text: str, limit: int) -> Dict[str, Any]:
        """Parse as-of date, warehouse, SKU list, quantities, unit costs."""
        return {
            "doc_id": _search_one(_DOC_ID_RE["inventory_report"], text) or _search_one(_DOC_ID_RE["invoice"], text),
            "issue_date": _search_one(_ISSUE_DATE_RE, text) or _search_one(_ANY_DATE_RE, text),
            "due_date": None,
            "supplier_code": _search_one(_SUPPLIER_CODE_RE, text),
            "supplier_name": _supplier_name(text),
            "total_amount": _search_one(_TOTAL_RE, text),
            "line_items": _parse_line_items(text, limit),
        }

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # ------------------------------------------------------------------
        # 1. Out-of-scope short-circuit (no-op pass-through).
        # ------------------------------------------------------------------
        if state.get("out_of_scope") is True:
            return {"status": AgentStatus.SUCCESS}

        text = state.get("validated_input") or ""
        doc_type = state.get("doc_type")

        if not isinstance(text, str) or not text.strip():
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or [])
                + ["DocumentParseNode: validated_input is empty — cannot parse"],
            }

        # ------------------------------------------------------------------
        # 2. Dispatch to the doc-type-specific parser.
        # ------------------------------------------------------------------
        dispatch = {
            "invoice": self._parse_invoice,
            "purchase_order": self._parse_purchase_order,
            "delivery_receipt": self._parse_delivery_receipt,
            "inventory_report": self._parse_inventory_report,
        }
        parser = dispatch.get(doc_type) if isinstance(doc_type, str) else None
        if parser is None:
            # doc_type unknown but not flagged out_of_scope — defensive ERROR.
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or [])
                + [f"DocumentParseNode: no parser registered for doc_type=" f"{doc_type!r}"],
            }

        limit = _line_item_limit(state)

        try:
            parsed_fields = parser(text, limit)
        except Exception as exc:  # noqa: BLE001 — surface as ERROR, never crash the graph
            # The full exception goes to the local log, which is an operator
            # channel. What is recorded in state is the exception's CLASS name
            # only: an exception's message can carry a fragment of the document
            # that produced it, and error_log is an audit record that travels
            # with the request. doc_type is a closed set, so it is safe to name.
            logger.exception("DocumentParseNode: parse failure for doc_type=%s", doc_type)
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or [])
                + [f"DocumentParseNode: malformed {doc_type} document " f"({type(exc).__name__})"],
            }

        # Attach the doc_type for downstream traceability.
        parsed_fields["doc_type"] = doc_type

        logger.info(
            "DocumentParseNode: parsed doc_type=%s line_items=%d",
            doc_type,
            len(parsed_fields.get("line_items", [])),
        )
        emit_trace_event(
            "document_parsed",
            {
                "doc_type": doc_type,
                "line_item_count": len(parsed_fields.get("line_items", [])),
                "has_doc_id": parsed_fields.get("doc_id") is not None,
            },
            state,
        )

        return {
            "parsed_fields": parsed_fields,
            "status": AgentStatus.SUCCESS,
        }
