"""AgentCore Platform v1.0"""

# Validation of the structured invocation parameters a caller may send alongside
# the document.
#
# Two callers use this module and they must not drift apart:
#
#   src/api/server.py       normalizes the request before invoke(), so a field
#                           this contract does not declare never reaches the
#                           graph at all;
#   src/nodes/pre_process_node.py
#                           validates again inside the graph, because the hosted
#                           gateway calls agent.invoke() directly and never goes
#                           through the HTTP adapter. The node owns the
#                           guarantee; the adapter only narrows what can arrive.
#
# Both call validate_caller_contract(), so what the adapter refuses and what the
# node refuses are one set by construction.
#
# Why validation cannot be left to "we only declare inert fields": a validator
# that ignores an undeclared key does not remove it. The key stays in
# input_context, the framework's first backbone node copies input_context
# verbatim into its own result, and the mandatory output gate scans every value
# of every result — so an undeclared field carrying a credential-shaped string
# fails the FIRST node of the graph, before any of this code runs. Ignoring is
# not stripping. normalize_caller_contract() therefore returns a NEW mapping
# built only from declared fields, and the adapter sends that, not the original.
#
# Nothing here imports the framework except the credential detector, so every
# rule below can be exercised by calling these functions directly.

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials_in_value

# ---------------------------------------------------------------------------
# The declared contract
# ---------------------------------------------------------------------------

#: The only key the caller may send at the top level of input_context.
ERP_REFERENCE = "erp_reference"

#: The only keys erp_reference may carry.
_SKU_MASTER = "sku_master"
_SUPPLIER_MASTER = "supplier_master"
_TOTAL_TOLERANCE = "total_tolerance"
_ERP_REFERENCE_FIELDS = (_SKU_MASTER, _SUPPLIER_MASTER, _TOTAL_TOLERANCE)

#: Structural caps. A reference list is caller-sized, so it is bounded here
#: rather than wherever it is iterated — 500 codes is far above any realistic
#: per-request master extract and far below a payload that could stall a node.
MAX_REFERENCE_CODES = 500

#: Reference codes render into the delivered payload (the sku_matches table), so
#: they are locked to an inert alphabet rather than accepted as free text. Free
#: text in a rendered field is caller-controlled output.
_INERT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Bounds for the one caller-controlled number in the contract. Zero means exact
#: reconciliation; the ceiling keeps a tolerance from being widened until every
#: document reconciles trivially.
TOLERANCE_MIN = 0.0
TOLERANCE_MAX = 1_000_000.0

# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

# Chat-template control tokens, screened as a CLASS rather than as directive
# phrases. A phrase-matching screen ("ignore all previous instructions") misses
# the token form entirely, and the token form is the one that actually steers a
# chat-formatted model.
_CONTROL_TOKEN_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>]{0,64}\|>"),  # <|im_start|>, <|endoftext|>, …
    re.compile(r"\[/?INST\]", re.IGNORECASE),  # [INST] … [/INST]
    re.compile(r"<</?SYS>>", re.IGNORECASE),  # <<SYS>> … <</SYS>>
)

# Markup strippers are not refusals, and a strip can make an attack HARDER to
# see: removing the angle brackets from "<|im_start|>system do X" leaves plain
# text that no token screen matches. So every string is screened twice — once
# raw, which catches the token before a strip could erase it, and once with
# markup removed, which catches a directive spliced through inert tags
# ("ig<b>nore</b> the rules").
_MARKUP_RE = re.compile(r"<[^>]{0,256}>")

_DIRECTIVE_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+|any\s+)?(?:previous|prior|above)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s*prompt\b", re.IGNORECASE),
)

# Credential shapes the FRAMEWORK detector does not carry. The framework's
# patterns describe credential FORMATS (sk_live_…, AKIA…, eyJ…, Bearer …,
# db://…); none of them match an assignment like `password=hunter2`, a GitLab or
# GitHub token prefix, or a PEM private-key header. These are kept as a UNION
# with the framework set, never as a replacement for it: a local set that
# replaced the framework's would be NARROWER, and a narrower screen than the
# gate that runs after it is a bypass, not a tightening.
_LOCAL_CREDENTIAL_RES: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?token)\b\s*[:=]\s*\S{6,}")),
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_\-]{16,}")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws_secret", re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*\S{16,}")),
)


