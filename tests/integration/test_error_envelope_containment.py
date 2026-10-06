# What a caller receives when this agent withholds its output.
#
# The framework builds the caller's answer as
# `{"output": state["formatted_output"] or state["result"], "status", …}`, with no
# status check. Two properties follow, and both are held down here:
#
#  1. Clearing `formatted_output` is not containment. `result` carries the same
#     payload and is selected by the fallback, so a gate that replaces one and
#     leaves the other has withheld nothing.
#  2. A FALSY replacement re-opens that fallback. `{}` and `""` both select
#     `result` again, so the withheld envelope has to be truthy.
#
# And the envelope itself must carry closed-set labels only. `error_log` is the
# internal audit channel: nodes append to it, the framework appends a caught
# exception and its traceback to it, and anything interpolated into it by a node
# ends up there too. Publishing it under `formatted_output` would put upstream
# text and document fragments on a caller-visible channel through a field nobody
# thinks of as output.

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    _OUTPUT_BEARING_FIELDS,
    _REASONS,
    PostProcessNode,
)

# A recognisable sentinel with the shapes the contract says must never ship: a
# personal name, an e-mail address, and a credential.
_SENTINEL = "boom: upstream said {'customer':'A. Tanaka','mail':'a@b.co','token':'sk-live-abcdefghijklmnopqrstuv'}"

_PAYLOAD = {
    "template_id": "RET-C2-334",
    "doc_type": "invoice",
    "extracted_data": {"doc_id": "INV-20260114001"},
    "erp_validation": {"overall_valid": True},
    "out_of_scope": False,
    "mandatory_fields_captured": ["doc_id"],
    "mandatory_fields_missing": [],
    "compliance_note": "ok",
}


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


@pytest.fixture
def node():
    return PostProcessNode()


def _error_state():
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [_SENTINEL],
        "result": _PAYLOAD,
        "formatted_output": _PAYLOAD,
        "extracted_data": _PAYLOAD["extracted_data"],
    }


def _violating_state():
    """A SUCCESS payload that the output gate must refuse to deliver."""
    payload = dict(_PAYLOAD)
    payload["extracted_data"] = {"doc_id": "INV-1", "leaked": "AKIAIOSFODNN7EXAMPLE"}
    return {
        "status": AgentStatus.SUCCESS.value,
        "error_log": [_SENTINEL],
        "result": payload,
        "formatted_output": payload,
    }


def _all_withholding_states():
    return {
        "upstream-error": _error_state(),
        "gate-violation": _violating_state(),
        "no-payload": {"status": AgentStatus.SUCCESS.value, "error_log": [_SENTINEL]},
    }


# ── The happy path still delivers ────────────────────────────────────────────


def test_a_clean_payload_is_delivered_unchanged(node):
    """Guard the tests below: if nothing were ever delivered, containment would
    be vacuous and every assertion here would pass for the wrong reason."""
    result = node.execute({"status": AgentStatus.SUCCESS.value, "formatted_output": _PAYLOAD})
    assert result["status"] == AgentStatus.SUCCESS
    assert result["formatted_output"] == _PAYLOAD


# ── Containment ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", sorted(_all_withholding_states()))
def test_every_withholding_path_clears_every_output_bearing_field(node, label):
    """Enumerated over every path that can return non-success, not just one.

    `result` is the one that matters and the one most easily forgotten: it is
    what the framework falls back to, so leaving it populated ships the un-gated
    payload under an ERROR status.
    """
    result = node.execute(_all_withholding_states()[label])
    assert result["status"] == AgentStatus.ERROR
    for field in _OUTPUT_BEARING_FIELDS:
        if field == "formatted_output":
            continue
        assert result[field] is None, f"{field} was not cleared on the {label} path"


@pytest.mark.parametrize("label", sorted(_all_withholding_states()))
def test_the_withheld_envelope_is_truthy(node, label):
    """A falsy replacement re-selects `result` through the framework fallback."""
    result = node.execute(_all_withholding_states()[label])
    assert result["formatted_output"], "a falsy envelope re-opens the result fallback"


@pytest.mark.parametrize("label", sorted(_all_withholding_states()))
def test_the_envelope_carries_only_closed_set_values(node, label):
    """Every value is a module constant or a count — nothing node-authored."""
    envelope = node.execute(_all_withholding_states()[label])["formatted_output"]
    assert envelope["reason"] in _REASONS
    for key, value in envelope.items():
        assert key in ("reason", "violations")
        assert isinstance(value, (str, int)), f"{key} carries {type(value).__name__}"
        if isinstance(value, str):
            assert value in _REASONS


@pytest.mark.parametrize("label", sorted(_all_withholding_states()))
def test_no_error_text_reaches_the_caller_on_any_path(node, label):
    """The sentinel appears nowhere in the returned mapping, walked recursively."""
    result = node.execute(_all_withholding_states()[label])
    rendered = json.dumps(result, default=str)
    for fragment in ("boom", "A. Tanaka", "a@b.co", "sk-live-"):
        assert fragment not in rendered, f"{fragment!r} reached the caller on the {label} path"


def test_the_payload_that_violated_the_gate_is_not_delivered(node):
    """The credential that triggered the refusal must not ride out in the answer."""
    result = node.execute(_violating_state())
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(result, default=str)


def test_a_schema_incomplete_payload_is_withheld(node):
    incomplete = {"template_id": "RET-C2-334"}
    result = node.execute({"status": AgentStatus.SUCCESS.value, "formatted_output": incomplete})
    assert result["status"] == AgentStatus.ERROR
    assert result["formatted_output"]["reason"] in _REASONS


def test_error_log_itself_is_left_alone(node):
    """The internal channel keeps its entries; it is simply not projected.

    The audit trail needs them, and the state reducer appends — re-emitting them
    from here would duplicate every line as well as publish it.
    """
    result = node.execute(_error_state())
    assert "error_log" not in result
