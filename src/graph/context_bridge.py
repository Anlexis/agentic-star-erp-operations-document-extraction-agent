"""AgentCore Platform v1.0"""

# Caller-parameter bridge across the outer/inner graph boundary.
#
# Why it exists: the framework invokes a nested graph as
#
#     subgraph.invoke(user_input, session_id=ctx.session_id, ctx=ctx)
#
# (framework/nodes/graph_node.py). Only the request STRING crosses. The
# structured invocation parameters are NOT forwarded, and neither is the outer
# state — so `state.get("input_context")` inside the inner graph is always the
# empty mapping, whatever the caller sent.
#
# That is not a theoretical gap for this agent: the ERP cross-check reads its
# reference data from the caller's parameters and lives in the INNER graph, so
# without a bridge the SKU and supplier checks can never run on any request. They
# would report "not verified" for every document while the suite stayed green,
# because a unit test that constructs the node itself passes state in directly.
#
# The two sanctioned subclass hooks bridge it:
#
#   ERPDocumentExtractionGraphNode.extract_input(state)  [BEFORE subgraph.invoke]
#       -> set_caller_contract(<validated contract>)
#   DomainWorkflowGraph._extra_initial_state()           [INSIDE subgraph.invoke]
#       -> seeds the contract into the inner state
#
# What crosses is the VALIDATED contract only — every code has already passed its
# inert-alphabet check and the tolerance has already been parsed as a finite
# bounded number. The raw request body never travels.
#
# Carrying the reference data inside the request string instead is not viable:
# the platform rewrites personal-data shapes out of that field at every node
# boundary, and its name heuristic reads consecutive title-case words as personal
# names — so supplier names and product descriptions arrive masked there. This
# channel is not rewritten.
#
# A ContextVar keeps the hand-off correct per thread and per task, so concurrent
# invocations inside one process cannot see each other's parameters.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CALLER_CONTRACT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ret_c2_334_caller_contract", default=None)


def set_caller_contract(contract: Optional[Dict[str, Any]]) -> None:
    """Stash the validated caller contract for the imminent inner-graph invoke."""
    _CALLER_CONTRACT.set(dict(contract) if contract else {})


def get_caller_contract() -> Dict[str, Any]:
    """Read (without consuming) the stashed contract; {} when none was set."""
    return _CALLER_CONTRACT.get() or {}
