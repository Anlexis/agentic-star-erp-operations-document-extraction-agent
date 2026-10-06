# Template Design Specification — RET-C2-334

## Position in AgentCore Architecture

- **Agent Class**: `RETERPDocumentExtractionAgent` (`src/graph/graph.py`)
- **Category**: Cat 2 — a domain pipeline for one job-to-be-done
- **Generation mode**: `deterministic`. No model is invoked anywhere in the
  pipeline; extraction is rule- and pattern-based. `requires.extras` and
  `requires.secrets` are therefore both empty in the manifest — declaring an
  unprovisioned secret would fail the agent at compile time.

| Decision | Value |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Inner graph base | `BaseGraph` — fully custom topology |
| Composition pattern | Nested: a `GraphNode` in the `main` slot wraps the inner graph |
| Error propagation | `propagate` — an inner failure re-raises and the backbone routes to `finalize` |
| Declared entry trust | `VERIFIED_EXTERNAL` (`config/agent.yaml`) |

**Three-layer separation**

- **State**: a flat `TypedDict` (`src/schemas/state.py`). Checkpoint
  serialization does not carry arbitrary Python objects safely, so no Pydantic
  models, dataclasses or other objects go in State — and no credentials.
- **Node**: `FunctionNode` subclasses overriding `execute(state) -> dict` and
  returning only the keys they change.
- **Graph**: composition via `register_nodes()`.

## Architecture Overview

### Outer backbone

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max_retry from config/config.yaml)
                                          pre_process
