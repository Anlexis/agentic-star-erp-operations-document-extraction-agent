"""The Marketplace runner invokes every agent as
    agent.invoke(message, ctx=ctx, input_context={"conversation_history": history})
(agenticstar-agentcore, shared/bootstrap/marketplace_app.py), whatever the user typed. A caller
contract that refuses undeclared keys therefore refused every chat request before the question
was read, with an empty error_log. These run the whole graph with exactly that shape.
"""
import importlib
import json
import re
from pathlib import Path

from framework.schemas import InvocationContext
from framework.schemas.trust_level import TrustLevel

_REPO = Path(__file__).resolve().parents[2]
_PAYLOAD = json.loads((_REPO / "deploy" / "invoke_payload.json").read_text(encoding="utf-8"))
_CLASS = re.search(r'^class:\s*"?([\w.]+)"?', (_REPO / "config" / "agent.yaml").read_text(encoding="utf-8"), re.M).group(1)
_MODULE, _NAME = _CLASS.rsplit(".", 1)


def _invoke(input_context):
    graph_cls = getattr(importlib.import_module(_MODULE), _NAME)
    return graph_cls().invoke(
        _PAYLOAD["input"],
        session_id="",
        ctx=InvocationContext(caller_id="test", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context=input_context,
    )


def _status(result):
    status = result.get("status")
    return getattr(status, "value", status)


def test_the_marketplace_chat_shape_is_admitted():
    assert _status(_invoke({"conversation_history": []})) == "success"


def test_prior_turns_do_not_refuse_the_current_request():
    # Ordinary earlier turns ride along untouched. (Injection- or credential-shaped
    # content in a prior turn is refused by the framework's own S-2 gate before any
    # template code runs — that is the platform's decision, not this contract's.)
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "How can I help?"}]
    assert _status(_invoke({"conversation_history": history})) == "success"


def test_an_undeclared_key_is_still_refused():
    assert _status(_invoke({"conversation_history": [], "recipient_name": "x"})) != "success"
