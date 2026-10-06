# Test Specification — RET-C2-334

## Test Strategy

Deterministic throughout: the pipeline invokes no model and makes no network
call, so every test below is reproducible and none is a sampling exercise.

The split that matters here is not unit-versus-integration but **what a level can
prove**. Three of this template's properties are invisible below the entry point,
and each of them was a live defect found by driving the real entry point:

- whether the caller's reference data crosses the nested-graph boundary is a
  property of the bridge, not of a node — a node-level test seeds the state
  itself and cannot tell a working bridge from a broken one;
- whether an authenticated caller can complete a request depends on state the
  entry point sets;
- what the caller actually receives is `formatted_output or result`, selected by
  the framework after the last node returns.

So the contract tests run through the real ASGI application with Bearer auth.
Node-level tests cover the parsing and normalisation rules, where they are the
sharper instrument.

Both directions are probed for every screen. A screen that refuses legitimate
requests is a worse failure than one that is slightly wide, so each hostile case
has an ordinary-domain-text counterpart drawn from the repository's own fixtures
(`tests/integration/sample_documents.py`).

## Framework Compliance Tests

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat TypedDict | No Pydantic/dataclass in post-invoke state | `tests/proof_of_boundary/test_state_safety.py` |
| TC-03 | No credential in State | CI `gate-credential-scan`: 0 violations | CI + `scripts/check_credentials.py` |
| TC-05 | No duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from node bodies | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-06 | `_security_gate_input()` not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | `_security_gate_output()` not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` declared and consistent with the manifest | No node demands more trust than the manifest publishes | `tests/integration/test_manifest_identity_alignment.py` |
| TC-11 | At least one domain `emit_trace_event()` per `execute()` | Gate PASS | `scripts/check_audit_trace.py` |
| TC-12 | Provisioned secret identity matches the manifest | namespace and agent name read from both sides and equal | `tests/integration/test_manifest_identity_alignment.py` |

TC-09 / TC-10 (the domain extension hooks) are **not applicable**: this template
does not use those hooks. Its domain output gate is an explicitly invoked method
on `OutputValidateNode`, covered by PB-3 below, and its domain input screening
lives in `src/services/caller_contract.py`, covered by the caller-contract tests.

## Proof-of-Boundary Tests

| PB-ID | Boundary | Test | Where |
|-------|----------|------|-------|
| PB-1 | Node → audit trail | A domain event fires on every invocation path | `scripts/check_audit_trace.py`, node unit tests |
| PB-2 | State serialization | Post-invoke state is primitives only | `tests/proof_of_boundary/test_state_safety.py` |
| PB-3 | Delivered payload → caller | Identifiers survive the output mask; account-number shapes do not; nested values and mapping keys are scanned | `tests/proof_of_boundary/test_output_boundary.py` |
| PB-4 | Import isolation | No platform SDK import; AST scan clean | `tests/proof_of_boundary/test_import_isolation.py` |
| PB-6 | Invoke execution order | Trust gate → lifecycle event → input gate → `execute()` → output gate → lifecycle event | `tests/proof_of_boundary/test_pb_invoke_order.py` |
| PB-7 | Human-in-the-loop interrupt propagation | Skipped — `config/config.yaml` does not enable it, and the test skips itself on that basis | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` |
| PB-8 | Extraction correctness | Canonical fields are produced from a realistic document | `tests/proof_of_boundary/test_extraction_correctness.py` |
| PB-9 | Out-of-scope handling | An unclassifiable document is answered, not failed | `tests/proof_of_boundary/test_out_of_scope.py` |
| PB-10 | Output gate wiring | The domain gate is invoked and the mandatory gate is not overridden | `tests/proof_of_boundary/test_s3_output_gate.py` |

PB-5 (external service connection) is **not applicable**: this template makes no
external call. The ERP reference data it cross-checks against is supplied by the
caller per request, which is what keeps the agent stateless.

