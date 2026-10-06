# RET-C2-334 — Unit Tests: outer graph (RETERPDocumentExtractionAgent) +
#                          inner graph (DomainWorkflowGraph) wiring
#
# Asserted against the MERGED develop implementation (c7d9d1e0):
#   - RETERPDocumentExtractionAgent (outer AgentBaseGraph) registers
#     ERPDocumentExtractionGraphNode in the "main" slot and does NOT override
#     add_edges().
#   - DomainWorkflowGraph (inner BaseGraph) registers all 5 domain nodes in
#     pipeline order: input_validate -> document_parse -> field_extract ->
#     erp_validate -> output_validate.
#   - Every node and the GraphNode is instantiated with NO constructor args
#     (SDK-v1 FunctionNode/GraphNode take no __init__ args) — register_nodes()
#     + get_subgraph() must not raise TypeError. This is the graph-construction
#     check that guards the config-arg instantiation regression.
#   - ERPDocumentExtractionGraphNode.merge_output() maps
#     sub_result["formatted_output"] -> "result" + "formatted_output" and passes
#     "out_of_scope" / "status" through (changed keys only).
#
# Framework-dependent instantiation is wrapped in ImportError skips (the SDK
# wheel may be absent in a bare local checkout). The merge_output / extract_input
# checks are pure dict ops and run unconditionally once the class imports.

import pytest


