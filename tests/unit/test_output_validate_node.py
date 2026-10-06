# RET-C2-334 — Unit Tests: OutputValidateNode
#
# Inner DomainWorkflowGraph node 5 (final): assemble formatted_output, run the
# S-3 output gate (ADR-017 _run_s3_domain_gate — called EXPLICITLY from execute()
# rather than through the framework hook, so the payload is sanitised before the
# mandatory gate scans the result), and emit the S-4 audit trace.
#
# Asserted against the develop implementation:
#   - Happy path -> formatted_output with template_id="RET-C2-334", the doc_type,
#     extracted_data, erp_validation, out_of_scope=False, a compliance_note.
#   - out_of_scope=True input -> formatted_output.out_of_scope=True,
#     extracted_data={}, status=SUCCESS.
#   - The S-3 gate (_run_s3_domain_gate) masks residual account-number
#     PII and backfills required keys.
#   - _extra_security_gate_output is NOT defined on the subclass, so there is one
#     gate on this node rather than two running in an unobvious order.
#   - _security_gate_output is @final and is NOT overridden (only the custom-named
#     _run_s3_domain_gate is added) — overriding it would raise TypeError.
#
# S-4 audit: emit_trace_event muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import pytest

from src.nodes.output_validate_node import OutputValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.output_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _extracted():
    return {
        "doc_type": "invoice",
        "doc_id": "INV-2024-0001",
        "issue_date": "2024-01-15",
        "supplier_code": "SUP-001",
        "line_items": [{"sku": "SKU-100", "quantity": 10.0, "unit": "pcs", "unit_price": 500.0, "line_total": 5000.0}],
        "total_amount": 5000.0,
        "currency": "JPY",
    }


def _erp_valid():
    return {
        "sku_matches": [{"sku": "SKU-100", "matched": True, "erp_sku": "SKU-100"}],
        "supplier_match": True,
        "date_in_range": None,
        "total_check": True,
        "validation_flags": [],
        "overall_valid": True,
    }


class TestOutputValidateNode:
    """Unit tests for OutputValidateNode (payload assembly + S-3 gate)."""

    def setup_method(self):
        self.node = OutputValidateNode()

    def test_happy_path_builds_payload(self):
        """TC-OV-01: in-scope -> formatted_output with template_id + compliance_note."""
        result = self.node.execute(
            {
                "doc_type": "invoice",
                "out_of_scope": False,
                "extracted_data": _extracted(),
                "erp_validation_result": _erp_valid(),
            }
        )

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is False
        fo = result["formatted_output"]
        assert fo["template_id"] == "RET-C2-334"
        assert fo["doc_type"] == "invoice"
        assert fo["extracted_data"] == _extracted()
        assert fo["erp_validation"] == _erp_valid()
        assert fo["out_of_scope"] is False
        assert isinstance(fo["compliance_note"], str) and fo["compliance_note"].strip()

    def test_out_of_scope_yields_empty_extracted(self):
        """TC-OV-02: out_of_scope=True -> out_of_scope payload, extracted_data={}."""
        result = self.node.execute({"doc_type": None, "out_of_scope": True})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        fo = result["formatted_output"]
        assert fo["template_id"] == "RET-C2-334"
        assert fo["out_of_scope"] is True
        assert fo["extracted_data"] == {}
        assert fo["erp_validation"] == {}

    def test_in_scope_without_data_reflags_out_of_scope(self):
        """Branch: in-scope but no extracted_data -> re-flag out_of_scope, SUCCESS."""
        result = self.node.execute({"doc_type": "invoice", "out_of_scope": False, "extracted_data": {}})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["formatted_output"]["extracted_data"] == {}

    def test_s3_gate_masks_residual_account_pii(self):
        """S-3: _run_s3_domain_gate masks long digit runs in the payload."""
        gated = self.node._run_s3_domain_gate(
            {
                "formatted_output": {
                    "template_id": "RET-C2-334",
                    "doc_type": "invoice",
                    "extracted_data": {"note": "remit to account 1234567890 immediately"},
                    "erp_validation": {},
                    "out_of_scope": False,
                    "compliance_note": "ok",
                }
            }
        )

        masked = gated["formatted_output"]["extracted_data"]["note"]
        assert "1234567890" not in masked
        assert "[REDACTED]" in masked

    def test_s3_gate_backfills_required_keys(self):
        """S-3: the gate backfills any missing required top-level key."""
        gated = self.node._run_s3_domain_gate({"formatted_output": {"template_id": "RET-C2-334"}})

        fo = gated["formatted_output"]
        for key in (
            "template_id",
            "doc_type",
            "extracted_data",
            "erp_validation",
            "out_of_scope",
            "compliance_note",
        ):
            assert key in fo

    def test_s3_gate_preserves_numeric_leaves(self):
        """S-3: numeric leaves (quantity / price) are NOT corrupted by masking."""
        gated = self.node._run_s3_domain_gate(
            {
                "formatted_output": {
                    "template_id": "RET-C2-334",
                    "doc_type": "invoice",
                    "extracted_data": {"line_items": [{"sku": "SKU-100", "unit_price": 500.0, "quantity": 10}]},
                    "erp_validation": {},
                    "out_of_scope": False,
                    "compliance_note": "ok",
                }
            }
        )

        item = gated["formatted_output"]["extracted_data"]["line_items"][0]
        assert item["unit_price"] == 500.0
        assert item["quantity"] == 10

    def test_final_security_gate_not_overridden(self):
        """ADR-017: the gate is the custom-named _run_s3_domain_gate; the
        auto-wrapped + @final framework hooks are NOT overridden."""
        # The concrete subclass must define the explicit domain gate ...
        assert "_run_s3_domain_gate" in OutputValidateNode.__dict__
        assert callable(OutputValidateNode._run_s3_domain_gate)
        # ... must NOT define the SDK-auto-wrapped hook (auto-wrap → None state) ...
        assert "_extra_security_gate_output" not in OutputValidateNode.__dict__, (
            "_extra_security_gate_output is SDK-auto-wrapped — must not be defined; "
            "use the explicit _run_s3_domain_gate instead"
        )
        # ... and must NOT override the @final framework gate.
        assert "_security_gate_output" not in OutputValidateNode.__dict__
