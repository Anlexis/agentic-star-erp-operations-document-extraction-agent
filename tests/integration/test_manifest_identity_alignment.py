# The identity and the entry contract the manifest declares, against what the
# code actually does.
#
# The manifest is the source of truth for who this agent is and who may call it.
# Two things are checked here, and BOTH sides of each are READ rather than
# restated — a test that spelled the expected value out twice would keep passing
# through exactly the drift it exists to catch.
#
# 1. Secret scoping. The secret provider is scoped by the declared identity: it
#    reads `env/namespaces/{namespace}/.env.{env}` and
#    `env/agents/{namespace}/{agent_name}/.env.{env}`. Under the registry the
#    scope comes from config/agent.yaml; standalone it comes from whatever
#    src/api/server.py passes to the factory. When those disagree, one agent's
#    secrets live in two stores and a key provisioned for one deployment is
#    simply absent in the other — with no error at boot, because a missing tier
#    file is ignored by design. Nothing fails until a secret is declared, and
#    then it fails in only one deployment.
#
# 2. The trust contract. The framework denies a node whose required_trust_level
#    is higher than the caller's, and the order is
#    ANONYMOUS < VERIFIED_EXTERNAL < INTERNAL. If any node demands more than the
#    manifest publishes, a caller admitted at the published level is refused
#    mid-graph — the agent cannot serve its own declared contract. This repo
#    shipped exactly that: every domain node demanded INTERNAL against a
#    VERIFIED_EXTERNAL manifest.
#
# The direction of each alignment is pinned too, so re-aligning the wrong way
# (moving the manifest onto the code instead of the reverse) fails here rather
# than passing as a fix.
#
# Deterministic — no model, no network, no filesystem beyond the manifest and the
# node modules.

import importlib
import pkgutil
from pathlib import Path

from framework.nodes.function_node import FunctionNode
from framework.schemas.trust_level import TrustLevel
from framework.utils.config_loader import load_config

import src.nodes
from src.api.server import agent

# tests/integration/<this file> -> parents[2] is the repository root.
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "agent.yaml"
_MANIFEST = load_config(str(_MANIFEST_PATH))

# The provider the entry point bound at import time — the identity that is live
# in a standalone deployment, not a re-derivation of it.
_PROVISIONED = agent._secrets_provider

# Least- to most-privileged, matching the framework's own ordering.
_TRUST_ORDER = [TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL]


def _domain_nodes():
    """Every FunctionNode subclass this template defines, found by import."""
    found = []
    for info in pkgutil.iter_modules(src.nodes.__path__):
        module = importlib.import_module(f"src.nodes.{info.name}")
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, FunctionNode) and attr is not FunctionNode:
                if attr.__module__ == module.__name__:
                    found.append(attr)
    return found


def test_manifest_declares_the_identity_fields() -> None:
    """The fields the rest of this module compares against must be present.

    Without this, a manifest that lost `namespace:` would make the comparisons
    below `None == None` and the file would pass while asserting nothing.
    """
    assert _MANIFEST.get("namespace"), f"{_MANIFEST_PATH} declares no namespace"
    assert _MANIFEST.get("name"), f"{_MANIFEST_PATH} declares no name"
    assert _MANIFEST.get("industry"), f"{_MANIFEST_PATH} declares no industry"
    assert _MANIFEST.get("required_trust_level"), f"{_MANIFEST_PATH} declares no required_trust_level"


def test_provisioned_namespace_matches_the_manifest() -> None:
    assert _PROVISIONED._namespace == _MANIFEST["namespace"], (
        "src/api/server.py provisions secrets under namespace "
        f"{_PROVISIONED._namespace!r}, but config/agent.yaml declares "
        f"{_MANIFEST['namespace']!r}. A secret would resolve from a different "
        "store standalone than under the registry."
    )


def test_provisioned_agent_name_matches_the_manifest() -> None:
    assert _PROVISIONED._agent_name == _MANIFEST["name"], (
        "src/api/server.py provisions secrets for agent name "
        f"{_PROVISIONED._agent_name!r}, but config/agent.yaml declares "
        f"{_MANIFEST['name']!r}."
    )


def test_manifest_namespace_is_lower_industry() -> None:
    """`namespace:` is lower(industry_code) — the fleet-wide convention.

    Checked against the manifest's own `industry` field, so this pins which of
    the two values is the correct one to align on without hard-coding either.
    """
    assert _MANIFEST["namespace"] == _MANIFEST["industry"].lower(), (
        f"config/agent.yaml declares namespace {_MANIFEST['namespace']!r}; the "
        f"convention is lower(industry) = {_MANIFEST['industry'].lower()!r}."
    )


def test_this_template_defines_nodes_to_check() -> None:
    """Guard the sweep below: an empty set would make it assert nothing."""
    assert len(_domain_nodes()) >= 5, f"expected this template's nodes, found {_domain_nodes()}"


def test_no_node_demands_more_trust_than_the_manifest_publishes() -> None:
    """A caller admitted at the declared level must be able to finish a request."""
    declared = TrustLevel(_MANIFEST["required_trust_level"])
    ceiling = _TRUST_ORDER.index(declared)
    too_strict = {
        node.__name__: node.required_trust_level.value
        for node in _domain_nodes()
        if _TRUST_ORDER.index(node.required_trust_level) > ceiling
    }
    assert not too_strict, (
        f"config/agent.yaml publishes required_trust_level {declared.value!r}, but "
        f"these nodes demand more: {too_strict}. A caller at the published level "
        "is refused at the first of them, so the agent cannot serve its own "
        "declared contract."
    )


def test_every_node_declares_a_trust_level_explicitly() -> None:
    """No node may inherit the permissive default.

    The framework raises at class-definition time for a missing declaration, so
    a regression here shows up as a collection error rather than a failure — this
    test states the requirement so the reason is named either way.
    """
    for node in _domain_nodes():
        assert (
            "required_trust_level" in node.__dict__
        ), f"{node.__name__} does not declare required_trust_level in its class body"
