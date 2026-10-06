# RET-C2-334 — Proof-of-Boundary: PB-01 S-3 output gate
#
# Boundary: the S-3 output gate (_run_s3_domain_gate, ADR-017) is wired and
# CANNOT be bypassed — residual account-number / PII in the delivered payload is
# masked, and the required output schema is enforced.
#
# The gate is intentionally NOT named _extra_security_gate_output: the released
# agenticstar-agentcore SDK auto-wraps that hook name into the LangGraph chain
# (clean path returns None → next node gets state=None → AttributeError at
# .invoke()). The node implements _run_s3_domain_gate and calls it EXPLICITLY
# from execute().
#
# Asserted against the develop implementation:
#   - OutputValidateNode._run_s3_domain_gate exists and is callable.
#   - A formatted_output carrying a raw bank-account-like digit run is masked
#     ([REDACTED]) by the gate; legitimate numeric leaves are untouched.
#   - _extra_security_gate_output is NOT defined on the subclass (auto-wrap guard).
#   - _security_gate_output is @final in FunctionNode and is NOT overridden by
#     OutputValidateNode (overriding it would raise TypeError at import).
#
# S-4 audit: emit_trace_event muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import pytest

from src.nodes.output_validate_node import OutputValidateNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.output_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestPoB1S3OutputGate:
    """PB-01: the S-3 output gate masks PII and is not bypassable."""

    def setup_method(self):
        self.node = OutputValidateNode()

    def test_gate_exists_and_is_callable(self):
        """The S-3 domain gate is defined on the concrete node and is callable."""
        assert hasattr(self.node, "_run_s3_domain_gate")
        assert callable(self.node._run_s3_domain_gate)
        assert "_run_s3_domain_gate" in OutputValidateNode.__dict__
        # The SDK-auto-wrapped hook must NOT be defined on the subclass.
        assert "_extra_security_gate_output" not in OutputValidateNode.__dict__, (
            "_extra_security_gate_output is SDK-auto-wrapped — must not be defined; "
            "use the explicit _run_s3_domain_gate instead"
        )

    def test_final_gate_not_overridden(self):
        """ADR-017: the @final _security_gate_output is NOT overridden."""
        # Overriding the @final framework gate would raise TypeError on import;
        # the fact that the module imported AND the subclass does not define it
        # proves only the custom-named _run_s3_domain_gate is added.
        assert "_security_gate_output" not in OutputValidateNode.__dict__

    def test_gate_masks_account_number_in_output(self):
        """TC-PB-01: a raw account number in formatted_output is masked by the gate."""
        raw_payload = {
            "formatted_output": {
                "template_id": "RET-C2-334",
                "doc_type": "invoice",
                "extracted_data": {
                    "doc_id": "INV-2024-0001",
                    "supplier_name": "Pay to bank account 9988776655 by EOM",
                },
                "erp_validation": {},
                "out_of_scope": False,
                "compliance_note": "ok",
            }
        }

        gated = self.node._run_s3_domain_gate(raw_payload)
        supplier_name = gated["formatted_output"]["extracted_data"]["supplier_name"]

        # The long digit run is removed / masked from the delivered payload.
        assert "9988776655" not in supplier_name
        assert "[REDACTED]" in supplier_name

    def test_gate_enforces_required_schema_keys(self):
        """The gate backfills the required top-level keys so output is well-formed."""
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
            assert key in fo, f"required output key missing after gate: {key}"

    def test_gate_preserves_numeric_fields(self):
        """Masking must NOT corrupt legitimate numeric fields (quantity/price)."""
        gated = self.node._run_s3_domain_gate(
            {
                "formatted_output": {
                    "template_id": "RET-C2-334",
                    "doc_type": "invoice",
                    "extracted_data": {
                        "total_amount": 5000.0,
                        "line_items": [{"sku": "SKU-100", "quantity": 10, "unit_price": 500.0}],
                    },
                    "erp_validation": {},
                    "out_of_scope": False,
                    "compliance_note": "ok",
                }
            }
        )

        ed = gated["formatted_output"]["extracted_data"]
        assert ed["total_amount"] == 5000.0
        assert ed["line_items"][0]["quantity"] == 10
        assert ed["line_items"][0]["unit_price"] == 500.0
