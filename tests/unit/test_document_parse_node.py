# RET-C2-334 — Unit Tests: DocumentParseNode
#
# Inner DomainWorkflowGraph node 2: convert raw document text into
# doc-type-specific raw key-value pairs. Pure deterministic parsing
# (regex / line-anchored) — NO LLM in v1.
#
# Asserted against the MERGED develop implementation (c7d9d1e0):
#   - A well-formed invoice -> parsed_fields with doc_id, issue_date,
#     supplier_code, total_amount, line_items, and the carried doc_type.
#   - out_of_scope=True -> the node no-ops (status=SUCCESS only).
#   - A document with no recognisable lines (and an unmatched doc_type, but
#     not flagged out_of_scope) -> status=ERROR (defensive: no parser).
#   - An empty validated_input -> status=ERROR.
#
# S-4 audit: emit_trace_event muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import pytest

from src.nodes.document_parse_node import DocumentParseNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.document_parse_node.emit_trace_event",
        lambda *a, **k: None,
    )


_INVOICE_TEXT = (
    "INVOICE\n"
    "Invoice No: INV-2024-0001\n"
    "Issue Date: 2024-01-15\n"
    "Supplier Code: SUP-001\n"
    "SKU-100 | 10 | pcs | 500 | 5000\n"
    "SKU-200 | 2 | box | 1500 | 3000\n"
    "Total: 8000\n"
)


class TestDocumentParseNode:
    """Unit tests for DocumentParseNode (regex parse + dispatch + no-op)."""

    def setup_method(self):
        self.node = DocumentParseNode()

    def test_happy_path_invoice_parsed(self):
        """TC-DP-01: well-formed invoice -> parsed_fields with the expected keys."""
        result = self.node.execute({"validated_input": _INVOICE_TEXT, "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        pf = result["parsed_fields"]
        assert pf["doc_id"] == "INV-2024-0001"
        assert pf["issue_date"] == "2024-01-15"
        assert pf["supplier_code"] == "SUP-001"
        assert pf["total_amount"] == "8000"
        assert pf["doc_type"] == "invoice"
        # Two SKU rows match the line-item anchor.
        assert len(pf["line_items"]) == 2
        first = pf["line_items"][0]
        assert first["sku"] == "SKU-100"
        assert first["quantity"] == "10"
        assert first["unit"] == "pcs"
        assert first["unit_price"] == "500"
        assert first["line_total"] == "5000"

    def test_out_of_scope_short_circuits(self):
        """TC-DP-02: out_of_scope=True -> no-op pass-through (SUCCESS, no parse)."""
        result = self.node.execute({"validated_input": "anything", "doc_type": None, "out_of_scope": True})

        assert result == {"status": AgentStatus.SUCCESS}
        assert "parsed_fields" not in result

    def test_unknown_doc_type_without_flag_is_error(self):
        """TC-DP-03: doc_type with no registered parser (not flagged) -> ERROR."""
        result = self.node.execute(
            {
                "validated_input": "some unrecognised body text",
                "doc_type": "mystery_type",
                "out_of_scope": False,
            }
        )

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]  # non-empty

    def test_empty_validated_input_is_error(self):
        """Branch: empty validated_input -> ERROR (cannot parse)."""
        result = self.node.execute({"validated_input": "   ", "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