class TestInnerDomainWorkflowGraph:
    """Inner BaseGraph: identity, node registration, output shaping."""

    def test_inner_graph_instantiates(self):
        """DomainWorkflowGraph() must not raise NotImplementedError (ABCs filled)."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph

            graph = DomainWorkflowGraph()
            assert graph is not None
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")
        except NotImplementedError as exc:  # pragma: no cover
            pytest.fail(f"DomainWorkflowGraph has unimplemented ABC methods: {exc}")

    def test_inner_graph_identity(self):
        """Inner graph name + state_schema are the shared workflow id and State."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.schemas.state import State
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        assert graph.name == "erp_document_extraction_workflow"
        assert graph.state_schema is State

    def test_register_nodes_all_five_in_pipeline_order(self):
        """All 5 domain nodes register in the linear pipeline order, no-arg ctors.

        register_nodes() instantiates each node with NO constructor args
        (InputValidateNode(), DocumentParseNode(), ...). If any node were
        instantiated with a config argument, this call would raise
        TypeError: <Node>() takes no arguments — so this test is the
        graph-construction guard for that regression.
        """
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.nodes.input_validate_node import InputValidateNode
            from src.nodes.document_parse_node import DocumentParseNode
            from src.nodes.field_extract_node import FieldExtractNode
            from src.nodes.erp_validate_node import ERPValidateNode
            from src.nodes.output_validate_node import OutputValidateNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        # _nodes is populated by register_nodes(); call it directly to avoid
        # depending on a full compile().
        graph._nodes = {}
        graph.register_nodes()

        keys = list(graph._nodes.keys())
        assert keys == [
            "input_validate",
            "document_parse",
            "field_extract",
            "erp_validate",
            "output_validate",
        ], f"Domain nodes must register in pipeline order, got {keys}"
        assert isinstance(graph._nodes["input_validate"], InputValidateNode)
        assert isinstance(graph._nodes["document_parse"], DocumentParseNode)
        assert isinstance(graph._nodes["field_extract"], FieldExtractNode)
        assert isinstance(graph._nodes["erp_validate"], ERPValidateNode)
        assert isinstance(graph._nodes["output_validate"], OutputValidateNode)

    def test_get_output_surfaces_formatted_output(self):
        """get_output() emits formatted_output + out_of_scope + status + trace_id."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        graph = DomainWorkflowGraph()
        state = {
            "formatted_output": {"template_id": "RET-C2-334"},
            "out_of_scope": False,
            "status": "SUCCESS",
            "trace_id": "T-1",
            "correlation_id": "C-1",
            "node_history": ["input_validate"],
        }
        out = graph.get_output(state)
        assert out["formatted_output"] == {"template_id": "RET-C2-334"}
        assert out["out_of_scope"] is False
        assert "status" in out
        assert out["trace_id"] == "T-1"
        assert out["node_history"] == ["input_validate"]


class TestOuterGraphWiring:
    """Outer RETERPDocumentExtractionAgent + GraphNode mapping methods."""

    def test_outer_agent_registers_main_graph_node(self):
        """register_nodes() puts ERPDocumentExtractionGraphNode in the 'main' slot.

        register_nodes() also instantiates PreProcessNode(), PostProcessNode()
        and ERPDocumentExtractionGraphNode() with no constructor args — a
        config-arg regression would surface here as a TypeError.
        """
        try:
            from src.graph.graph import (
                RETERPDocumentExtractionAgent,
                ERPDocumentExtractionGraphNode,
            )
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        agent = RETERPDocumentExtractionAgent()
        agent._nodes = {}
        agent.register_nodes()

        assert "main" in agent._nodes
        assert isinstance(agent._nodes["main"], ERPDocumentExtractionGraphNode)
        # The other backbone slots are also filled with no-arg node instances.
        assert "pre_process" in agent._nodes
        assert "post_process" in agent._nodes

    def test_outer_graph_does_not_override_add_edges(self):
        """The outer backbone wiring belongs to the framework — add_edges not overridden."""
        try:
            from src.graph.graph import RETERPDocumentExtractionAgent
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        assert "add_edges" not in RETERPDocumentExtractionAgent.__dict__

    def test_outer_agent_identity(self):
        """RETERPDocumentExtractionAgent name + state_schema + Graph alias are correct."""
        try:
            from src.graph.graph import RETERPDocumentExtractionAgent, Graph
            from src.schemas.state import State
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        agent = RETERPDocumentExtractionAgent()
        assert agent.name == "RETERPDocumentExtractionAgent"
        assert agent.state_schema is State
        assert Graph is RETERPDocumentExtractionAgent

    def test_merge_output_maps_formatted_output_to_result(self):
        """merge_output maps sub_result['formatted_output'] -> 'result' + passes status."""
        try:
            from src.graph.graph import ERPDocumentExtractionGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = ERPDocumentExtractionGraphNode()
        sub_result = {
            "formatted_output": {"template_id": "RET-C2-334"},
            "out_of_scope": False,
            "status": "SUCCESS",
        }
        merged = node.merge_output({}, sub_result)
        assert merged["result"] == {"template_id": "RET-C2-334"}
        assert merged["formatted_output"] == {"template_id": "RET-C2-334"}
        assert merged["out_of_scope"] is False
        assert merged["status"] == "SUCCESS"

    def test_merge_output_returns_only_changed_keys(self):
        """merge_output must NOT echo the outer state back."""
        try:
            from src.graph.graph import ERPDocumentExtractionGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = ERPDocumentExtractionGraphNode()
        outer_state = {"user_input": "raw", "session_id": "S-1"}
        sub_result = {"formatted_output": {}, "out_of_scope": False, "status": "SUCCESS"}

        merged = node.merge_output(outer_state, sub_result)
        assert "user_input" not in merged
        assert "session_id" not in merged
        assert set(merged.keys()) == {"result", "formatted_output", "out_of_scope", "status"}

    def test_extract_input_prefers_validated_input(self):
        """extract_input() prefers validated_input, falling back to user_input."""
        try:
            from src.graph.graph import ERPDocumentExtractionGraphNode
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = ERPDocumentExtractionGraphNode()
        assert node.extract_input({"validated_input": "clean", "user_input": "raw"}) == "clean"
        assert node.extract_input({"user_input": "raw"}) == "raw"

    def test_get_subgraph_returns_inner_graph(self):
        """get_subgraph() returns a DomainWorkflowGraph instance (no-arg ctor)."""
        try:
            from src.graph.graph import ERPDocumentExtractionGraphNode
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            pytest.skip(f"Framework not installed in CI: {exc}")

        node = ERPDocumentExtractionGraphNode()
        assert isinstance(node.get_subgraph(), DomainWorkflowGraph)
