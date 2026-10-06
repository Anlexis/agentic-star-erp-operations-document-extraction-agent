# RET-C2-334 — Unit Tests: ERPValidateNode
#
# Inner DomainWorkflowGraph node 4: cross-validate extracted fields against the
# caller's ERP reference data. The caller sends it as
# input_context["erp_reference"]; it reaches this node as the validated
# `caller_contract`, seeded into inner state by the graph's initial-state hook.
# Deterministic — no live ERP API in v1.
#
# Asserted against the MERGED develop implementation (c7d9d1e0):
#   - A matching SKU + supplier in erp_reference -> all matched=True,
#     no validation_flags, overall_valid=True.
#   - An unknown SKU -> matched=False, a validation flag, overall_valid=False.
#   - No erp_reference -> SKU/supplier checks SKIPPED (matched/supplier_match
#     None); with no flags overall_valid stays True; status=SUCCESS.
#   - A line-total mismatch vs the document total -> a flag + overall_valid=False.
#   - out_of_scope=True -> no-op pass-through (SUCCESS).
#
# Validation mismatches are NOT errors — only system/infra failures are.
#
# S-4 audit: emit_trace_event muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import pytest

from src.nodes.erp_validate_node import ERPValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.erp_validate_node.emit_trace_event",
        lambda *a, **k: None,
    )


def _extracted(**overrides):
    """A canonical extracted_data dict with one reconciling line item."""
    base = {
        "doc_type": "invoice",
        "doc_id": "INV-2024-0001",
        "issue_date": "2024-01-15",
        "supplier_code": "SUP-001",
        "line_items": [{"sku": "SKU-100", "quantity": 10.0, "unit": "pcs", "unit_price": 500.0, "line_total": 5000.0}],
        "total_amount": 5000.0,
        "currency": "JPY",
    }
    base.update(overrides)
    return base


def _state(extracted, erp_reference=None):
    """Build inner-graph state as the graph itself builds it.

    The reference data is seeded on `caller_contract`, NOT on `input_context`.
    That is not a cosmetic rename: the framework invokes a nested graph with the
    request string only and forwards no structured parameters, so `input_context`
    inside this graph is always empty on every real request. These tests used to
    place the data there and pass — proving the node worked on a channel nothing
    in production ever fills, which is exactly the shape of defect a unit test is
    supposed to catch rather than hide.

    tests/integration/test_invoke_end_to_end.py drives the same data through the
    real entry point, which is the only place the wiring itself can be proven.
    """
    state = {"extracted_data": extracted, "doc_type": "invoice", "out_of_scope": False}
    if erp_reference is not None:
        state["caller_contract"] = dict(erp_reference)
    return state


class TestERPValidateNode:
    """Unit tests for ERPValidateNode (SKU/supplier/total cross-checks)."""

    def setup_method(self):
        self.node = ERPValidateNode()

    def test_matching_reference_is_overall_valid(self):
        """TC-EV-01: matching SKU + supplier -> matched=True, overall_valid=True."""
        result = self.node.execute(
            _state(
                _extracted(),
                erp_reference={"sku_master": ["SKU-100"], "supplier_master": ["SUP-001"]},
            )
        )

        assert result["status"] == AgentStatus.SUCCESS
        res = result["erp_validation_result"]
        assert res["sku_matches"][0]["matched"] is True
        assert res["sku_matches"][0]["erp_sku"] == "SKU-100"
        assert res["supplier_match"] is True
        assert res["total_check"] is True
        assert res["validation_flags"] == []
        assert res["overall_valid"] is True

    def test_unknown_sku_flags_and_invalidates(self):
        """TC-EV-02: unknown SKU -> matched=False, a flag, overall_valid=False."""
        result = self.node.execute(
            _state(
                _extracted(),
                erp_reference={"sku_master": ["SKU-999"], "supplier_master": ["SUP-001"]},
            )
        )

        assert result["status"] == AgentStatus.SUCCESS
        res = result["erp_validation_result"]
        assert res["sku_matches"][0]["matched"] is False
        assert res["sku_matches"][0]["erp_sku"] is None
        assert res["validation_flags"]  # non-empty
        assert res["overall_valid"] is False

    def test_no_reference_skips_checks_success(self):
        """TC-EV-03: no erp_reference -> checks skipped (None), SUCCESS, no flags."""
        result = self.node.execute(_state(_extracted()))  # no input_context at all

        assert result["status"] == AgentStatus.SUCCESS
        res = result["erp_validation_result"]
        # SKUs recorded as unverified (matched=None), supplier check skipped.
        assert res["sku_matches"][0]["matched"] is None
        assert res["supplier_match"] is None
        # total_check still runs (it needs no reference) and reconciles here.
        assert res["total_check"] is True
        assert res["validation_flags"] == []
        # No check actively failed -> overall_valid stays True.
        assert res["overall_valid"] is True

    def test_total_mismatch_flags(self):
        """Branch: line-item total != document total -> a flag + overall_valid=False."""
        bad = _extracted(total_amount=9999.0)  # line_total sums to 5000.0
        result = self.node.execute(
            _state(bad, erp_reference={"sku_master": ["SKU-100"], "supplier_master": ["SUP-001"]})
        )

        assert result["status"] == AgentStatus.SUCCESS
        res = result["erp_validation_result"]
        assert res["total_check"] is False
        assert res["validation_flags"]
        assert res["overall_valid"] is False

    def test_out_of_scope_short_circuits(self):
        """TC-EV-04: out_of_scope=True -> no-op pass-through (SUCCESS, no result)."""
        state = _state(_extracted())
        state["out_of_scope"] = True
        result = self.node.execute(state)

        assert result == {"status": AgentStatus.SUCCESS}
        assert "erp_validation_result" not in result

    def test_missing_extracted_data_is_error(self):
        """Branch: extracted_data absent/not a dict -> ERROR (FieldExtract must run)."""
        result = self.node.execute({"extracted_data": None, "doc_type": "invoice", "out_of_scope": False})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