## Entry-Contract Tests

`tests/integration/test_invoke_end_to_end.py`, through the real ASGI application:

| Test | What it holds down |
|---|---|
| Unauthenticated request refused | 401 at the entry point, before any node — not a 200 carrying an opaque error |
| Authenticated request completes | Every backbone node runs; the canonical record comes back non-empty |
| Reference data reaches the ERP check | The context bridge works end to end |
| The verdict moves with the reference data | Same document, different reference data, different answer |
| An empty reference list matches nothing | An empty list is not the same as an absent field |
| Caller tolerance changes the outcome | A declared value reaches the inner graph and changes the result |
| Non-finite tolerance refused | `NaN` / `Infinity` / `-Infinity` sent as a raw body, which is how they actually arrive |
| Out-of-contract tolerance refused | negative, non-numeric, boolean, over-range |
| Credential-shaped value refused | 400 naming the field, value never echoed |
| Control tokens refused | `<\|im_start\|>`, `[INST]`, `<<SYS>>` |
| Undeclared field refused | Ignoring a key is not removing it |
| Hostile field name not echoed | Field names are caller data too |
| Ordinary domain text passes | The screens do not fire on real requests |
| Entry cap enforced | Reference lists are bounded |
| Identifiers survive the output mask | The invoice number reaches the caller intact |
| The compliance note matches the payload | No field is claimed captured that the payload does not carry |
| The deploy payload matches the fixture | `deploy/invoke_payload.json` and these tests assert one contract |
| The deploy payload is actually served | The committed payload returns a real answer, not a refusal |

## Containment Tests

`tests/integration/test_error_envelope_containment.py`, parameterised over every
path that can return a non-success status:

- every output-bearing field is cleared, `result` included — it is what the
  framework falls back to;
- the withheld envelope is truthy, since a falsy one re-opens that fallback;
- every value in the envelope is drawn from the module's declared constants;
- a sentinel seeded into `error_log` — carrying a name, an e-mail address and a
  credential — appears nowhere in the returned mapping, walked recursively;
- `error_log` itself is left as it is: it is the internal audit channel, and it
  is simply not projected.

## Caller-Contract Tests

`tests/unit/test_caller_contract.py` exercises the validator directly, without a
framework wrapper in front of it — the guarantee has to hold in the code that
owns the contract, not only where a gate happens to be configured. Covers: shape,
undeclared fields, the normalisation that forwards declared fields only, a
per-field non-finite matrix, bounds, booleans, entry caps, inert alphabets,
control tokens raw and markup-stripped, hostile field names, and credential
shapes — both the framework's own set, which this screen must never be narrower
than, and the local shapes the framework does not carry.

## Business Logic Tests

Node-level, in `tests/unit/`:

| Test | Input | Expected |
|---|---|---|
| Document type detection | invoice / purchase order / delivery receipt / inventory report headers | correct `doc_type`; inventory matched before the generic invoice signature |
| Unclassifiable input | prose | `out_of_scope`, status success |
| Empty input | `""` | status error |
| Field normalisation | raw captures | ISO dates, finite floats, upper-cased codes |
| Non-finite amounts | over-long digit runs, `nan`, `inf` | rejected to `None`, which the mandatory-field check treats as absent |
| Redacted supplier name | a value containing the platform's mask token | reported as redacted, never certified as extracted |
| ERP cross-check | known / unknown / absent / empty reference data | matched, unmatched, skipped, all-unmatched respectively |
| Line-total reconciliation | totals inside and outside tolerance | `total_check` true / false with a flag |
| Payload assembly | full and out-of-scope states | complete schema on both |

## Test Execution Summary

- Suite: 201 passed, 2 skipped, run against the real framework wheel.
- The 2 skips are PB-7, which skips itself because this template does not enable
  human-in-the-loop interrupts.
- `ruff check` and `ruff format --check` clean on both layers; `mypy src` clean.
