"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — FieldExtractNode
# Inner DomainWorkflowGraph node 3: map doc-type-specific raw parsed fields
# to the canonical ERP-compatible schema.  Deterministic normalization
# (date → ISO 8601, amount → float, SKU → upper/trimmed) — NO LLM in v1.
#
# Input state keys:
#   parsed_fields: dict — raw key-value pairs from DocumentParseNode
#   doc_type:      str
#   out_of_scope:  bool — if True, short-circuit pass-through
#
# Output state keys (partial dict — only changed keys):
#   extracted_data: dict — normalized canonical fields (schema below)
#   status:         AgentStatus.SUCCESS or AgentStatus.ERROR
#
# Canonical extracted_data schema:
#   {
#     "doc_type", "doc_id", "issue_date", "due_date",
#     "supplier_code", "supplier_name",
#     "line_items": [{"sku","quantity","unit","unit_price","line_total"}],
#     "total_amount", "currency", "raw_fields",
#   }
#
# Denshi-Chobo-Hozon-Ho mandatory fields: doc_id + issue_date + supplier_code.
#   Missing doc_id / issue_date / >=1 line_item → ERROR.
#   raw_fields preserved for audit traceability.

import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

_DEFAULT_CURRENCY = "JPY"

_DATE_SPLIT_RE = re.compile(r"[-/.]")

#: The token the platform's personal-data filter substitutes for a redacted span.
#: The document text this node works from has already passed through that filter
#: at every node boundary, so the sentinel arrives here as an ordinary string.
_REDACTION_SENTINEL = "[MASKED]"

#: The extracted string fields that could contain a redacted span. Numeric and
#: code-shaped fields cannot: the filter rewrites personal-data shapes, and a
#: rewritten span would stop matching the code or date pattern that captured it.
_REDACTABLE_FIELDS = ("supplier_name",)


def _redacted_fields(extracted: Dict[str, Any]) -> List[str]:
    """Return the extracted fields whose value is a redaction, not a value.

    A masked value is not an extracted value. The platform's filter runs BEFORE
    this node sees the document, so a supplier name it rewrote arrives as an
    ordinary string containing the sentinel — and reporting it as a captured
    field would certify a redaction as data. The field is nulled and named here
    instead, so a reader can tell "this document did not say" from "we were not
    allowed to read it".
    """
    return [
        field
        for field in _REDACTABLE_FIELDS
        if isinstance(extracted.get(field), str) and _REDACTION_SENTINEL in extracted[field]
    ]


def _to_iso_date(raw: Any) -> Optional[str]:
    """Normalize a raw date string to ISO 8601 (YYYY-MM-DD).

    Accepts YYYY-MM-DD, YYYY/MM/DD, YYYY.M.D with 1- or 2-digit month/day.
    Returns None if the value cannot be parsed.
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = _DATE_SPLIT_RE.split(raw.strip())
    if len(parts) != 3:
        return None
    try:
        year, month, day = (int(p) for p in parts)
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _to_float(raw: Any) -> Optional[float]:
    """Normalize a raw amount to a FINITE float, or None.

    Finiteness is not a formality here. `float()` happily returns `inf` for a
    long-enough digit run — a document carrying a 400-digit "amount" parses to
    infinity rather than raising — and it accepts the literals "nan" and "inf"
    outright. Every comparison against NaN is False, so a NaN reaching the
    reconciliation check downstream would not raise; it would quietly decide the
    question the check exists to answer. An infinity is no better: it survives
    into the delivered payload, where `json.dumps` writes it as the bare token
    `Infinity`, which is not valid JSON for a strict client.

    Returning None puts the value on the same path as "absent", which the
    mandatory-field check downstream already treats as a failure.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        cleaned = re.sub(r"[,\s¥￥$]", "", raw.strip())
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _normalize_sku(raw: Any) -> Optional[str]:
    """SKU normalization: strip whitespace, uppercase."""
    if raw is None or not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper()
    return cleaned or None


