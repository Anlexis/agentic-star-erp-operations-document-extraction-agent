# Boundary: what the residual account-number mask rewrites, and what it must not.
#
# The rule this gate enforces is "an account-number-like digit run never reaches
# the delivered payload". The rule it must NOT enforce is "no long digit run
# reaches the delivered payload" — those differ on exactly the values this agent
# exists to produce, and the difference is not academic: a bare `\b\d{7,}\b` rule
# treats a hyphen as a word boundary, so a realistic invoice number was delivered
# as `INV-[REDACTED]`.
#
# Both directions are probed. One direction alone cannot distinguish a working
# gate from a gate that rewrites everything, or from one that rewrites nothing.

import pytest

from src.nodes.output_validate_node import _mask_residual_pii


# ── Identifiers survive ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "INV-20260114001",
        "INV-STOCK-20260114001",
        "PO-20260114001",
        "DN-20260114001",
        "SKU-48210001234",
        "SUP-000000001",
        "sku_482100012345",
        "Invoice INV-20260114001 issued",
    ],
    ids=[
        "invoice",
        "inventory",
        "purchase-order",
        "delivery-note",
        "long-sku",
        "supplier",
        "underscore-alphabet",
        "in-a-sentence",
    ],
)
def test_document_identifiers_are_delivered_intact(value):
    """A token containing a letter is an identifier, not an account number."""
    assert _mask_residual_pii(value) == value


@pytest.mark.parametrize(
    "value",
    ["2026-01-14", "90d", "25200", "12", "1500.00", "STAR 2026", "合計 25200"],
    ids=["date", "horizon", "total", "qty", "decimal", "acronym-year", "jp-total"],
)
def test_structural_and_domain_values_are_byte_identical(value):
    """Short runs, dates, quantities and amounts are not account numbers."""
    assert _mask_residual_pii(value) == value


# ── Account-number shapes are still masked ───────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1234567890", "[REDACTED]"),
        ("Account 1234567890", "Account [REDACTED]"),
        ("口座番号 1234567890", "口座番号 [REDACTED]"),
        ("1234-5678-9012345678", "1234-5678-[REDACTED]"),
        ("transfer to 98765432 today", "transfer to [REDACTED] today"),
    ],
    ids=["bare", "labelled", "japanese", "hyphen-grouped", "in-a-sentence"],
)
def test_bare_long_digit_runs_are_masked(value, expected):
    """The shape an account number actually takes, in Latin and Japanese text."""
    assert _mask_residual_pii(value) == expected


def test_a_six_digit_run_is_below_the_threshold():
    assert _mask_residual_pii("123456") == "123456"


# ── Structure ────────────────────────────────────────────────────────────────


def test_the_scan_reaches_nested_values():
    """A payload nests line items and validation results.

    A scan that only reached top-level strings would leave everything this agent
    actually produces unexamined — the nested case is the realistic one, and the
    top-level case above is the control that proves the scan itself works.
    """
    payload = {"erp_validation": {"sku_matches": [{"note": "acct 1234567890"}]}}
    assert _mask_residual_pii(payload) == {"erp_validation": {"sku_matches": [{"note": "acct [REDACTED]"}]}}


def test_mapping_keys_are_scanned_too():
    assert _mask_residual_pii({"1234567890": "x"}) == {"[REDACTED]": "x"}


def test_non_string_leaves_are_untouched():
    """Quantities and amounts are numbers; rewriting them would corrupt the answer."""
    payload = {"quantity": 12.0, "total": 25200, "ok": True, "none": None}
    assert _mask_residual_pii(payload) == payload
