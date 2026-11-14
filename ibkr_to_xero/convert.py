"""Map a Statement's records to per-currency output transactions.

Every function here returns unrounded Decimal amounts; rounding and the
synthetic MTM/ROUNDING rows are the reconciler's job (reconcile.py), because
they only make sense once cash has been proven to add up.

Trade descriptions follow one grammar (cash transactions stay verbatim):

    {+|-}{qty} {symbol} [({event})] price: {price} comm: {comm} [({qualifiers})]

Zero fields are omitted (an expiry shows no price, a free trade no comm).
The instrument type lives in the Reference column (STOCK/BOND/OPTION/FUTURE/
FX/FX-FEE), not the description. The "(incl. GST)" qualifier is appended by
reconcile.py once the embedded-GST cross-check has verified it.

Implements: AD-016
"""

from __future__ import annotations

import collections
import datetime as dt
from decimal import Decimal

from .model import (
    CURRENCY_RE,
    AssetClass,
    CashKind,
    OutputRow,
    Statement,
    StatementError,
    TaxKind,
    Trade,
    fmt_number,
)

# Trade lifecycle codes surfaced as events in the description. Ordinary order
# mechanics (O/C open/close, P partial execution) stay silent; only codes with
# a confirmed meaning get a label.
_EVENT_CODES = (("A", "assigned"), ("Ep", "expired"))

_CENT = Decimal("0.01")


def _fmt_comm(value: Decimal) -> str:
    return fmt_number(value.quantize(_CENT))


def _fmt_plain(value: Decimal) -> str:
    """A source number as the source wrote it (less any thousands separators)."""
    return format(value, "f")


