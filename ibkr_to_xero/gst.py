"""GST: the rate the checks use, and the GST a source links to its fees.

Both convert.py (which folds such GST into the fee's row) and reconcile.py
(which checks it with the fees) need the links, so they live here rather than
in either (plan D19). The rate is a ledger setting (D57): Australia's 10% by
default, and for the one-shot conversion of a downloaded statement.

Implements: AD-106
"""

from __future__ import annotations

from decimal import Decimal

from .model import CashKind, Statement, StatementError, TaxKind, fmt_number

# Australia's GST rate: 10% of commissions on GST-liable exchanges, and of fees
# for services IBKR Australia itself supplies.
DEFAULT_RATE = Decimal("0.1")


def percent(rate: Decimal) -> str:
    """The rate as messages show it: Decimal("0.1") -> "10%"."""
    return f"{fmt_number(rate * 100)}%"


def fee_gst(statement: Statement) -> dict[str, Decimal]:
    """GST linked to a fee in this statement: fee id -> GST amount (plan D19).

    Such GST is folded into the fee's row. GST naming no record here is left
    alone — it is the GST inside trade commissions, which reconcile.py
    verifies — but GST naming a cash transaction that is not a fee would be
    folded into the wrong bucket => reject.

    Implements: AD-105, AD-011
    """
    txns = {txn.id: txn for txn in statement.cash_txns}
    linked: dict[str, Decimal] = {}
    for item in statement.tax_items:
        if item.kind is not TaxKind.GST or item.taxed_id not in txns:
            continue
        txn = txns[item.taxed_id]
        if txn.kind is not CashKind.FEE or txn.currency != item.currency:
            raise StatementError(
                f"{item.where}: GST on {txn.where} ({txn.kind.value}, {txn.currency}); "
                f"only GST on fees in their own currency is supported"
            )
        linked[txn.id] = linked.get(txn.id, Decimal(0)) + item.amount
    return linked
