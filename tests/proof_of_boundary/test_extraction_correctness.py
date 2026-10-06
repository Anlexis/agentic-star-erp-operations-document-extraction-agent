# RET-C2-334 — Proof-of-Boundary: PB-02 Extraction correctness
#
# Boundary: given a well-formed supplier invoice, the extraction pipeline
# correctly captures every mandatory Denshi-Chobo-Hozon-Ho field
# (doc_id + issue_date + supplier_code + >=1 line item).
#
# The five domain nodes are driven in their real pipeline order
# (input_validate -> document_parse -> field_extract -> erp_validate ->
# output_validate), threading each node's partial-dict output into a shared
# state — exactly the linear topology DomainWorkflowGraph wires. This exercises
# the REAL extraction regexes/parsers/normalizers, not a mock.
#
# Asserted against the MERGED develop implementation (c7d9d1e0).
#
# S-4 audit: emit_trace_event muted at every node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

import re

import pytest

from src.nodes.input_validate_node import InputValidateNode
from src.nodes.document_parse_node import DocumentParseNode
from src.nodes.field_extract_node import FieldExtractNode
from src.nodes.erp_validate_node import ERPValidateNode
from src.nodes.output_validate_node import OutputValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free functions at every node module under test."""
    for mod in (
        "input_validate_node",
        "document_parse_node",
        "field_extract_node",
        "erp_validate_node",
        "output_validate_node",
    ):
        monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)


# A realistic, well-formed supplier invoice fixture (mixed JA/EN labels).
_INVOICE_FIXTURE = (
    "請求書 / INVOICE\n"
    "Invoice No: INV-2024-0042\n"
    "発行日: 2024-04-01\n"
    "取引先コード: SUP-789\n"
    "Supplier: Yamada Trading Co.\n"
    "----------------------------------------\n"
    "SKU-1001 | 12 | pcs | 1500 | 18000\n"
    "SKU-1002 | 3 | box | 4000 | 12000\n"
    "----------------------------------------\n"
    "合計: 30000\n"
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _run_pipeline(text, erp_reference=None):
    """Drive the 5 domain nodes in pipeline order against a single shared state."""
    state = {
        "user_input": text,
        "validated_input": text,
        "error_log": [],
        "node_history": [],
    }
    if erp_reference is not None:
        state["input_context"] = {"erp_reference": erp_reference}

    for node in (
        InputValidateNode(),
        DocumentParseNode(),
        FieldExtractNode(),
        ERPValidateNode(),
        OutputValidateNode(),
    ):
        delta = node.execute(state)
        state.update(delta)
    return state


class TestPoB2ExtractionCorrectness:
    """PB-02: mandatory ERP / Denshi-Chobo-Hozon-Ho fields are extracted."""

    def test_well_formed_invoice_extracts_mandatory_fields(self):
        """TC-PB-02: every mandatory field is captured from a clean invoice."""
        state = _run_pipeline(
            _INVOICE_FIXTURE,
            erp_reference={
                "sku_master": ["SKU-1001", "SKU-1002"],
                "supplier_master": ["SUP-789"],
            },
        )

        assert state["status"] == AgentStatus.SUCCESS
        assert state["doc_type"] == "invoice"
        assert state["out_of_scope"] is False

        ed = state["extracted_data"]
        # doc_id present and non-empty.
        assert isinstance(ed["doc_id"], str) and ed["doc_id"] == "INV-2024-0042"
        # issue_date normalized to ISO 8601.
        assert _ISO_DATE.match(ed["issue_date"])
        assert ed["issue_date"] == "2024-04-01"
        # supplier_code present and non-empty.
        assert isinstance(ed["supplier_code"], str) and ed["supplier_code"] == "SUP-789"
        # at least one line item, each with sku + quantity.
        assert len(ed["line_items"]) >= 1
        assert len(ed["line_items"]) == 2
        for item in ed["line_items"]:
            assert "sku" in item and item["sku"]
            assert "quantity" in item
        # currency default applied.
        assert ed["currency"] == "JPY"

    def test_pipeline_total_reconciles_against_erp(self):
        """The line-item totals reconcile against the document total (18000+12000=30000)."""
        state = _run_pipeline(
            _INVOICE_FIXTURE,
            erp_reference={
                "sku_master": ["SKU-1001", "SKU-1002"],
                "supplier_master": ["SUP-789"],
            },
        )

        res = state["erp_validation_result"]
        assert res["total_check"] is True
        assert res["overall_valid"] is True
        assert res["validation_flags"] == []

    def test_final_payload_is_well_formed(self):
        """The S-3-gated formatted_output carries the full schema for the caller."""
        state = _run_pipeline(_INVOICE_FIXTURE)
        fo = state["formatted_output"]
        assert fo["template_id"] == "RET-C2-334"
        assert fo["doc_type"] == "invoice"
        assert fo["out_of_scope"] is False
        assert fo["extracted_data"]["doc_id"] == "INV-2024-0042"
