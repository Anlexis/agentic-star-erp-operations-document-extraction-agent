# ERP & Operations Document Extraction Agent

AI agent for extracting structured data from retail ERP and operations documents, built with Agentic Star.

> **Category**: Cat 2 (a domain-specific pipeline for one job-to-be-done)
> **Industry**: Retail
> **Template ID**: RET-C2-334

## Overview

Retail operations run on documents that arrive as text: supplier invoices,
purchase orders, delivery receipts, inventory reports. Each one carries the same
handful of facts — a document number, an issue date, a supplier code, a list of
SKUs with quantities and prices, a total — in whatever layout the sender uses.
Getting those facts into an ERP system is manual work, and Japan's
Denshi-Chobo-Hozon-Ho record-keeping rules mean the result has to be complete and
auditable rather than approximately right.

This agent reads one such document and returns the structured record: the
canonical fields, the line items normalised to a single schema, and a statement of
which record-keeping fields it was actually able to capture.

It also cross-checks what it read. A caller can send the SKU and supplier codes
their ERP already knows about, and the agent reports which line items matched,
which did not, and whether the line totals reconcile against the document total
within a tolerance the caller or the deployment chooses. Mismatches are reported,
never treated as failures — the agent's job is to tell you what the document says
and how it differs from your records, not to decide what to do about it.

Extraction is deterministic: the pipeline is rule- and pattern-based and invokes
no model, so the same document always yields the same record and there is nothing
to review for hallucination.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Sending a request

The document goes in `input`. The ERP reference data goes in `input_context` —
not in the request string, and not the other way round.

```json
{
  "input": "INVOICE\nInvoice No: INV-20260114001\nIssue Date: 2026-01-14\nSupplier Code: SUP-001\nSKU-48210 | 12 | pcs | 1500 | 18000\nTotal: 18000",
  "input_context": {
    "erp_reference": {
      "sku_master": ["SKU-48210", "SKU-48211"],
      "supplier_master": ["SUP-001"],
      "total_tolerance": 0.01
    }
  }
}
```

`erp_reference` is the only field the contract declares, and `sku_master`,
`supplier_master` and `total_tolerance` are the only fields inside it. Anything
else is refused rather than ignored. Reference codes are restricted to short
inert codes and capped in number; `total_tolerance` must be a finite number in
range. A refused request names the field that failed and never repeats its value.

Omitting `erp_reference` is fine — the agent then extracts the document and
reports the cross-checks as not performed, rather than as passed.

### A note on document layout

The platform rewrites personal-data shapes out of the request string before any
of this agent's code runs, and its name heuristic treats two consecutive
title-case words as a personal name. On an invoice that includes ordinary field
**labels**: `Supplier Code`, `Issue Date`, `Invoice No` are all masked, so a
label-anchored capture cannot find them.

In practice this means an English invoice using two-word labels will often come
back with `supplier_code` among `mandatory_fields_missing`. Single-word labels
(`Supplier:`) and Japanese labels (`取引先コード:`) are unaffected. The agent
reports which fields it captured rather than assuming, so the gap is visible in
the response instead of silently absent. The operation guide covers the
layouts that are and are not affected.

## Project Structure

```
src/          agent implementation (nodes, graphs, services, schemas)
tests/        unit, boundary and integration tests
config/       agent manifest and runtime parameters
deploy/       local deployment recipe and a smoke payload
docs/         design and operational documentation
```

`docs/` holds the design (`02_design.md`) and the test specification
(`03_test_spec.md`).

## Customising

1. Adjust `config/config.yaml` for your own environment: the reconciliation
   tolerance and the line-item cap both change what the agent returns.
2. Extend the document-type signatures and field anchors in
   `src/nodes/document_parse_node.py` for the layouts your suppliers actually
   send — that is where most adaptation work goes.
3. Extend the caller contract in `src/services/caller_contract.py` if your ERP
   supplies reference data this template does not model.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