def _fmt_when(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%d, %H:%M:%S")


def _signed_qty(trade: Trade) -> str:
    qty = _fmt_plain(trade.quantity)
    return qty if qty.startswith("-") else f"+{qty}"


def _labels(trade: Trade) -> str:
    """The optional lifecycle parenthetical after the symbol: " (event)" or ""."""
    labels = [label for code, label in _EVENT_CODES if code in trade.codes]
    return f" ({', '.join(labels)})" if labels else ""


def _trade_description(trade: Trade, stamp_duty: Decimal) -> str:
    # Implements: AD-041
    text = f"{_signed_qty(trade)} {trade.symbol}{_labels(trade)}"
    if trade.price is not None and trade.price != 0:
        text += f" price: {_fmt_plain(trade.price)}"
    if trade.commission != 0:
        text += f" comm: {_fmt_comm(trade.commission)}"
        if stamp_duty:
            text += f" (incl. stamp duty {_fmt_comm(stamp_duty)})"
    return text


def _stamp_duties(statement: Statement) -> dict[tuple[str, str, dt.datetime], Decimal]:
    """Nonzero stamp duty keyed by its unique matching trade.

    Stamp duty is already embedded in the trade's commission (reconcile.py
    cross-checks the totals against the Cash Report); here it only enriches
    the trade description. Zero items carry nothing to display — and in the
    Activity Statement their symbol is an exchange option code that matches
    no trade — so they are skipped. A nonzero item that does not match exactly
    one trade would misattribute money => reject.

    Implements: AD-045
    """
    trade_count: collections.Counter[tuple[str, str, dt.datetime]] = collections.Counter(
        (trade.currency, trade.symbol, trade.when) for trade in statement.trades
    )
    duties: dict[tuple[str, str, dt.datetime], Decimal] = {}
    for item in statement.tax_items:
        if item.kind is not TaxKind.STAMP_DUTY or item.amount == 0:
            continue
        key = (item.currency, item.symbol, item.when)
        if trade_count[key] != 1:
            raise StatementError(
                f"{item.where}: Transaction Fees row for {item.symbol!r} at "
                f"{_fmt_when(item.when)!r} ({item.amount}) matches {trade_count[key]} "
                f"trades; cannot attribute it to exactly one"
            )
        duties[key] = duties.get(key, Decimal(0)) + item.amount
    return duties


def _convert_forex_trade(trade: Trade, out: dict[str, list[OutputRow]]) -> None:
    """A forex trade is a transfer between two currency cash accounts.

    The pair symbol is "BASE.QUOTE" (e.g. AUD.USD). The base account moves by
    the quantity, the quote account (= the trade's currency) by the proceeds.
    Both legs share one description and are tagged FX so they can be matched
    across the two output files. The commission is charged in its own currency
    (USD in the Activity Statement) and emitted there as a row tagged FX-FEE.

    Implements: AD-044
    """
    pair = trade.symbol
    base, _, quote = pair.partition(".")
    if not (CURRENCY_RE.match(base) and quote == trade.currency):
        raise StatementError(
            f"{trade.where}: cannot understand forex pair {pair!r} "
            f"with currency {trade.currency!r}"
        )
    date = trade.when.date()
    price = _fmt_plain(trade.price) if trade.price is not None else ""
    description = f"{_signed_qty(trade)} {pair}{_labels(trade)} price: {price}"
    for currency, amount in ((base, trade.quantity), (quote, trade.proceeds)):
        out.setdefault(currency, []).append(
            OutputRow(
                date=date,
                amount=amount,
                description=description,
                reference=AssetClass.FOREX.value,
                source_section="Trades",
            )
        )
    if trade.commission != 0:
        out.setdefault(trade.commission_currency, []).append(
            OutputRow(
                date=date,
                amount=trade.commission,
                description=f"{pair} commission: {_fmt_comm(trade.commission)}",
                reference="FX-FEE",
                source_section="Trades",
            )
        )


def _convert_trades(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    duties = _stamp_duties(statement)
    for trade in statement.trades:
        if trade.asset_class is AssetClass.FOREX:
            _convert_forex_trade(trade, out)
        elif trade.asset_class is AssetClass.FUTURE:
            # Implements: AD-043
            # Futures notional never touches cash: the P/L arrives via the
            # Cash Report's "Cash Settling MTM" (one synthetic row per
            # period); only the per-trade commission is real dated cash.
            out.setdefault(trade.currency, []).append(
                OutputRow(
                    date=trade.when.date(),
                    amount=trade.commission,
                    description=f"{_signed_qty(trade)} {trade.symbol}"
                    f"{_labels(trade)} commission: {_fmt_comm(trade.commission)}",
                    reference=trade.asset_class.value,
                    source_section="Trades",
                )
            )
        else:
            # Implements: AD-042
            # Cash trades: proceeds + commission hit the trade's currency.
            duty = duties.get((trade.currency, trade.symbol, trade.when), Decimal(0))
            out.setdefault(trade.currency, []).append(
                OutputRow(
                    date=trade.when.date(),
                    amount=trade.proceeds + trade.commission,
                    description=_trade_description(trade, duty),
                    reference=trade.asset_class.value,
                    source_section="Trades",
                )
            )


def _convert_corporate_actions(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    """Corporate actions move shares and sometimes cash (mergers pay a
    residual). Every row is emitted with its cash proceeds (usually 0, so
    splits and ISIN changes stay visible unless --skip-zero) and the source
    description verbatim, tagged CORP. The cash flows through the Cash
    Report's Trades (Sales)/(Purchase) components, so these rows are
    cross-checked as part of the trades bucket.

    Implements: AD-046 (with fee_gst and _convert_cash_txns)
    """
    for action in statement.corporate_actions:
        out.setdefault(action.currency, []).append(
            OutputRow(
                date=action.when.date(),
                amount=action.proceeds,
                description=action.description,
                reference="CORP",
                source_section="Corporate Actions",
            )
        )


def fee_gst(statement: Statement) -> dict[str, Decimal]:
    """GST linked to a fee in this statement: fee id -> GST amount (plan D19).

    Such GST is folded into the fee's row. GST naming no record here is left
    alone — it is the GST inside trade commissions, which reconcile.py
    verifies — but GST naming a cash transaction that is not a fee would be
    folded into the wrong bucket => reject.
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


def _convert_cash_txns(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    gst = fee_gst(statement)
    for kind in CashKind:  # declaration order is the output order
        for txn in statement.cash_txns:
            if txn.kind is not kind:
                continue
            amount, description = txn.amount, txn.description
            if txn.id in gst:
                amount += gst[txn.id]
                description += f" (incl. GST {abs(gst[txn.id]).quantize(_CENT)})"
            out.setdefault(txn.currency, []).append(
                OutputRow(
                    date=txn.date,
                    amount=amount,
                    description=description,
                    source_section=kind.value,
                )
            )


def convert(statement: Statement) -> dict[str, list[OutputRow]]:
    """Produce per-currency transaction rows from the statement's records.

    Row order: trades, corporate actions, then the cash transactions by kind,
    each in source order — matching the reference outputs in examples/.

    Implements: AD-040
    """
    out: dict[str, list[OutputRow]] = {}
    _convert_trades(statement, out)
    _convert_corporate_actions(statement, out)
    _convert_cash_txns(statement, out)
    return out
