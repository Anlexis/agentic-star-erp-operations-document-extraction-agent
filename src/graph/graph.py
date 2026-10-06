"""AgentCore Platform v1.0"""

# RET-C2-334 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (ERPDocumentExtractionGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph):
#     input_validate → document_parse → field_extract → erp_validate → output_validate
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ RETERPDocumentExtractionAgent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ERPDocumentExtractionGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No Level 0 platform SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.utils.config_loader import load_agent_config
from src.graph.context_bridge import set_caller_contract
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# src/graph/<this file> -> parents[2] is the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Used only when config/config.yaml is unreadable. Kept in one place so the
# declared values and the fallback cannot drift into two different contracts.
_FALLBACK_ERP: Dict[str, Any] = {"total_tolerance": 0.01, "max_line_items": 1000}


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The registry loads this file and passes it to the graph constructor; the
    standalone entry point does the same, so `max_retry` and the ERP tuning are
    live in both deployments rather than declared and ignored.

    Reading config/agent.yaml here instead would return nothing usable: the
    manifest carries identity and compile-time requirements only, and it has no
    `agent.config` block any more. A reader still pointed at it would degrade
    silently to defaults — the failure mode this function exists to prevent.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


class ERPDocumentExtractionGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of RETERPDocumentExtractionAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before
    post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pass the validated document text into the inner graph
      merge_output()  — map sub_result fields into the outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    # "handle": call on_subgraph_error() instead — use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    # True: surface inner HITL interrupt to the outer caller.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """The ERP tuning to hand the inner graph, read from the live config file.

        Falls back to the module constant only when the file carries no `erp`
        block, so a deployment that removed it still runs with a known contract
        rather than with whatever each node happened to default to.
        """
        erp = runtime_config().get("erp")
        if not isinstance(erp, dict) or not erp:
            erp = dict(_FALLBACK_ERP)
        return {"erp": dict(erp)}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.

        The inner graph receives the runtime tuning through its constructor; its
        domain nodes still take no constructor arguments and read their tuning
        per call from seeded state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the document text, and bridge the validated caller contract.

        The framework hands the inner graph only a string
        (``subgraph.invoke(user_input, session_id=…, ctx=…)``), so the caller's
        structured parameters travel on the context bridge instead — set here,
        one step before the inner invoke, and read by the inner graph's
        initial-state hook. Without this the ERP reference data never reaches the
        node that cross-checks against it, and the SKU and supplier checks report
        "not verified" on every request.

        PreProcessNode writes the sanitized document to validated_input; prefer
        it, falling back to the raw user_input.
        """
        contract = state.get("caller_contract")
        set_caller_contract(contract if isinstance(contract, dict) else {})
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output()   emits: "formatted_output", "out_of_scope",
                                      "status", "trace_id", "correlation_id",
                                      "node_history"
          This merge_output() reads: sub_result.get("formatted_output"),
                                     sub_result.get("out_of_scope"),
                                     sub_result.get("status")

        The main-slot node also surfaces "result" for the outer backbone /
        PostProcessNode and downstream FinalizeNode.
        """
        return {
            "result": sub_result.get("formatted_output"),
            "formatted_output": sub_result.get("formatted_output"),
            "out_of_scope": sub_result.get("out_of_scope", False),
            "status": sub_result.get("status"),
        }


class RETERPDocumentExtractionAgent(AgentBaseGraph):
    """Outer graph for RET-C2-334 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ERPDocumentExtractionGraphNode (main slot), which delegates
    to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (S-1 input sanitization)
      - main:         ERPDocumentExtractionGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "RETERPDocumentExtractionAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ERPDocumentExtractionGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — callers may reference either name.
Graph = RETERPDocumentExtractionAgent