```

`add_edges()` is not overridden — the backbone belongs to the framework. Note
that `route()` sends any non-success status to `finalize`, **skipping
post_process**: an upstream failure never reaches the delivery node.

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| initialize | framework default | — | session/trace identity, trust level |
| pre_process | `PreProcessNode` — accept the document, validate the caller's parameters | `user_input`, `input_context` | `validated_input`, `caller_contract` |
| main | `ERPDocumentExtractionGraphNode` — delegate to the inner graph | `validated_input`, `caller_contract` | `result`, `formatted_output`, `out_of_scope`, `status` |
| post_process | `PostProcessNode` — deliver the payload or withhold it | `formatted_output`, `result`, `status` | `formatted_output`, `result`, `status` |
| finalize | framework default | — | response metadata, timings |

### Inner domain workflow

```
START → input_validate → document_parse → field_extract → erp_validate → output_validate → END
```

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| `InputValidateNode` | detect the document type from header signatures | `validated_input` | `doc_type`, `doc_metadata`, `out_of_scope` |
| `DocumentParseNode` | pattern-anchored parse into raw fields | `validated_input`, `doc_type`, `erp_runtime_config` | `parsed_fields` |
| `FieldExtractNode` | normalise to the canonical schema | `parsed_fields` | `extracted_data` |
| `ERPValidateNode` | cross-check against the caller's reference data | `extracted_data`, `caller_contract`, `erp_runtime_config` | `erp_validation_result` |
| `OutputValidateNode` | assemble and gate the delivered payload | `extracted_data`, `erp_validation_result` | `formatted_output` |

An unrecognised document is **not** an error: `out_of_scope` is set, the
downstream nodes pass through, and a well-formed payload is still delivered
saying so. Errors are reserved for system failures.

### The nested-graph boundary

The framework invokes a nested graph as
`subgraph.invoke(user_input, session_id=…, ctx=…)`. **Only the request string
crosses.** Neither the outer state nor the caller's structured invocation
parameters are forwarded, so `state["input_context"]` inside the inner graph is
always empty.

This matters because the ERP cross-check reads the caller's reference data and
lives in the inner graph. Two hooks bridge it:

- `ERPDocumentExtractionGraphNode.extract_input()` runs immediately before the
  inner invoke and stashes the **validated** contract on a `ContextVar`
  (`src/graph/context_bridge.py`);
- `DomainWorkflowGraph._extra_initial_state()` runs inside the inner invoke and
  seeds it into inner state, alongside the runtime tuning.

Only validated data crosses: every reference code has already passed its inert
alphabet check and the tolerance has already been parsed as a finite, bounded
number.

The tuning travels the same way for a related reason — node `execute()` methods
receive no config argument, so a declared value that is not seeded into state is
read by nothing.

### State

| Field | Type | Purpose |
|---|---|---|
| `validated_input` | `str` | the sanitised document text |
| `caller_contract` | `dict` | validated reference codes and tolerance |
| `erp_runtime_config` | `dict` | tuning forwarded from `config/config.yaml` |
| `doc_type` | `str` | `invoice` / `purchase_order` / `delivery_receipt` / `inventory_report` |
| `doc_metadata` | `dict` | source hint and character length; never document content |
| `parsed_fields` | `dict` | raw captures, before normalisation |
| `extracted_data` | `dict` | the canonical record |
| `erp_validation_result` | `dict` | cross-check outcome |
| `formatted_output` | `dict` | the delivered payload |
| `out_of_scope` | `bool` | the document could not be classified |

## Caller contract

`input_context` declares exactly one field, `erp_reference`, carrying
`sku_master`, `supplier_master` and `total_tolerance`. Everything else is
refused rather than ignored, and the entry point forwards a mapping rebuilt from
declared fields only.

That is not pedantry about unknown keys. The framework's first backbone node
copies `input_context` verbatim into its own result, and the mandatory output
gate scans every value of every node result — so an undeclared field carrying a
credential-shaped string fails the first node of the graph before any template
code runs, and the caller gets an error with nothing pointing at the cause.
Refusing it at the entry point turns that into an actionable 400 naming the
field.

Reference codes render into the delivered payload, so they are restricted to a
short inert alphabet; free text in a rendered field is output the caller
controls. The one caller-supplied number goes through a finite, bounded parser:
`NaN` and both infinities parse through `float()` and arrive intact through raw
JSON, and every comparison against `NaN` is False — an unchecked one would
silently answer the reconciliation question the tolerance exists to decide.

## Security design

| Layer | Where |
|---|---|
| Trust gate | Every node declares `required_trust_level = VERIFIED_EXTERNAL`, matching the manifest. The entry point authenticates a Bearer token and runs the caller at that level; `tests/integration/test_manifest_identity_alignment.py` holds code and manifest together by reading both. |
| Input screening | `src/services/caller_contract.py`, called by both the entry point and `PreProcessNode`, so the guarantee does not depend on which one a deployment goes through. Chat-template control tokens are screened as a class, raw and markup-stripped, across keys and values. |
| Output gate | `OutputValidateNode._run_s3_domain_gate()` masks residual account-number shapes and completes the schema; `PostProcessNode` then re-screens the assembled payload and withholds it on violation. |
| Containment | Withholding clears every output-bearing field including `result` — the field the framework falls back to — and replaces it with a truthy envelope of closed-set labels. `error_log` is never projected. |
| Audit | Every `execute()` emits a domain event via the `emit_trace_event` free function. The framework emits the node lifecycle events itself; templates must not duplicate them. |

Framework facilities used: `InvocationContext`, `TrustLevel`,
`detect_credentials_in_value`, `emit_trace_event`, `bound_secrets`. The
mandatory input and output gates are `@final` on `FunctionNode` — overriding
either raises `TypeError` at class definition; domain checks extend them through
the provided hooks or, as here, through an explicitly invoked domain method.

## Import isolation

- The template imports `framework/` and `shared/` only.
- No platform SDK import anywhere in `src/`; `tests/proof_of_boundary/test_import_isolation.py` scans for it.

## Known platform interaction

The platform's personal-data filter rewrites matching shapes in `user_input` and
`validated_input` before any node runs, and its name heuristic treats two
consecutive title-case words as a personal name. Ordinary invoice field labels
match: `Supplier Code`, `Issue Date`, `Invoice No`. A label-anchored capture then
finds nothing, so `supplier_code` is routinely absent on English documents using
two-word labels.

The agent does not work around this — it reports it. `mandatory_fields_captured`
and `mandatory_fields_missing` state what the payload actually holds, and the
compliance note is derived from them rather than asserted alongside them. A
supplier name that arrives already redacted is reported as redacted rather than
certified as an extracted value.
