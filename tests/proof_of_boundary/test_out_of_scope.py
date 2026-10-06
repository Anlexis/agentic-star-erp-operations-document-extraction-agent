# RET-C2-334 — Proof-of-Boundary: PB-03 Out-of-scope boundary
#
# Boundary: the agent MUST NOT return ERROR for an unrecognised document. It
# MUST set out_of_scope=True with status=SUCCESS, and the final formatted_output
# must carry out_of_scope=True with an empty extracted_data — out-of-scope is a
# graceful-success boundary, NOT an error boundary.
#
# The five domain nodes are driven in their real pipeline order so the
# out_of_scope flag propagates exactly as DomainWorkflowGraph wires it
# (downstream nodes short-circuit on out_of_scope internally).
#
# Asserted against the MERGED develop implementation (c7d9d1e0).
#
# AgentStatus has only SUCCESS / ERROR — out-of-scope is NOT a separate status.
#
# S-4 audit: emit_trace_event muted at every node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared
# package).

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


# Genuinely unrecognised prose: NONE of the doc-type signatures
# InputValidateNode._detect_doc_type keys on appear here — no 'invoice',
# 'purchase order', 'delivery receipt', 'inventory report', 'INV-'/'PO-'/'DN-'
# /'INV-STOCK' ids, and no Japanese header markers (請求書/発注書/納品書/在庫).
# So _detect_doc_type returns None and the input gate flags out_of_scope=True.
_UNRECOGNISED_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "This is an ordinary paragraph of free-form prose with no document "
    "structure, no header signatures, and no recognisable business form. "
    "Just a few plain sentences about the weather and a walk in the park."
)


def _run_pipeline(text):
    """Drive the 5 domain nodes in pipeline order against a single shared state."""
    state = {
        "user_input": text,
        "validated_input": text,
        "error_log": [],
        "node_history": [],
    }
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


class TestPoB3OutOfScopeIsSuccess:
    """PB-03: an unrecognised document is graceful SUCCESS, not an error."""

    def test_unrecognised_document_flows_to_out_of_scope_success(self):
        """TC-PB-03: unknown doc -> out_of_scope=True + SUCCESS end-to-end."""
        state = _run_pipeline(_UNRECOGNISED_TEXT)

        # Out-of-scope, never ERROR.
        assert state["out_of_scope"] is True
        assert state["status"] == AgentStatus.SUCCESS
        assert state["status"] != AgentStatus.ERROR

        # The final payload reflects out-of-scope with empty extraction.
        fo = state["formatted_output"]
        assert fo["out_of_scope"] is True
        assert fo["extracted_data"] == {}
        assert fo["template_id"] == "RET-C2-334"

    def test_input_validate_flags_out_of_scope_at_the_gate(self):
        """The boundary is set at the input gate: out_of_scope=True, SUCCESS, doc_type None."""
        result = InputValidateNode().execute({"validated_input": _UNRECOGNISED_TEXT})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["doc_type"] is None

    def test_downstream_nodes_short_circuit_on_out_of_scope(self):
        """Once flagged, parse/extract/erp no-op (SUCCESS) without producing data."""
        flagged = {"validated_input": _UNRECOGNISED_TEXT, "doc_type": None, "out_of_scope": True}

        assert DocumentParseNode().execute(flagged) == {"status": AgentStatus.SUCCESS}
        assert FieldExtractNode().execute(flagged) == {"status": AgentStatus.SUCCESS}
        assert ERPValidateNode().execute(flagged) == {"status": AgentStatus.SUCCESS}
