"""Map in-scope statement sections to per-currency output transactions.

Every function here returns unrounded Decimal amounts; rounding and the
synthetic MTM/ROUNDING rows are the reconciler's job (reconcile.py), because
they only make sense once cash has been proven to add up.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .model import (
    CURRENCY_RE,
    OutputRow,
    SectionRow,
    Statement,
    StatementError,
    fmt_number,
    parse_money,
)

# Trades.DataDiscriminator values: which rows are cash transactions,
# which are informational/aggregate. Anything unlisted => reject.
_TRADE_DISCRIMINATOR_IS_TXN = {
    "Order": True,
    "Trade": True,  # some IB exports list individual executions as "Trade"
    "ClosedLot": False,
    "SubTotal": False,
    "Total": False,
}

_SUPPORTED_TRADE_CATEGORIES = {"Stocks", "Equity and Index Options"}

# Sections that share the simple Currency/Date/Description/Amount shape,
# in the output order established by the reference examples.
_SIMPLE_SECTIONS = ("Fees", "Deposits & Withdrawals", "Dividends", "Withholding Tax", "Interest")


def _parse_date(text: str, row: SectionRow) -> dt.date:
    # Trades carry "2026-06-26, 06:20:00"; other sections a bare "2026-06-26".
    date_part = text.split(",")[0].strip()
    try:
        return dt.date.fromisoformat(date_part)
    except ValueError as exc:
        raise StatementError(
            f"line {row.line_no}: cannot parse date {text!r} in section {row.section!r}"
        ) from exc


def _trade_description(row: SectionRow) -> str:
    qty = row["Quantity"].replace(",", "").strip()
    symbol = row["Symbol"].strip()
    category = row["Asset Category"]
    if category == "Stocks":
        price = row["T. Price"].replace(",", "").strip()
        comm = fmt_number(parse_money(row["Comm/Fee"]).quantize(Decimal("0.01")))
        return f"{qty} {symbol} price: {price} comm: {comm}"
    # Options: "-1xCSL 16JUL26 130 C"
    return f"{qty}x{symbol}"


def _convert_trades(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    for row in statement.rows("Trades"):
        discriminator = row["DataDiscriminator"]
        is_txn = _TRADE_DISCRIMINATOR_IS_TXN.get(discriminator)
        if is_txn is None:
            raise StatementError(
                f"line {row.line_no}: unknown Trades DataDiscriminator {discriminator!r}"
            )
        if not is_txn:
            continue
        category = row["Asset Category"]
        if category not in _SUPPORTED_TRADE_CATEGORIES:
            raise StatementError(
                f"line {row.line_no}: unsupported trade asset category {category!r} "
                f"(supported: {sorted(_SUPPORTED_TRADE_CATEGORIES)})"
            )
        currency = row["Currency"]
        if not CURRENCY_RE.match(currency):
            raise StatementError(
                f"line {row.line_no}: trade Order row with non-currency {currency!r}"
            )
        amount = parse_money(row["Proceeds"]) + parse_money(row["Comm/Fee"])
        out.setdefault(currency, []).append(
            OutputRow(
                date=_parse_date(row["Date/Time"], row),
                amount=amount,
                description=_trade_description(row),
                source_section="Trades",
            )
        )


def _convert_simple_section(
    statement: Statement, section: str, out: dict[str, list[OutputRow]]
) -> None:
    for row in statement.rows(section):
        currency = row["Currency"]
        if not CURRENCY_RE.match(currency):
            continue  # "Total"/"Total in USD" aggregates; cross-checked in reconcile.py
        date_field = "Settle Date" if section == "Deposits & Withdrawals" else "Date"
        out.setdefault(currency, []).append(
            OutputRow(
                date=_parse_date(row[date_field], row),
                amount=parse_money(row["Amount"]),
                description=row["Description"].strip(),
                source_section=section,
            )
        )


def convert(statement: Statement) -> dict[str, list[OutputRow]]:
    """Produce per-currency transaction rows from the in-scope sections.

    Row order mirrors the statement (Trades first, then the cash sections),
    matching the reference outputs in examples/.
    """
    out: dict[str, list[OutputRow]] = {}
    _convert_trades(statement, out)
    for section in _SIMPLE_SECTIONS:
        _convert_simple_section(statement, section, out)
    return out
