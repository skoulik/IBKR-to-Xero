"""Map in-scope statement sections to per-currency output transactions.

Every function here returns unrounded Decimal amounts; rounding and the
synthetic MTM/ROUNDING rows are the reconciler's job (reconcile.py), because
they only make sense once cash has been proven to add up.

Trade descriptions follow one grammar (simple sections stay verbatim):

    {+|-}{qty} {symbol} [({event})] price: {price} comm: {comm} [({qualifiers})]

Zero fields are omitted (an expiry shows no price, a free trade no comm).
The instrument type lives in the Reference column (STOCK/BOND/OPTION/FUTURE/
FX/FX-FEE), not the description. The "(incl. GST)" qualifier is appended by
reconcile.py once the embedded-GST cross-check has verified it.
"""

from __future__ import annotations

import collections
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

# Cash trades: Proceeds + Comm/Fee hits the row's currency directly.
# Bonds behave exactly like stocks (accrued interest and coupons arrive
# separately through the Interest section).
_CASH_TRADE_CATEGORIES = {"Stocks", "Bonds", "Equity and Index Options"}

# Instrument type tag written to the Reference column for cash trades.
_TRADE_REFERENCE = {"Stocks": "STOCK", "Bonds": "BOND", "Equity and Index Options": "OPTION"}

# Trades "Code" flags surfaced as lifecycle events in the description.
# Ordinary order mechanics (O/C open/close, P partial execution) stay
# silent; only codes with a confirmed meaning get a label.
_EVENT_CODES = (("A", "assigned"), ("Ep", "expired"))

# Sections that share the simple Currency/Date/Description/Amount shape,
# in the output order established by the reference examples.
_SIMPLE_SECTIONS = ("Fees", "Deposits & Withdrawals", "Dividends", "Withholding Tax", "Interest")

_CENT = Decimal("0.01")


def _parse_date(text: str, row: SectionRow) -> dt.date:
    # Trades carry "2026-06-26, 06:20:00"; other sections a bare "2026-06-26".
    date_part = text.split(",")[0].strip()
    try:
        return dt.date.fromisoformat(date_part)
    except ValueError as exc:
        raise StatementError(
            f"line {row.line_no}: cannot parse date {text!r} in section {row.section!r}"
        ) from exc


def _fmt_comm(value: Decimal) -> str:
    return fmt_number(value.quantize(_CENT))


def _signed_qty(row: SectionRow) -> str:
    qty = row["Quantity"].replace(",", "").strip()
    return qty if qty.startswith("-") else f"+{qty}"


def _labels(row: SectionRow) -> str:
    """The optional lifecycle parenthetical after the symbol: " (event)" or ""."""
    codes = {code.strip() for code in row.values.get("Code", "").split(";")}
    labels = [label for code, label in _EVENT_CODES if code in codes]
    return f" ({', '.join(labels)})" if labels else ""


def _trade_description(row: SectionRow, stamp_duty: Decimal) -> str:
    text = f"{_signed_qty(row)} {row['Symbol'].strip()}{_labels(row)}"
    price = row["T. Price"].replace(",", "").strip()
    if price and parse_money(price) != 0:
        text += f" price: {price}"
    comm = parse_money(row["Comm/Fee"])
    if comm != 0:
        text += f" comm: {_fmt_comm(comm)}"
        if stamp_duty:
            text += f" (incl. stamp duty {_fmt_comm(stamp_duty)})"
    return text


def _stamp_duties(statement: Statement) -> dict[tuple[str, str, str], Decimal]:
    """Nonzero Transaction Fees amounts keyed by their unique matching trade.

    The section is a per-trade breakdown of fees already embedded in trade
    Comm/Fee (reconcile.py cross-checks the totals against the Cash Report);
    here it only enriches the trade description. Zero rows carry nothing to
    display — and their Symbol column uses exchange option codes that match
    no Trades row — so they are skipped. A nonzero row that does not match
    exactly one trade would misattribute money => reject.
    """
    trade_count: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for row in statement.rows("Trades"):
        if _TRADE_DISCRIMINATOR_IS_TXN.get(row.values.get("DataDiscriminator", "")):
            trade_count[
                (row["Currency"], row["Symbol"].strip(), row["Date/Time"].strip())
            ] += 1
    duties: dict[tuple[str, str, str], Decimal] = {}
    for row in statement.rows("Transaction Fees"):
        currency = row.values.get("Currency", "")
        if not CURRENCY_RE.match(currency):
            continue  # "Total"/"Total in USD" aggregates
        amount = parse_money(row["Amount"])
        if amount == 0:
            continue
        key = (currency, row["Symbol"].strip(), row["Date/Time"].strip())
        if trade_count[key] != 1:
            raise StatementError(
                f"line {row.line_no}: Transaction Fees row for {row['Symbol']!r} at "
                f"{row['Date/Time']!r} ({amount}) matches {trade_count[key]} trades; "
                f"cannot attribute it to exactly one"
            )
        duties[key] = duties.get(key, Decimal(0)) + amount
    return duties


