"""Canonical request fixtures, shared by the integration tests and the deploy payload.

`deploy/invoke_payload.json` is posted verbatim by the deployment smoke check, and
a test in this package asserts it equals BASE_REQUEST. That is the point of
keeping it here rather than writing the payload by hand: a payload invented for
the deploy and a fixture written for the tests drift apart silently, and the
deploy is the side with no assertions on it — a refused invoke still answers
HTTP 200 with `status: error`, so nothing downstream notices.
"""

# A realistic supplier invoice. Every field the parser looks for is present, the
# line totals reconcile against the document total exactly, and the invoice
# number carries a long digit run on purpose: it is the case the output mask used
# to destroy.
SAMPLE_INVOICE = "\n".join(
    [
        "INVOICE",
        "Invoice No: INV-20260114001",
        "Issue Date: 2026-01-14",
        "Due Date: 2026-02-14",
        "Supplier Code: SUP-001",
        "SKU-48210 | 12 | pcs | 1500 | 18000",
        "SKU-48211 | 3 | pcs | 2400 | 7200",
        "Total: 25200",
    ]
)

#: The document identifier the pipeline must return intact.
SAMPLE_DOC_ID = "INV-20260114001"

#: The SKUs the sample document carries, in order.
SAMPLE_SKUS = ["SKU-48210", "SKU-48211"]

#: The reconciling document total.
SAMPLE_TOTAL = 25200.0

BASE_REQUEST = {
    "input": SAMPLE_INVOICE,
    "session_id": "deploy-smoke-001",
    "input_context": {
        "erp_reference": {
            "sku_master": SAMPLE_SKUS,
            "supplier_master": ["SUP-001"],
        }
    },
}
