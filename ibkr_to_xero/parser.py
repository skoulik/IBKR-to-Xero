"""Parse an IB Activity Statement CSV into the typed Statement model.

File format: every row is `SectionName,RowKind,...` where RowKind is `Header`
or `Data`. A Header row defines the column names for the Data rows that follow
it; a section may re-emit Header rows (e.g. Trades does so per asset category),
so we keep a running "current header" per section. The first cell of the file
carries a UTF-8 BOM (handled by utf-8-sig).

Two stages: parse_sections() reads the generic section structure, and
parse_statement() maps the in-scope sections onto the model (model.py) that
convert.py and reconcile.py work on.

Implements: AD-014
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .model import (
    CURRENCY_RE,
    AssetClass,
    CashKind,
    CashTxn,
    CorporateAction,
    SectionRow,
    Statement,
    StatementError,
    TaxItem,
    TaxKind,
    Trade,
    parse_money,
)

_ROW_KINDS = {"Header", "Data"}

# Aggregate/footnote row kinds IB emits alongside Header/Data. They never hold
# transactions (per-currency sums are cross-checked via the Cash Report), so
# they are skipped; any other unknown kind still rejects the file.
_SKIPPED_ROW_KINDS = {"Total", "SubTotal", "Total in USD", "Notes"}

# Statement section "Period" field: "June 1, 2026 - June 30, 2026"
# (single-day statements use just "June 1, 2026")
_PERIOD_SEP = re.compile(r"\s+-\s+")

# Trades.DataDiscriminator values: which rows are cash transactions,
# which are informational/aggregate. Anything unlisted => reject; execution-level
# "Trade" rows with a message of their own (D58): beside an Order row they would
# count the same trade twice.
_TRADE_DISCRIMINATOR_IS_TXN = {
    "Order": True,
    "ClosedLot": False,
    "SubTotal": False,
    "Total": False,
}

# Trades "Asset Category" -> instrument type. Anything unlisted => reject.
# Bonds behave exactly like stocks (accrued interest and coupons arrive
# separately through the Interest section).
_ASSET_CLASS = {
    "Stocks": AssetClass.STOCK,
    "Bonds": AssetClass.BOND,
    "Equity and Index Options": AssetClass.OPTION,
    "Futures": AssetClass.FUTURE,
    "Forex": AssetClass.FOREX,
}

# Sections that share the simple Currency/Date/Description/Amount shape.
# Commission Adjustments holds commissions IB priced after the trade was
# reported: the trade's own Comm/Fee is 0 and the money arrives as a separate
# dated row.
_SIMPLE_SECTIONS = {kind.value: kind for kind in CashKind}

# Forex rows carry their commission in a column named after the currency it is
# charged in ("Comm in USD"); the header, not an assumption, says which.
_FOREX_COMMISSION_COLUMN = re.compile(r"^Comm in ([A-Z]{3})$")


@dataclass
class RawStatement:
    """The generic section structure of an Activity Statement CSV."""

    sections: dict[str, list[SectionRow]]
    period_start: dt.date
    period_end: dt.date
    account: str = ""
    base_currency: str = ""

    def rows(self, section: str) -> list[SectionRow]:
        return self.sections.get(section, [])


def _parse_period(text: str) -> tuple[dt.date, dt.date]:
    parts = _PERIOD_SEP.split(text.strip())
    if len(parts) not in (1, 2):
        raise StatementError(f"cannot parse statement period {text!r}")
    try:
        dates = [dt.datetime.strptime(p.strip(), "%B %d, %Y").date() for p in parts]
    except ValueError as exc:
        raise StatementError(f"cannot parse statement period {text!r}") from exc
    return dates[0], dates[-1]


def _field_map(rows: list[SectionRow]) -> dict[str, str]:
    """Fold Field Name / Field Value rows into a dict."""
    return {r["Field Name"]: r["Field Value"] for r in rows}


def parse_sections(path: str | Path) -> RawStatement:
    """Read an IB Activity Statement CSV file into its sections.

    Raises StatementError on any structural surprise: better to reject the
    input than to silently mis-read it.

    Implements: AD-029, AD-030
    """
    path = Path(path)
    sections: dict[str, list[SectionRow]] = {}
    current_headers: dict[str, list[str]] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        for line_no, row in enumerate(csv.reader(fh), start=1):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) < 2:
                raise StatementError(f"line {line_no}: expected 'Section,Kind,...', got {row!r}")
            section, kind, cells = row[0], row[1], row[2:]
            if not kind or kind in _SKIPPED_ROW_KINDS:
                # Aggregate/footnote rows, and one-line summary sections
                # ("Total P/L for Statement Period") with no marker at all:
                # no transactions here, skip rather than reject.
                continue
            if kind not in _ROW_KINDS:
                raise StatementError(
                    f"line {line_no}: unknown row kind {kind!r} in section {section!r} "
                    "(expected Header or Data)"
                )
            if kind == "Header":
                current_headers[section] = cells
                sections.setdefault(section, [])
                continue
            headers = current_headers.get(section)
            if headers is None:
                raise StatementError(
                    f"line {line_no}: Data row in section {section!r} before any Header row"
                )
            if len(cells) > len(headers):
                # Tolerate only trailing empty cells (IB emits trailing commas).
                extra = cells[len(headers):]
                if any(cell.strip() for cell in extra):
                    raise StatementError(
                        f"line {line_no}: section {section!r} row has {len(cells)} cells "
                        f"but header has {len(headers)} columns; extra data: {extra!r}"
                    )
                cells = cells[: len(headers)]
            elif len(cells) < len(headers):
                cells = cells + [""] * (len(headers) - len(cells))
            values = dict(zip(headers, cells))
            sections.setdefault(section, []).append(SectionRow(section, values, line_no))

    meta = _field_map(sections.get("Statement", []))
    period = meta.get("Period")
    if not period:
        raise StatementError("statement has no 'Statement' section with a Period field")
    period_start, period_end = _parse_period(period)

    account_info = _field_map(sections.get("Account Information", []))

    return RawStatement(
        sections=sections,
        period_start=period_start,
        period_end=period_end,
        account=account_info.get("Account", ""),
        base_currency=account_info.get("Base Currency", ""),
    )


def parse_statement(path: str | Path) -> Statement:
    """Read a statement file into the typed Statement model.

    An Activity Statement CSV or, recognised by its content, a Flex report
    (XML; flex.py).

    Implements: AD-002, AD-008, AD-028
    """
    from .flex import is_flex_report, parse_flex  # flex.py never imports this module

    if is_flex_report(path):
        return parse_flex(path)
    return to_statement(parse_sections(path))


# --- mapping the sections onto the model -----------------------------------


def _parse_when(text: str, row: SectionRow) -> dt.datetime:
    """'2026-06-26, 06:20:00' (Trades and friends) or a bare '2026-06-26'."""
    date_part, _, time_part = text.partition(",")
    try:
        date = dt.date.fromisoformat(date_part.strip())
        time = (
            dt.time.fromisoformat(time_part.strip()) if time_part.strip() else dt.time()
        )
    except ValueError as exc:
        raise StatementError(
            f"line {row.line_no}: cannot parse date {text!r} in section {row.section!r}"
        ) from exc
    return dt.datetime.combine(date, time)


def _optional_money(text: str) -> Decimal | None:
    return parse_money(text) if text.strip() else None


def _source(row: SectionRow) -> dict:
    return {"id": str(row.line_no), "where": f"line {row.line_no}", "report_date": None}


def _trades(raw: RawStatement) -> list[Trade]:
    # Implements: AD-031
    trades = []
    for row in raw.rows("Trades"):
        discriminator = row["DataDiscriminator"]
        is_txn = _TRADE_DISCRIMINATOR_IS_TXN.get(discriminator)
        if discriminator == "Trade":
            raise StatementError(
                f"line {row.line_no}: the statement lists trades by execution ('Trade' "
                "rows); export it with trades at order level"
            )
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
        asset_class = _ASSET_CLASS.get(category)
        if asset_class is None:
            raise StatementError(
                f"line {row.line_no}: unsupported trade asset category {category!r} "
                f"(supported: {sorted(_ASSET_CLASS)})"
            )
        if asset_class is AssetClass.FOREX:
            columns = [
                (column, match.group(1))
                for column in row.values
                if (match := _FOREX_COMMISSION_COLUMN.match(column))
            ]
            if len(columns) != 1:
                raise StatementError(
                    f"line {row.line_no}: forex trade needs exactly one 'Comm in <currency>' "
                    f"column, found {[c for c, _ in columns]}"
                )
            (column, commission_currency), = columns
            commission = _optional_money(row[column]) or Decimal(0)
        else:
            commission = parse_money(row["Comm/Fee"])
            commission_currency = currency
        trades.append(
            Trade(
                **_source(row),
                when=_parse_when(row["Date/Time"], row),
                currency=currency,
                asset_class=asset_class,
                symbol=row["Symbol"].strip(),
                quantity=parse_money(row["Quantity"]),
                price=_optional_money(row["T. Price"]),
                # Futures notional never touches cash: their header has
                # Notional Value instead of Proceeds. Every other class needs it.
                proceeds=(
                    _optional_money(row.values.get("Proceeds", ""))
                    if asset_class is AssetClass.FUTURE
                    else parse_money(row["Proceeds"])
                ),
                commission=commission,
                commission_currency=commission_currency,
                codes=frozenset(code.strip() for code in row.values.get("Code", "").split(";")),
            )
        )
    return trades


def _corporate_actions(raw: RawStatement) -> list[CorporateAction]:
    return [
        CorporateAction(
            **_source(row),
            when=_parse_when(row["Date/Time"], row),
            currency=row.values.get("Currency", ""),
            description=row["Description"].strip(),
            proceeds=parse_money(row["Proceeds"]),
        )
        for row in raw.rows("Corporate Actions")
        if CURRENCY_RE.match(row.values.get("Currency", ""))  # skip "Total" aggregates
    ]


def _cash_txns(raw: RawStatement) -> list[CashTxn]:
    # Implements: AD-032 (with _corporate_actions and _tax_items)
    txns = []
    for section, kind in _SIMPLE_SECTIONS.items():
        date_field = "Settle Date" if kind is CashKind.DEPOSIT_WITHDRAWAL else "Date"
        for row in raw.rows(section):
            currency = row["Currency"]
            if not CURRENCY_RE.match(currency):
                continue  # "Total"/"Total in USD" aggregates; cross-checked in reconcile.py
            txns.append(
                CashTxn(
                    **_source(row),
                    date=_parse_when(row[date_field], row).date(),
                    currency=currency,
                    kind=kind,
                    description=row["Description"].strip(),
                    amount=parse_money(row["Amount"]),
                )
            )
    return txns


def _tax_items(raw: RawStatement) -> list[TaxItem]:
    """The Transaction Fees section: a per-trade breakdown of taxes (stamp duty)
    already embedded in trade Comm/Fee — never extra cash."""
    return [
        TaxItem(
            **_source(row),
            when=_parse_when(row["Date/Time"], row),
            currency=row.values.get("Currency", ""),
            kind=TaxKind.STAMP_DUTY,
            symbol=row["Symbol"].strip(),
            amount=parse_money(row["Amount"]),
        )
        for row in raw.rows("Transaction Fees")
        if CURRENCY_RE.match(row.values.get("Currency", ""))  # skip "Total" aggregates
    ]


def _cash_report(raw: RawStatement) -> dict[str, dict[str, Decimal]]:
    """Per-currency component amounts from the Cash Report section.

    The Cash Report is organised in blocks, each opened by a "Starting Cash"
    row: first the base-currency summary, then one block per actual currency.
    Rows must be bucketed by their *block*, not their own Currency cell — the
    base block can contain rows whose Currency cell names a real currency
    (e.g. GST charged in AUD shown converted to a base-USD line).

    Implements: AD-033
    """
    report: dict[str, dict[str, Decimal]] = {}
    rows = raw.rows("Cash Report")
    if not rows:
        raise StatementError("statement has no Cash Report section; cannot reconcile")
    block_currency: str | None = None
    for row in rows:
        component = row["Currency Summary"]
        if component == "Starting Cash":
            block_currency = row["Currency"]
            if CURRENCY_RE.match(block_currency) and block_currency in report:
                raise StatementError(
                    f"line {row.line_no}: duplicate Cash Report block for {block_currency}"
                )
        if block_currency is None:
            raise StatementError(
                f"line {row.line_no}: Cash Report row before any 'Starting Cash' block"
            )
        if not CURRENCY_RE.match(block_currency):
            continue  # base-currency summary block: out of scope
        amount = parse_money(row["Total"])
        per_ccy = report.setdefault(block_currency, {})
        if component in per_ccy:
            raise StatementError(
                f"line {row.line_no}: duplicate Cash Report component "
                f"{component!r} for {block_currency}"
            )
        per_ccy[component] = amount
    return report


def to_statement(raw: RawStatement) -> Statement:
    """Map an Activity Statement's in-scope sections onto the typed model."""
    return Statement(
        period_start=raw.period_start,
        period_end=raw.period_end,
        account=raw.account,
        base_currency=raw.base_currency,
        trades=_trades(raw),
        corporate_actions=_corporate_actions(raw),
        cash_txns=_cash_txns(raw),
        tax_items=_tax_items(raw),
        cash_report=_cash_report(raw),
    )
