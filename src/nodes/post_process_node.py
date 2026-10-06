"""AgentCore Platform v1.0"""

# RET-C2-334 — PostProcessNode (outer backbone, post_process slot)
#
# The last node that can withhold anything from the caller, and therefore the
# one place the containment contract has to hold.
#
# What the caller actually receives is built by the framework as
#
#     {"output": state["formatted_output"] or state["result"], "status", …}
#
# (framework/graph/agent_base_graph.py). Two consequences drive everything below:
#
#  * `result` IS caller-visible. The main slot writes the domain payload to BOTH
#    `formatted_output` and `result`, so a gate that blocks `formatted_output`
#    and leaves `result` in place has blocked nothing — the fallback ships the
#    un-gated payload. Withholding means CLEARING every output-bearing field,
#    not replacing one of them.
#  * A FALSY replacement re-opens the same fallback. `formatted_output = {}` or
#    `""` selects `result` again. The withheld envelope is therefore a non-empty
#    mapping carrying a constant reason code.
#
# The envelope carries CLOSED-SET labels only — a reason code chosen from the
# constants in this module, and counts. It never carries `error_log`, a caught
# exception's message, a node-authored string, or any fragment of the document.
# `error_log` stays exactly as it is: it is the internal audit channel, the state
# reducer appends to it, and it is not projected to the caller. Publishing it
# would put upstream text and identifiers on a caller-visible channel through a
# field nobody thinks of as output.

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import REQUIRED_OUTPUT_KEYS
from src.services.caller_contract import screen_credentials

#: Every state field that can carry released document text to the caller, either
#: directly through the framework's output selection or through a future reader
#: of the merged state. All of them are cleared together on a withheld path.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "extracted_data",
    "parsed_fields",
    "erp_validation_result",
)

#: The closed set of reasons the caller may be told. Nothing outside this tuple
#: is ever placed in the delivered envelope.
_REASON_WORKFLOW_FAILED = "workflow_failed"
_REASON_OUTPUT_WITHHELD = "output_withheld"
_REASONS = (_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD)


class PostProcessNode(FunctionNode):
    """Deliver the domain payload, or withhold it and say so in closed-set terms.

    Output (partial dict — only changed keys): formatted_output, status, and —
    on a withheld path — every output-bearing field, cleared.
    """

    # See the note in PreProcessNode: the level matches the manifest's declared
    # entry contract, checked against it by
    # tests/integration/test_manifest_identity_alignment.py.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        # An upstream failure reaches the caller as a reason code. The inner
        # entries are already in error_log; re-emitting them here would both
        # duplicate every line and publish them.
        if state.get("status") in (AgentStatus.ERROR, AgentStatus.ERROR.value):
            emit_trace_event("post_process_withheld", {"reason": _REASON_WORKFLOW_FAILED}, state)
            return self._contain(_REASON_WORKFLOW_FAILED)

        payload = state.get("formatted_output")
        if not isinstance(payload, dict) or not payload:
            payload = state.get("result") if isinstance(state.get("result"), dict) else None
        if not isinstance(payload, dict) or not payload:
            emit_trace_event("post_process_withheld", {"reason": _REASON_WORKFLOW_FAILED}, state)
            return self._contain(_REASON_WORKFLOW_FAILED)

        violations = self._gate_output(payload)
        if violations:
            emit_trace_event(
                "post_process_withheld",
                {"reason": _REASON_OUTPUT_WITHHELD, "violations": len(violations)},
                state,
            )
            return self._contain(_REASON_OUTPUT_WITHHELD, violations=len(violations))

        return {
            "formatted_output": payload,
            "result": payload,
            "status": AgentStatus.SUCCESS,
        }

    # ── Output gate ───────────────────────────────────────────────────────────

    def _gate_output(self, payload: Dict[str, Any]) -> List[str]:
        """Return closed-set violation labels for *payload*; empty when clean.

        Runs on the assembled payload immediately before it is delivered, so it
        sees exactly what the caller would receive — not an earlier draft of it.

        The credential screen is the union of the framework's detector and the
        local shapes it does not carry (see src/services/caller_contract.py). It
        has to be at least as wide as the framework's: the framework's own output
        gate scans every value of this result too, and a value it catches that
        this gate missed makes the framework RAISE inside the node wrapper, which
        discards the clearing below and returns a bare error instead. A narrower
        local screen is a containment bypass, not a lighter check.

        The labels are constants; no fragment of the payload is carried out.
        """
        violations: List[str] = []
        missing = [key for key in REQUIRED_OUTPUT_KEYS if key not in payload]
        if missing:
            violations.append("schema_incomplete")
        if screen_credentials(payload):
            violations.append("credential_in_output")
        return violations

    # ── Containment ───────────────────────────────────────────────────────────

    def _contain(self, reason: str, violations: int = 0) -> Dict[str, Any]:
        """Return an ERROR delta that releases nothing.

        Every output-bearing field is cleared, including `result` — the field the
        framework falls back to. The envelope is a non-empty mapping so it cannot
        re-select that fallback by being falsy, and its values come only from the
        closed set declared in this module.
        """
        # `reason` is always one of the module constants above. It is not
        # re-checked here: raising inside execute() would be caught by the node
        # wrapper, which returns a bare error and discards this clearing — so a
        # defensive raise on the containment path would defeat containment. The
        # closed set is enforced by test instead, across every path that can
        # return ERROR.
        contained: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
        envelope: Dict[str, Any] = {"reason": reason}
        if violations:
            envelope["violations"] = violations
        contained["formatted_output"] = envelope
        contained["status"] = AgentStatus.ERROR
        return contained