def _convert_forex_trade(row: SectionRow, out: dict[str, list[OutputRow]]) -> None:
    """A forex trade is a transfer between two currency cash accounts.

    The pair symbol is "BASE.QUOTE" (e.g. AUD.USD). The base account moves by
    Quantity, the quote account (= the row's Currency column) by Proceeds.
    Both legs share one description and are tagged FX so they can be matched
    across the two output files. IB charges the commission in USD regardless
    of the pair ("Comm in USD" column); it is emitted as a separate USD row
    tagged FX-FEE.
    """
    pair = row["Symbol"].strip()
    base, _, quote = pair.partition(".")
    if not (CURRENCY_RE.match(base) and quote == row["Currency"]):
        raise StatementError(
            f"line {row.line_no}: cannot understand forex pair {pair!r} "
            f"with currency {row['Currency']!r}"
        )
    date = _parse_date(row["Date/Time"], row)
    price = row["T. Price"].replace(",", "").strip()
    description = f"{_signed_qty(row)} {pair}{_labels(row)} price: {price}"
    out.setdefault(base, []).append(
        OutputRow(
            date=date,
            amount=parse_money(row["Quantity"]),
            description=description,
            reference="FX",
            source_section="Trades",
        )
    )
    out.setdefault(quote, []).append(
        OutputRow(
            date=date,
            amount=parse_money(row["Proceeds"]),
            description=description,
            reference="FX",
            source_section="Trades",
        )
    )
    comm_text = row["Comm in USD"].strip()
    comm = parse_money(comm_text) if comm_text else Decimal(0)
    if comm != 0:
        out.setdefault("USD", []).append(
            OutputRow(
                date=date,
                amount=comm,
                description=f"{pair} commission: {_fmt_comm(comm)}",
                reference="FX-FEE",
                source_section="Trades",
            )
        )


def _convert_trades(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    duties = _stamp_duties(statement)
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
        currency = row["Currency"]
        if not CURRENCY_RE.match(currency):
            raise StatementError(
                f"line {row.line_no}: trade Order row with non-currency {currency!r}"
            )
        if category == "Forex":
            _convert_forex_trade(row, out)
        elif category == "Futures":
            # Futures notional never touches cash: the P/L arrives via the
            # Cash Report's "Cash Settling MTM" (one synthetic row per
            # period); only the per-trade commission is real dated cash.
            comm = parse_money(row["Comm/Fee"])
            out.setdefault(currency, []).append(
                OutputRow(
                    date=_parse_date(row["Date/Time"], row),
                    amount=comm,
                    description=f"{_signed_qty(row)} {row['Symbol'].strip()}"
                    f"{_labels(row)} commission: {_fmt_comm(comm)}",
                    reference="FUTURE",
                    source_section="Trades",
                )
            )
        elif category in _CASH_TRADE_CATEGORIES:
            amount = parse_money(row["Proceeds"]) + parse_money(row["Comm/Fee"])
            duty = duties.get(
                (currency, row["Symbol"].strip(), row["Date/Time"].strip()), Decimal(0)
            )
            out.setdefault(currency, []).append(
                OutputRow(
                    date=_parse_date(row["Date/Time"], row),
                    amount=amount,
                    description=_trade_description(row, duty),
                    reference=_TRADE_REFERENCE[category],
                    source_section="Trades",
                )
            )
        else:
            raise StatementError(
                f"line {row.line_no}: unsupported trade asset category {category!r} "
                f"(supported: {sorted(_CASH_TRADE_CATEGORIES | {'Forex', 'Futures'})})"
            )


def _convert_corporate_actions(statement: Statement, out: dict[str, list[OutputRow]]) -> None:
    """Corporate actions move shares and sometimes cash (mergers pay a
    residual). Every row is emitted with its cash Proceeds (usually 0, so
    splits and ISIN changes stay visible unless --skip-zero) and the
    statement's description verbatim, tagged CORP. The cash flows through
    the Cash Report's Trades (Sales)/(Purchase) components, so these rows
    are cross-checked as part of the trades bucket.
    """
    for row in statement.rows("Corporate Actions"):
        currency = row.values.get("Currency", "")
        if not CURRENCY_RE.match(currency):
            continue  # "Total"/"Total in USD" aggregates
        out.setdefault(currency, []).append(
            OutputRow(
                date=_parse_date(row["Date/Time"], row),
                amount=parse_money(row["Proceeds"]),
                description=row["Description"].strip(),
                reference="CORP",
                source_section="Corporate Actions",
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
    _convert_corporate_actions(statement, out)
    for section in _SIMPLE_SECTIONS:
        _convert_simple_section(statement, section, out)
    return out
