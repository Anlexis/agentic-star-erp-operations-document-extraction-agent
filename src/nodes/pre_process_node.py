"""AgentCore Platform v1.0"""

# Node contract (§1 of the node design notes):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-334 — PreProcessNode (outer backbone, pre_process slot)
#
# Two jobs, both of which have to happen here rather than deeper in:
#
#  1. Accept the document text and hand it on as validated_input.
#  2. Validate the caller's structured parameters and publish the normalized
#     contract for the main slot to bridge into the inner graph.
#
# Job 2 belongs in a node, not only in the HTTP adapter, because the hosted
# gateway calls agent.invoke() directly and never passes through the adapter. A
# guarantee that only the adapter enforces is absent on that path. The adapter
# calls the SAME validator, so the two cannot disagree about what is acceptable.
#
# Rejection is a closed-set outcome: the reason names the FIELD that failed and
# never repeats the value, because the value is caller text and the error log is
# an audit record.

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.caller_contract import ContractError, validate_caller_contract

#: Upper bound on the document text this agent will parse. The parse is
#: line-anchored and linear, so the cap is about bounding the work a single
#: request can ask for, not about correctness.
MAX_DOCUMENT_CHARS = 200_000


class PreProcessNode(FunctionNode):
    """Validate the incoming document and the caller's structured parameters.

    Runs in the outer backbone's ``pre_process`` slot, before the main slot
    delegates to the domain pipeline.

    Output (partial dict — only changed keys):
        validated_input, caller_contract, enriched_context, status, error_log.
    """

    # The manifest declares this agent's entry contract as VERIFIED_EXTERNAL;
    # every node in this template declares the same level, so a caller admitted
    # at the declared level can complete a request. Declaring a HIGHER level on
    # a node than the manifest declares for the agent makes the agent refuse its
    # own published contract at that node.
    # tests/integration/test_manifest_identity_alignment.py holds this to the
    # manifest by reading both sides rather than restating either.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])
        user_input = state.get("user_input", "")

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + ["PreProcessNode: document text is empty or missing"],
            }

        if len(user_input) > MAX_DOCUMENT_CHARS:
            emit_trace_event(
                "document_rejected",
                {"reason": "too_large", "limit": MAX_DOCUMENT_CHARS},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"PreProcessNode: document exceeds the {MAX_DOCUMENT_CHARS}-character limit"],
            }

        # input_context is read-only here; the normalized result is published as
        # its own state field rather than written back over the caller's.
        try:
            contract: Dict[str, Any] = validate_caller_contract(state.get("input_context"))
        except ContractError as exc:
            emit_trace_event(
                "caller_contract_rejected",
                # The field name has itself passed an inert-name check inside the
                # validator; the rejected VALUE never appears here.
                {"field": exc.field},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"PreProcessNode: {exc.field} {exc.reason}"],
            }

        emit_trace_event(
            "document_accepted",
            {
                "char_len": len(user_input.strip()),
                "reference_fields": sorted(contract),
            },
            state,
        )

        return {
            "validated_input": user_input.strip(),
            "caller_contract": contract,
            "enriched_context": {
                "source": "RETERPDocumentExtractionAgent",
                "reference_fields": sorted(contract),
            },
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
