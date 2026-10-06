"""AgentCore Platform v1.0"""

# RET-C2-334 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full ERP / operations document extraction workflow:
#
#   START → input_validate → document_parse → field_extract
#         → erp_validate → output_validate → END
#
# Called by ERPDocumentExtractionGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with ERPDocumentExtractionGraphNode.merge_output()
#   ❌ No Level 0 platform SDK imports

import math
from typing import Any, Dict

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_contract
from src.nodes.document_parse_node import DocumentParseNode
from src.nodes.erp_validate_node import ERPValidateNode
from src.nodes.field_extract_node import FieldExtractNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_validate_node import OutputValidateNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-334.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ERPDocumentExtractionGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate   (InputValidateNode)
          → document_parse   (DocumentParseNode)
          → field_extract    (FieldExtractNode)
          → erp_validate     (ERPValidateNode)
          → output_validate  (OutputValidateNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "erp_document_extraction_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate the forwarded ERP tuning before compilation.

        The values arrive from config/config.yaml through the main slot's
        _parent_config(). They are operator-supplied rather than caller-supplied,
        but a bad value here is silently wrong for every request, so it is
        rejected at compile time rather than defaulted away per call.
        """
        erp = self.config.get("erp")
        if erp is None:
            return
        if not isinstance(erp, dict):
            raise ConfigError(f"[{self.__class__.__name__}] 'erp' must be a mapping, got: {type(erp).__name__}")

        tolerance = erp.get("total_tolerance")
        if tolerance is not None:
            if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
                raise ConfigError(f"[{self.__class__.__name__}] 'erp.total_tolerance' must be a number")
            if not math.isfinite(float(tolerance)) or float(tolerance) < 0:
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'erp.total_tolerance' must be a finite, non-negative number"
                )

        max_line_items = erp.get("max_line_items")
        if max_line_items is not None:
            if isinstance(max_line_items, bool) or not isinstance(max_line_items, int) or max_line_items < 1:
                raise ConfigError(f"[{self.__class__.__name__}] 'erp.max_line_items' must be a positive integer")

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the bridged caller contract and the live tuning into inner state.

        Both have to arrive this way. The framework hands the inner graph only a
        string, so the validated reference data travels on the context bridge;
        and node execute() methods receive no config argument, so the tuning
        travels as state or it is declared and never read.
        """
        erp = self.config.get("erp")
        return {
            "caller_contract": dict(get_caller_contract()),
            "erp_runtime_config": dict(erp) if isinstance(erp, dict) else {},
        }

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["document_parse"] = DocumentParseNode()
        self._nodes["field_extract"] = FieldExtractNode()
        self._nodes["erp_validate"] = ERPValidateNode()
        self._nodes["output_validate"] = OutputValidateNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear ERP document extraction domain topology.

        Each step passes its partial-dict output into the shared State.
        The topology is intentionally linear — no conditional branching
        between domain nodes. route() is implemented as required by the ABC
        but add_conditional_edges() is not used. Downstream nodes short-circuit
        on out_of_scope internally.

        Flow:
          input_validate → document_parse → field_extract
                        → erp_validate → output_validate
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "document_parse")
        self._sg.add_edge("document_parse", "field_extract")
        self._sg.add_edge("field_extract", "erp_validate")
        self._sg.add_edge("erp_validate", "output_validate")
        self._sg.add_edge("output_validate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. It is implemented to satisfy the ABC
        contract. Returns END on error so an unexpected call does not re-enter
        a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_validate"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ERPDocumentExtractionGraphNode.merge_output()
        in graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "formatted_output", "out_of_scope",
                                        "status", "trace_id", "correlation_id",
                                        "node_history"
            Outer merge_output() reads: sub_result.get("formatted_output"),
                                        sub_result.get("out_of_scope"),
                                        sub_result.get("status")

        Additional fields (trace_id, correlation_id, node_history) are surfaced
        for observability / downstream extension.
        """
        return {
            "formatted_output": state.get("formatted_output"),
            "out_of_scope": state.get("out_of_scope", False),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def get_state_class(self) -> type:
        """Return the State TypedDict used by both inner and outer graphs."""
        return State
