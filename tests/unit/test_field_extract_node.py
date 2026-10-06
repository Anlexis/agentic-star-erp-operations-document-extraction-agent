# RET-C2-334 — Unit Tests: FieldExtractNode
#
# Inner DomainWorkflowGraph node 3: map doc-type-specific raw parsed fields to
# the canonical ERP-compatible schema. Deterministic normalization
# (date -> ISO 8601, amount -> float, SKU -> upper/trimmed) — NO LLM in v1.
#
# Asserted against the MERGED develop implementation (c7d9d1e0):
#   - A complete invoice parsed_fields -> extracted_data with normalized doc_id,
#     issue_date (ISO 8601), line_items, total_amount (float), currency=JPY.
#   - SKU normalization: "  sku-abc  " -> "SKU-ABC".
#   - Missing mandatory field (doc_id absent) -> status=ERROR.
#   - out_of_scope=True -> no-op pass-through (SUCCESS).
#
# Mandatory Denshi-Chobo-Hozon-Ho fields: doc_id + issue_date + >=1 line_item.
#
# S-4 audit: emit_trace_event muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import pytest

from src.nodes.field_extract_node import FieldExtractNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.field_extract_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _parsed(**overrides):
    """A complete invoice parsed_fields dict (raw strings, pre-normalization)."""
    base = {
        "doc_id": "INV-2024-0001",
        "issue_date": "2024/1/15",
        "due_date": "2024-02-15",
        "supplier_code": "SUP-001",
        "supplier_name": "Acme Supplies",
        "total_amount": "8,000",
        "line_items": [
            {
                "sku": "  sku-abc  ",
                "quantity": "10",
                "unit": "pcs",
                "unit_price": "500",
                "line_total": "5,000",
            }
        ],
        "doc_type": "invoice",
    }
    base.update(overrides)
    return base


class TestFieldExtractNode:
    """Unit tests for FieldExtractNode (normalization + mandatory-field gate)."""

    def setup_method(self):
        self.node = FieldExtractNode()

    def test_happy_path_normalizes_to_canonical_schema(self):
        """TC-FE-01: invoice parsed_fields -> canonical extracted_data, SUCCESS."""
        result = self.node.execute({"parsed_fields": _parsed(), "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        ed = result["extracted_data"]
        assert ed["doc_type"] == "invoice"
        assert ed["doc_id"] == "INV-2024-0001"
        # Date normalized to ISO 8601 (zero-padded month/day).
        assert ed["issue_date"] == "2024-01-15"
        assert ed["due_date"] == "2024-02-15"
        # Amount strips the thousands comma and becomes a float.
        assert ed["total_amount"] == 8000.0
        # Default currency when none supplied.
        assert ed["currency"] == "JPY"
        assert len(ed["line_items"]) == 1
        # raw_fields preserved for audit traceability.
        assert ed["raw_fields"] == _parsed()

    def test_sku_normalization_upper_and_trim(self):
        """TC-FE-04: SKU '  sku-abc  ' -> 'SKU-ABC' and quantity -> float."""
        result = self.node.execute({"parsed_fields": _parsed(), "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        item = result["extracted_data"]["line_items"][0]
        assert item["sku"] == "SKU-ABC"
        assert item["quantity"] == 10.0
        assert item["unit_price"] == 500.0
        assert item["line_total"] == 5000.0

    def test_missing_doc_id_is_error(self):
        """TC-FE-02: a parsed_fields with no doc_id -> compliance ERROR."""
        result = self.node.execute(
            {
                "parsed_fields": _parsed(doc_id=None),
                "doc_type": "invoice",
                "out_of_scope": False,
            }
        )

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]  # non-empty
        # The failure names doc_id as the missing mandatory field.
        assert "doc_id" in result["error_log"][-1]

    def test_missing_line_items_is_error(self):
        """Branch: no line items -> compliance ERROR (>=1 line_item required)."""
        result = self.node.execute(
            {
                "parsed_fields": _parsed(line_items=[]),
                "doc_type": "invoice",
                "out_of_scope": False,
            }
        )

        assert result["status"] == AgentStatus.ERROR
        assert "line_items" in result["error_log"][-1]

    def test_out_of_scope_short_circuits(self):
        """TC-FE-03: out_of_scope=True -> no-op pass-through (SUCCESS, no extract)."""
        result = self.node.execute({"parsed_fields": _parsed(), "doc_type": "invoice", "out_of_scope": True})

        assert result == {"status": AgentStatus.SUCCESS}
        assert "extracted_data" not in result

    def test_missing_parsed_fields_is_error(self):
        """Branch: parsed_fields absent/not a dict -> ERROR (DocumentParse must run)."""
        result = self.node.execute({"parsed_fields": None, "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
