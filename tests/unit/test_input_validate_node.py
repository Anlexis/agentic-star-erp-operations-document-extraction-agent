# RET-C2-334 — Unit Tests: InputValidateNode
#
# Inner DomainWorkflowGraph node 1: document intake validation. Detects the
# document type from header / keyword signatures, builds lightweight intake
# metadata, flags out-of-scope (unknown / unparseable) inputs, and emits an
# S-4 audit trace.
#
# Asserted against the MERGED develop implementation (c7d9d1e0):
#   - A valid invoice text -> doc_type="invoice", out_of_scope=False, SUCCESS.
#   - A valid purchase-order text -> doc_type="purchase_order".
#   - The INV-STOCK inventory signature wins over the generic INV- invoice
#     signature (inventory pattern is checked first).
#   - An unrecognised document -> out_of_scope=True, status=SUCCESS (NOT ERROR).
#   - A completely empty payload -> status=ERROR, error_log populated.
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse
# fixture (NEVER stub shared.* in sys.modules — the CI wheel ships a real
# shared package).

from unittest.mock import MagicMock

import pytest

from src.nodes.input_validate_node import InputValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.input_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


_INVOICE_TEXT = (
    "INVOICE\n"
    "Invoice No: INV-2024-0001\n"
    "Issue Date: 2024-01-15\n"
    "Supplier Code: SUP-001\n"
    "SKU-100 | 10 | pcs | 500 | 5000\n"
    "Total: 5000\n"
)

_PO_TEXT = (
    "PURCHASE ORDER\n"
    "PO No: PO-2024-0007\n"
    "Order Date: 2024-02-01\n"
    "Supplier Code: SUP-002\n"
    "SKU-200 | 4 | box | 250 | 1000\n"
)

_INVENTORY_TEXT = (
    "INVENTORY REPORT\n" "Report No: INV-STOCK-2024-03\n" "As-of: 2024-03-31\n" "SKU-300 | 42 | pcs | 80 | 3360\n"
)


class TestInputValidateNode:
    """Unit tests for InputValidateNode (detection + out-of-scope + metadata)."""

    def setup_method(self):
        self.node = InputValidateNode()

    def test_happy_path_invoice_detected(self):
        """TC-IV-01: valid invoice text -> doc_type=invoice, in-scope, SUCCESS."""
        result = self.node.execute({"validated_input": _INVOICE_TEXT})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["doc_type"] == "invoice"
        assert result["out_of_scope"] is False
        # Intake metadata is built without echoing raw content.
        meta = result["doc_metadata"]
        assert meta["source"] == "text"
        assert meta["char_len"] == len(_INVOICE_TEXT.strip())
        # validated_input is the cleaned/trimmed text.
        assert result["validated_input"] == _INVOICE_TEXT.strip()

    def test_purchase_order_detected(self):
        """TC-IV-02: valid PO text -> doc_type=purchase_order."""
        result = self.node.execute({"validated_input": _PO_TEXT})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["doc_type"] == "purchase_order"
        assert result["out_of_scope"] is False

    def test_inventory_signature_wins_over_invoice(self):
        """Key branch: INV-STOCK is detected as inventory_report, not invoice.

        The inventory pattern is checked before the generic invoice INV-
        pattern, so an inventory report is never mis-detected as an invoice.
        """
        result = self.node.execute({"validated_input": _INVENTORY_TEXT})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["doc_type"] == "inventory_report"
        assert result["out_of_scope"] is False

    def test_falls_back_to_user_input_when_validated_input_blank(self):
        """Branch: blank validated_input -> the raw user_input is used."""
        result = self.node.execute({"validated_input": "   ", "user_input": _INVOICE_TEXT})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["doc_type"] == "invoice"

    def test_unknown_format_is_out_of_scope_success(self):
        """TC-IV-03: unrecognised document -> out_of_scope=True, SUCCESS (not ERROR)."""
        result = self.node.execute({"validated_input": "Hello there, this is just a friendly note with no structure."})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["doc_type"] is None

    def test_empty_input_is_error(self):
        """TC-IV-04: no validated_input and no user_input -> ERROR + error_log."""
        result = self.node.execute({})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]  # non-empty

    def test_audit_payload_never_echoes_raw_document(self):
        """S-4: the emitted audit PAYLOAD carries only metadata, never raw text."""
        spy = MagicMock()
        import src.nodes.input_validate_node as mod

        original = mod.emit_trace_event
        mod.emit_trace_event = spy
        try:
            result = self.node.execute({"validated_input": _INVOICE_TEXT})
        finally:
            mod.emit_trace_event = original

        assert result["status"] == AgentStatus.SUCCESS
        assert spy.called
        # The 2nd positional arg is the audit payload the logger persists; it
        # must carry only doc_type / char_len / source — never the raw SKU rows.
        emitted_payloads = repr([c.args[1] for c in spy.call_args_list if len(c.args) > 1])
        assert "SKU-100" not in emitted_payloads
        assert "SUP-001" not in emitted_payloads