class ContractError(ValueError):
    """A caller field failed the contract.

    ``field`` names the offending field; the rejected VALUE is never carried on
    the exception, so no error path can echo it back to the caller or into a log.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _screen_control_tokens(text: str) -> Optional[str]:
    """Return the screen that fired on *text*, or None.

    Screened raw first, then with markup removed — see the note on
    ``_MARKUP_RE``. The returned label is a closed-set constant, never a slice
    of the caller's text.
    """
    for pattern in _CONTROL_TOKEN_RES:
        if pattern.search(text):
            return "control_token"
    stripped = _MARKUP_RE.sub("", text)
    for candidate in (text, stripped):
        for pattern in _DIRECTIVE_RES:
            if pattern.search(candidate):
                return "directive"
    return None


def screen_credentials(value: object) -> bool:
    """True when *value* carries a credential shape, by the union of both sets.

    The framework detector is the FLOOR: it already recurses mappings and lists,
    and it is the same function the mandatory output gate calls, so anything it
    catches is refused here too. The local patterns only ADD shapes it does not
    carry.
    """
    if detect_credentials_in_value(value):
        return True
    return _local_credentials(value)


def _local_credentials(value: object) -> bool:
    """Recurse *value*, applying only the local (non-framework) patterns."""
    if isinstance(value, str):
        return any(pattern.search(value) for _, pattern in _LOCAL_CREDENTIAL_RES)
    if isinstance(value, dict):
        return any(_local_credentials(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_local_credentials(item) for item in value)
    return False


def screen_text(value: object, field: str) -> None:
    """Raise ContractError when any string leaf of *value* fails a screen.

    Walks KEYS as well as values. JSON ``\\u`` escapes decode during parsing, so
    a post-parse walk sees the decoded text an escape was hiding; and a hostile
    field NAME is caller data exactly like a field value.
    """
    if isinstance(value, str):
        fired = _screen_control_tokens(value)
        if fired is not None:
            raise ContractError(field, f"contains a disallowed {fired} pattern")
        if screen_credentials(value):
            raise ContractError(field, "contains a credential-shaped value")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _screen_control_tokens(key) is not None:
                # The key itself is hostile; report it by position, not by name.
                raise ContractError(field, "carries a field name with a disallowed pattern")
            screen_text(item, field)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            screen_text(item, field)


def finite_in_range(raw: object, field: str, low: float, high: float) -> float:
    """Parse *raw* as a finite number inside [low, high], or raise.

    NaN and both infinities parse perfectly well through ``float()`` and arrive
    intact through ``json.loads`` (Python's JSON accepts the bare literals). Every
    comparison against NaN is False, so an unchecked NaN does not raise — it
    silently makes a threshold test pass, which is a fail-OPEN on the exact
    decision the field exists for. Rejecting here is fail-CLOSED.

    ``bool`` is rejected before the numeric check: ``isinstance(True, int)`` is
    True in Python, so ``True`` would otherwise be accepted as 1.
    """
    if isinstance(raw, bool):
        raise ContractError(field, "must be a number, not a boolean")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            raise ContractError(field, "is not a number") from None
    else:
        raise ContractError(field, "is not a number")
    if not math.isfinite(value):
        raise ContractError(field, "must be a finite number")
    if not (low <= value <= high):
        raise ContractError(field, f"must be between {low} and {high}")
    return value


def _reference_codes(raw: object, field: str) -> List[str]:
    """Validate one reference-code list into an upper-cased, de-duplicated list."""
    if not isinstance(raw, (list, tuple)):
        raise ContractError(field, "must be a list of reference codes")
    if len(raw) > MAX_REFERENCE_CODES:
        raise ContractError(field, f"carries more than {MAX_REFERENCE_CODES} entries")
    codes: List[str] = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not _INERT_CODE_RE.match(item):
            # Position, never the value: the rejected code is caller text.
            raise ContractError(f"{field}[{index}]", "is not an inert reference code")
        code = item.strip().upper()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


# Keys the platform itself puts into input_context, not the caller. The Marketplace
# runner invokes every agent as
#     agent.invoke(message, ctx=ctx, input_context={"conversation_history": history})
# (agenticstar-agentcore, shared/bootstrap/marketplace_app.py), whatever the user typed.
# Refusing it as an unknown field refused every chat request before the question was
# read. Discarded, not validated: nothing in this pipeline reads prior turns, and
# screening a transcript would let one earlier message refuse every later one. Discarding
# adds no exposure — the backbone's first node has already copied the raw input_context
# into state before this contract runs.
PLATFORM_RESERVED_KEYS = frozenset({"conversation_history"})


def validate_caller_contract(input_context: object) -> Dict[str, Any]:
    """Validate the caller's structured parameters into the normalized contract.

    Returns a NEW mapping containing only declared fields. An absent or empty
    input_context is valid and yields an empty contract — the pipeline then runs
    its document-only path rather than failing.

    Raises ContractError naming the offending field. The value that failed is
    never included.
    """
    if input_context is None:
        return {}
    if not isinstance(input_context, dict):
        raise ContractError("input_context", "must be a mapping")
    if not input_context:
        return {}

    input_context = {k: v for k, v in input_context.items() if k not in PLATFORM_RESERVED_KEYS}
    if not input_context:
        return {}

    # Screen the whole object before looking at any individual field: a hostile
    # string in a field this contract does not declare is still a hostile string
    # the caller sent, and refusing it is more useful than dropping it silently.
    screen_text(input_context, "input_context")

    unknown = [key for key in input_context if key != ERP_REFERENCE]
    if unknown:
        raise ContractError("input_context", f"declares no field named {_name_or_position(unknown[0], input_context)}")

    reference = input_context.get(ERP_REFERENCE)
    if reference is None:
        return {}
    if not isinstance(reference, dict):
        raise ContractError(ERP_REFERENCE, "must be a mapping")

    unknown_ref = [key for key in reference if key not in _ERP_REFERENCE_FIELDS]
    if unknown_ref:
        raise ContractError(ERP_REFERENCE, f"declares no field named {_name_or_position(unknown_ref[0], reference)}")

    contract: Dict[str, Any] = {}
    if _SKU_MASTER in reference:
        contract[_SKU_MASTER] = _reference_codes(reference[_SKU_MASTER], f"{ERP_REFERENCE}.{_SKU_MASTER}")
    if _SUPPLIER_MASTER in reference:
        contract[_SUPPLIER_MASTER] = _reference_codes(
            reference[_SUPPLIER_MASTER], f"{ERP_REFERENCE}.{_SUPPLIER_MASTER}"
        )
    if _TOTAL_TOLERANCE in reference:
        contract[_TOTAL_TOLERANCE] = finite_in_range(
            reference[_TOTAL_TOLERANCE], f"{ERP_REFERENCE}.{_TOTAL_TOLERANCE}", TOLERANCE_MIN, TOLERANCE_MAX
        )
    return contract


def normalize_input_context(input_context: object) -> Dict[str, Any]:
    """Validate, then rebuild input_context carrying ONLY declared fields.

    The HTTP adapter sends THIS, not the caller's original mapping. That is what
    makes the "undeclared key" case structurally impossible rather than merely
    rejected: the framework's first backbone node copies input_context verbatim
    into its own result and the mandatory output gate scans every value of every
    result, so an undeclared field carrying a credential shape would fail the
    first node of the graph with an opaque error. Rejecting it is good; not
    forwarding it is better, and both happen here.

    The shape round-trips: what this returns validates to the same contract, so
    the node inside the graph re-derives exactly what the adapter derived.
    """
    contract = validate_caller_contract(input_context)
    if not contract:
        return {}
    reference: Dict[str, Any] = {}
    for field in _ERP_REFERENCE_FIELDS:
        if field in contract:
            reference[field] = contract[field]
    return {ERP_REFERENCE: reference}


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _name_or_position(name: object, container: Dict[str, Any]) -> str:
    """Render an unknown field name safely, or fall back to its position.

    A field name is caller-controlled text like any value, so it is repeated back
    only when it is short, inert, and carries no credential shape.
    """
    if isinstance(name, str) and _SAFE_NAME_RE.match(name) and not screen_credentials(name):
        return repr(name)
    try:
        index = list(container.keys()).index(name) + 1  # type: ignore[arg-type]
    except ValueError:  # pragma: no cover - name always comes from the container
        index = 0
    return f"field #{index}"