def _normalize_line_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a raw parsed line item to the canonical line-item schema.

    Returns None when the item has no usable SKU.
    """
    sku = _normalize_sku(raw.get("sku"))
    if not sku:
        return None
    quantity = _to_float(raw.get("quantity"))
    return {
        "sku": sku,
        "quantity": quantity if quantity is not None else 0.0,
        "unit": (raw.get("unit") or "unit"),
        "unit_price": _to_float(raw.get("unit_price")),
        "line_total": _to_float(raw.get("line_total")),
    }


class FieldExtractNode(FunctionNode):
    """Normalize raw parsed fields into the canonical ERP schema.

    Third node in the inner DomainWorkflowGraph for RET-C2-334.
    Deterministic normalization — NO LLM in v1.

    Behaviour:
    - If ``out_of_scope`` is True → no-op pass-through (status=SUCCESS).
    - Maps parsed_fields → canonical extracted_data.
    - Normalizes dates to ISO 8601, amounts to float, SKUs to upper/trimmed.
    - Missing mandatory fields (doc_id, issue_date, >=1 line_item) → ERROR.

    Output (partial dict — only changed keys): extracted_data, status.
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

        parsed_fields = state.get("parsed_fields")
        doc_type = state.get("doc_type")

        if not isinstance(parsed_fields, dict):
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or [])
                + ["FieldExtractNode: parsed_fields is missing or not a dict; " "DocumentParseNode must run first"],
            }

        # ------------------------------------------------------------------
        # 2. Normalize line items.
        # ------------------------------------------------------------------
        raw_items = parsed_fields.get("line_items") or []
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        line_items: List[Dict[str, Any]] = [
            item for item in (_normalize_line_item(ri) for ri in raw_items if isinstance(ri, dict)) if item is not None
        ]

        # ------------------------------------------------------------------
        # 3. Build the canonical extracted_data dict.
        # ------------------------------------------------------------------
        doc_id = parsed_fields.get("doc_id")
        issue_date = _to_iso_date(parsed_fields.get("issue_date"))
        supplier_name = parsed_fields.get("supplier_name")

        extracted_data: Dict[str, Any] = {
            "doc_type": doc_type or parsed_fields.get("doc_type"),
            "doc_id": doc_id.strip() if isinstance(doc_id, str) else doc_id,
            "issue_date": issue_date,
            "due_date": _to_iso_date(parsed_fields.get("due_date")),
            "supplier_code": _normalize_sku(parsed_fields.get("supplier_code")),
            "supplier_name": supplier_name.strip() if isinstance(supplier_name, str) else supplier_name,
            "line_items": line_items,
            "total_amount": _to_float(parsed_fields.get("total_amount")),
            "currency": parsed_fields.get("currency") or _DEFAULT_CURRENCY,
            "raw_fields": parsed_fields,
        }

        # ------------------------------------------------------------------
        # 3b. Redaction check — a masked span is not an extracted value.
        # ------------------------------------------------------------------
        redacted = _redacted_fields(extracted_data)
        for field in redacted:
            extracted_data[field] = None
        extracted_data["redacted_fields"] = redacted

        # ------------------------------------------------------------------
        # 4. Mandatory-field validation (Denshi-Chobo-Hozon-Ho compliance).
        # ------------------------------------------------------------------
        missing: List[str] = []
        if not extracted_data["doc_id"]:
            missing.append("doc_id")
        if not extracted_data["issue_date"]:
            missing.append("issue_date")
        if not extracted_data["line_items"]:
            missing.append("line_items")

        if missing:
            reason = "FieldExtractNode: mandatory field(s) missing for compliance — " + ", ".join(missing)
            logger.error(reason)
            return {
                "status": AgentStatus.ERROR,
                "error_log": (state.get("error_log") or []) + [reason],
            }

        logger.info(
            "FieldExtractNode: normalized doc_type=%s doc_id=%s line_items=%d currency=%s",
            extracted_data["doc_type"],
            extracted_data["doc_id"],
            len(line_items),
            extracted_data["currency"],
        )
        emit_trace_event(
            "fields_extracted",
            {
                "doc_type": extracted_data["doc_type"],
                "has_supplier_code": extracted_data["supplier_code"] is not None,
                "line_item_count": len(line_items),
                "has_total": extracted_data["total_amount"] is not None,
            },
            state,
        )

        return {
            "extracted_data": extracted_data,
            "status": AgentStatus.SUCCESS,
        }
