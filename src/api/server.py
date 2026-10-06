"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# On the hosted platform the gateway calls agent.invoke() directly instead.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event
from src.graph.graph import RETERPDocumentExtractionAgent, runtime_config
from src.services.caller_contract import ContractError, normalize_input_context

app = FastAPI(title="Agent")

# The registry loads config/config.yaml and passes it as Graph(config=...); this
# standalone server mirrors that exactly, so max_retry and the ERP tuning are
# live in both deployments instead of only one. Constructing the graph bare here
# would leave every declared runtime parameter read by nothing.
agent = RETERPDocumentExtractionAgent(config=runtime_config())
agent.compile()
# The secret provider is scoped by the identity the manifest declares, so a
# secret resolves from the same place here and under the registry. The provider
# reads `env/namespaces/{namespace}/…` and `env/agents/{namespace}/{name}/…`, so
# a namespace that disagrees with config/agent.yaml silently splits one agent's
# secrets across two stores — with no error at boot, because a missing tier file
# is ignored by design. `namespace` is lower(industry) — "ret" — not the
# lowercased Template ID.
# tests/integration/test_manifest_identity_alignment.py holds these two values to
# the manifest; it reads both sides rather than restating them.
agent.provision_secrets(secrets_factory(namespace="ret", agent_name="RETERPDocumentExtractionAgent"))

#: Coarse upper bound on the serialized structured parameters (bytes). The
#: contract enforces per-field bounds — inert code alphabets, a finite numeric
#: range, an entry cap; this keeps an oversized payload from being parsed at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # The ERP reference data to cross-check the document against. This is the
    # channel it belongs on: the platform rewrites personal-data shapes out of
    # `input` at every node boundary, and its name heuristic reads consecutive
    # title-case words as personal names — so supplier and product names
    # embedded in the document arrive masked there. This channel is not
    # rewritten, which is exactly why everything on it is validated below.
    input_context: dict[str, Any] | None = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth. When INVOKE_AUTH_TOKEN is set on the server
    # environment, a caller that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and then runs at
    # VERIFIED_EXTERNAL — the level this agent's manifest declares. Trust already
    # established by middleware is never demoted.
    #
    # This is required, not optional hardening: every node of this agent declares
    # VERIFIED_EXTERNAL, and nothing else sets request.state.trust_level in a
    # standalone deployment. Without this boundary every request arrives
    # ANONYMOUS, the trust gate denies it at the first node, and the agent
    # returns an error for every call while looking healthy.
    #
    # STG_INTERNAL_RUNNER_TOKEN is accepted on the same footing. The deployment
    # recipe passes both variables, and the evidence harness presents whichever
    # the manifest's declared level calls for; accepting both means a manifest
    # change cannot silently leave the entry point unauthenticated. It grants
    # VERIFIED_EXTERNAL, never INTERNAL — this adapter never mints a trust level
    # above the one the manifest publishes.
    #
    # A deployment-level caller credential, not an agent secret, so the secrets
    # provider does not apply: no invocation context exists before auth.
    if trust is TrustLevel.ANONYMOUS:
        accepted = [
            value
            for value in (
                os.environ.get("INVOKE_AUTH_TOKEN"),
                os.environ.get("STG_INTERNAL_RUNNER_TOKEN"),
            )
            if value
        ]
        if accepted:
            supplied = request.headers.get("authorization", "").encode()
            # Compare bytes: compare_digest raises TypeError on non-ASCII str
            # input (headers decode as latin-1), which would 500 rather than
            # answering the generic 401. Every candidate is compared so the work
            # does not depend on which token matched.
            matched = False
            for value in accepted:
                if secrets.compare_digest(supplied, f"Bearer {value}".encode()):
                    matched = True
            if not matched:
                # Generic body on purpose — never reveal whether the token was
                # absent, malformed, or simply wrong.
                raise HTTPException(status_code=401, detail="Token is invalid or expired.")
            trust = TrustLevel.VERIFIED_EXTERNAL

    raw_context = req.input_context or {}
    if raw_context and len(json.dumps(raw_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    # Validate here AND inside the graph. The hosted gateway calls agent.invoke()
    # directly and never reaches this function, so the node owns the guarantee;
    # what this adds is that only declared fields are forwarded at all.
    try:
        input_context = normalize_input_context(raw_context)
    except ContractError as exc:
        emit_trace_event(
            "input_context_rejected",
            # The field name has passed an inert-name check inside the validator.
            # The rejected value never appears — here, in the response, or in the
            # audit record.
            {"field": exc.field},
            {"session_id": req.session_id},
        )
        # 400, not 422: pydantic owns 422 and answers there with a list of error
        # objects, so reusing it would make client handling ambiguous.
        raise HTTPException(status_code=400, detail=f"{exc.field} {exc.reason}.") from None

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "RETERPDocumentExtractionAgent"}
