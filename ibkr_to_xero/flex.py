"""Parse an IBKR Activity Flex Query report (XML) into the typed Statement model.

The second front end beside parser.py (plan D18). A Flex report is configured
by the user, so it is validated before it is read (plan D16): SECTIONS below
is the single table of what the converter needs — sections, the rows read from
each, and the attributes those rows must carry — and drives both the check
and the mapping. Anything missing rejects, naming the section, field or option
to tick in Client Portal. What the XML cannot show (a section with no rows in
the period carries no attributes; an unticked cash-transaction type leaves no
trace) is caught by reconciliation, since the Cash Report still counts the cash.

Rows are selected by IBKR's report date (plan I1): every row belongs to the
period holding its reportDate, whatever its own date.

Implements: AD-015
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .model import (
    CURRENCY_RE,
    AssetClass,
    CashKind,
    CashTxn,
    CorporateAction,
    Statement,
    StatementError,
    TaxItem,
    TaxKind,
    Trade,
)

# Flex CashReportCurrency attribute -> Cash Report component, named as in the
# Activity Statement (the model's vocabulary; reconcile.py maps them to buckets).
CASH_COMPONENTS = {
    "startingCash": "Starting Cash",
    "endingCash": "Ending Cash",
    "endingSettledCash": "Ending Settled Cash",
    "commissions": "Commissions",
    "salesTax": "GST",
    "transactionTax": "Transaction Fees",
    "netTradesSales": "Trades (Sales)",
    "netTradesPurchases": "Trades (Purchase)",
    "deposits": "Deposits",
    "withdrawals": "Withdrawals",
    "accountTransfers": "Account Transfers",
    "dividends": "Dividends",
    "paymentInLieu": "Payment In Lieu of Dividends",
    "withholdingTax": "Withholding Tax",
    "brokerInterest": "Broker Interest Paid and Received",
    "bondInterest": "Bond Interest Paid and Received",
    "otherFees": "Other Fees",
    "cashSettlingMtm": "Cash Settling MTM",
    "fxTranslationGainLoss": "Cash FX Translation Gain/Loss",
    # Stock Yield Enhancement Program collateral lines: informational.
    "slbNetCash": "Net Cash Balance",
    "slbNetSettledCash": "Net Settled Cash Balance",
    "slbStartingCashCollateral": "Starting Collateral Value",
    "slbEndingCashCollateral": "Ending Collateral Value",
    "slbNetSecuritiesLentActivity": "Net Securities Lent Activity",
}
# CashReportCurrency attributes that are not components: identification, a
# sum of two components, per-segment splits (…Com/…Sec) and running totals
# that long periods add (…MTD/…YTD). Any other attribute is a component; an
# unknown one is passed through under its Flex name, and reconcile.py rejects
# it if nonzero.
_CASH_REPORT_NOT_COMPONENTS = {
    "accountId", "acctAlias", "model", "currency", "fromDate", "toDate",
    "levelOfDetail", "depositWithdrawals",
}
_CASH_REPORT_SPLITS = ("Com", "Sec", "MTD", "YTD")

_ASSET_CLASS = {
    "STK": AssetClass.STOCK,
    "OPT": AssetClass.OPTION,
    "FUT": AssetClass.FUTURE,
    "BOND": AssetClass.BOND,
    "CASH": AssetClass.FOREX,
}

_CASH_KIND = {
    "Other Fees": CashKind.FEE,
    "Deposits/Withdrawals": CashKind.DEPOSIT_WITHDRAWAL,
    "Dividends": CashKind.DIVIDEND,
    "Payment In Lieu Of Dividends": CashKind.DIVIDEND,
    "Withholding Tax": CashKind.WITHHOLDING,
    "Broker Interest Paid": CashKind.INTEREST,
    "Broker Interest Received": CashKind.INTEREST,
    "Bond Interest Paid": CashKind.INTEREST,
    "Bond Interest Received": CashKind.INTEREST,
    "Commission Adjustments": CashKind.COMMISSION_ADJUSTMENT,
}

_DIRECTION = {"IN": "In From", "OUT": "Out To"}


@dataclass(frozen=True)
class SectionSpec:
    """One section the converter reads.

    tag: the XML element under FlexStatement. title: the section's name in
    Client Portal's query editor. rows/level: the row element read and the
    levelOfDetail it must have (None: any); option: what to tick for that
    level. fields: attributes every row read must carry (on the section
    element itself when rows is None).

    Implements: AD-034 (with SECTIONS and _section_rows)
    """

    tag: str
    title: str
    rows: str | None
    level: str | None
    option: str
    fields: tuple[str, ...]


SECTIONS = (
    SectionSpec("AccountInformation", "Account Information", None, None, "",
                ("currency",)),
    SectionSpec("CashReport", "Cash Report", "CashReportCurrency", "Currency",
                "the currency breakout", ("currency", *CASH_COMPONENTS)),
    SectionSpec("Trades", "Trades", "Order", "ORDER", "Orders",
                ("assetCategory", "currency", "symbol", "description", "dateTime",
                 "reportDate", "quantity", "tradePrice", "proceeds", "ibCommission",
                 "ibCommissionCurrency", "taxes", "notes", "ibOrderID")),
    SectionSpec("TransactionTaxes", "Transaction Fees", "TransactionTax", "SUMMARY",
                "Summary", ("currency", "symbol", "date", "reportDate", "taxAmount")),
    SectionSpec("CorporateActions", "Corporate Actions", "CorporateAction", "DETAIL",
                "Detail", ("currency", "dateTime", "reportDate", "actionDescription",
                           "proceeds", "transactionID")),
    SectionSpec("CashTransactions", "Cash Transactions", "CashTransaction", "DETAIL",
                "Detail", ("type", "currency", "dateTime", "settleDate", "reportDate",
                           "description", "amount", "transactionID")),
    SectionSpec("Transfers", "Transfers", "Transfer", "TRANSFER", "Transfer",
                ("type", "direction", "assetCategory", "currency", "date", "reportDate",
                 "account", "cashTransfer", "transactionID")),
    SectionSpec("SalesTaxes", "GST Details", "SalesTax", None, "",
                ("currency", "date", "taxType", "salesTax", "taxableTransactionID",
                 "transactionID")),
)

_DATE = re.compile(r"^(\d{8})$")
_DATETIME = re.compile(r"^(\d{8})(?:;(\d{6}))?$")
_DATE_FORMAT_HINT = (
    "set the query's Date Format to yyyyMMdd, Time Format to HHmmss and the "
    "Date/Time Separator to ';'"
)


def is_flex_report(path: str | Path) -> bool:
    """Whether the file is XML (a Flex report) rather than an Activity Statement CSV.

    Implements: AD-028
    """
    with Path(path).open("rb") as fh:
        head = fh.read(512)
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


def parse_flex(path: str | Path) -> Statement:
    """Read a Flex report file into the typed Statement model."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise StatementError(f"not a readable Flex report: {exc}") from exc
    return to_statement(root)


# --- validation (plan D16) -------------------------------------------------


def _statement_element(root: ET.Element) -> ET.Element:
    # Implements: AD-035 (with the date parsing below and to_statement); D20
    if root.tag != "FlexQueryResponse":
        raise StatementError(f"not a Flex report: root element is <{root.tag}>")
    statements = root.findall("FlexStatements/FlexStatement")
    if len(statements) != 1:
        raise StatementError(
            f"the Flex report holds {len(statements)} account statements; "
            "run a query for exactly one account"
        )
    return statements[0]


def _section_rows(statement: ET.Element, spec: SectionSpec) -> list[ET.Element]:
    """The rows the converter reads from one section, checked against its spec."""
    section = statement.find(spec.tag)
    if section is None:
        raise StatementError(
            f"Flex query: section {spec.title!r} is missing; add it to the query "
            "with all its fields (Select All)"
        )
    if spec.rows is None:
        elements = [section]
    else:
        present = list(section)
        elements = [
            row for row in present
            if row.tag == spec.rows
            and (spec.level is None or row.get("levelOfDetail") == spec.level)
        ]
        if present and not elements:
            raise StatementError(
                f"Flex query: section {spec.title!r} has no {spec.level or spec.rows} "
                f"rows; tick {spec.option!r} in its options"
            )
    for element in elements:
        missing = [f for f in spec.fields if f not in element.attrib]
        if missing:
            raise StatementError(
                f"Flex query: section {spec.title!r} lacks field(s) "
                f"{', '.join(missing)}; select them in the query (or Select All)"
            )
    return elements


# --- field parsing -----------------------------------------------------------


def _where(row: ET.Element, index: int) -> str:
    ident = row.get("transactionID") or row.get("ibOrderID")
    return f"Flex {row.tag} {ident}" if ident else f"Flex {row.tag} #{index + 1}"


def _money(row: ET.Element, field: str, where: str) -> Decimal:
    text = row.get(field, "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StatementError(f"{where}: {field}={text!r} is not an amount") from None


def _optional_money(row: ET.Element, field: str, where: str) -> Decimal | None:
    return _money(row, field, where) if row.get(field, "").strip() else None


def _datetime(row: ET.Element, field: str, where: str) -> dt.datetime:
    text = row.get(field, "")
    match = _DATETIME.match(text)
    try:
        if not match:
            raise ValueError
        date = dt.datetime.strptime(match.group(1), "%Y%m%d")
        time = dt.datetime.strptime(match.group(2), "%H%M%S").time() if match.group(2) else dt.time()
    except ValueError:
        raise StatementError(
            f"{where}: {field}={text!r} is not yyyyMMdd[;HHmmss]; {_DATE_FORMAT_HINT}"
        ) from None
    return dt.datetime.combine(date.date(), time)


def _date(row: ET.Element, field: str, where: str) -> dt.date:
    text = row.get(field, "")
    if not _DATE.match(text):
        raise StatementError(f"{where}: {field}={text!r} is not yyyyMMdd; {_DATE_FORMAT_HINT}")
    return _datetime(row, field, where).date()


def _optional_date(row: ET.Element, field: str, where: str) -> dt.date | None:
    return _date(row, field, where) if row.get(field) else None


def _source(row: ET.Element, index: int, ident: str | None) -> dict:
    where = _where(row, index)
    return {
        "id": ident or f"{row.tag}#{index + 1}",
        "where": where,
        "report_date": _optional_date(row, "reportDate", where),
    }


# --- mapping onto the model --------------------------------------------------


def _cash_report(rows: list[ET.Element]) -> dict[str, dict[str, Decimal]]:
    # Implements: AD-036
    report: dict[str, dict[str, Decimal]] = {}
    for index, row in enumerate(rows):
        where = _where(row, index)
        currency = row.get("currency", "")
        if not CURRENCY_RE.match(currency):
            raise StatementError(f"{where}: Cash Report row for non-currency {currency!r}")
        if currency in report:
            raise StatementError(f"{where}: duplicate Cash Report row for {currency}")
        components: dict[str, Decimal] = {}
        for attr in row.attrib:
            if attr in _CASH_REPORT_NOT_COMPONENTS or attr.endswith(_CASH_REPORT_SPLITS):
                continue
            amount = _money(row, attr, where)
            name = CASH_COMPONENTS.get(attr, attr)
            if amount != 0 or name in ("Starting Cash", "Ending Cash"):
                components[name] = amount
        report[currency] = components
    if not report:
        raise StatementError("the Flex report's Cash Report has no currencies; cannot reconcile")
    return report


def _trades(rows: list[ET.Element]) -> list[Trade]:
    # Implements: AD-037
    trades = []
    for index, row in enumerate(rows):
        where = _where(row, index)
        category = row.get("assetCategory")
        asset_class = _ASSET_CLASS.get(category)
        if asset_class is None:
            raise StatementError(
                f"{where}: unsupported trade asset category {category!r} "
                f"(supported: {sorted(_ASSET_CLASS)})"
            )
        currency = row.get("currency")
        commission_currency = row.get("ibCommissionCurrency")
        if asset_class is not AssetClass.FOREX and commission_currency != currency:
            raise StatementError(
                f"{where}: commission charged in {commission_currency!r} on a "
                f"{currency!r} trade; only forex commissions may differ"
            )
        trades.append(
            Trade(
                **_source(row, index, row.get("ibOrderID")),
                when=_datetime(row, "dateTime", where),
                currency=currency,
                asset_class=asset_class,
                # An option's symbol is the exchange's code; its description
                # carries the readable contract ("XYZ 18JUN26 95 C").
                symbol=(row.get("description") if asset_class is AssetClass.OPTION
                        else row.get("symbol")).strip(),
                quantity=_money(row, "quantity", where),
                price=_optional_money(row, "tradePrice", where),
                proceeds=_optional_money(row, "proceeds", where),
                # ibCommission excludes taxes on it (GST, stamp duty); the
                # model's commission is the full charge.
                commission=_money(row, "ibCommission", where) + _money(row, "taxes", where),
                commission_currency=commission_currency,
                codes=frozenset(c.strip() for c in row.get("notes", "").split(";") if c.strip()),
            )
        )
    return trades


def _corporate_actions(rows: list[ET.Element]) -> list[CorporateAction]:
    return [
        CorporateAction(
            **_source(row, index, row.get("transactionID")),
            when=_datetime(row, "dateTime", _where(row, index)),
            currency=row.get("currency"),
            description=row.get("actionDescription").strip(),
            proceeds=_money(row, "proceeds", _where(row, index)),
        )
        for index, row in enumerate(rows)
    ]


def _cash_txns(rows: list[ET.Element]) -> list[CashTxn]:
    # Implements: AD-038 (with _transfers)
    txns = []
    for index, row in enumerate(rows):
        where = _where(row, index)
        kind = _CASH_KIND.get(row.get("type"))
        if kind is None:
            raise StatementError(
                f"{where}: unsupported cash transaction type {row.get('type')!r} "
                f"(supported: {sorted(_CASH_KIND)})"
            )
        if kind is CashKind.DEPOSIT_WITHDRAWAL:
            date = _date(row, "settleDate", where)
        else:
            date = _datetime(row, "dateTime", where).date()
        txns.append(
            CashTxn(
                **_source(row, index, row.get("transactionID")),
                date=date,
                currency=row.get("currency"),
                kind=kind,
                description=row.get("description").strip(),
                amount=_money(row, "amount", where),
            )
        )
    return txns


def _transfers(rows: list[ET.Element]) -> list[CashTxn]:
    """Cash moved between accounts, reported with the deposits and withdrawals.

    Flex leaves a cash transfer's description as "--"; it is rebuilt in the
    Activity Statement's wording. Position transfers move no cash.
    """
    txns = []
    for index, row in enumerate(rows):
        where = _where(row, index)
        cash = _money(row, "cashTransfer", where)
        if row.get("assetCategory") != "CASH":
            if cash != 0:
                raise StatementError(f"{where}: a position transfer that moves cash")
            continue
        direction = _DIRECTION.get(row.get("direction"))
        if direction is None:
            raise StatementError(f"{where}: unknown transfer direction {row.get('direction')!r}")
        kind = "Internal Transfer" if row.get("type") == "INTERNAL" else f"{row.get('type')} Transfer"
        txns.append(
            CashTxn(
                **_source(row, index, row.get("transactionID")),
                # Flex settleDate is a day earlier than the date the Activity
                # Statement shows; `date` matches it.
                date=_date(row, "date", where),
                currency=row.get("currency"),
                kind=CashKind.DEPOSIT_WITHDRAWAL,
                description=f"{kind} {direction} Account {row.get('account')}",
                amount=cash,
            )
        )
    return txns


def _tax_items(stamp_duty_rows: list[ET.Element], gst_rows: list[ET.Element]) -> list[TaxItem]:
    # Implements: AD-039
    items = [
        TaxItem(
            **_source(row, index, None),
            when=_datetime(row, "date", _where(row, index)),
            currency=row.get("currency"),
            kind=TaxKind.STAMP_DUTY,
            symbol=row.get("symbol").strip(),
            amount=_money(row, "taxAmount", _where(row, index)),
        )
        for index, row in enumerate(stamp_duty_rows)
    ]
    for index, row in enumerate(gst_rows):
        where = _where(row, index)
        if row.get("taxType") != "GST":
            raise StatementError(f"{where}: unsupported sales tax type {row.get('taxType')!r}")
        items.append(
            TaxItem(
                **_source(row, index, row.get("transactionID")),
                when=_datetime(row, "date", where),
                currency=row.get("currency"),
                kind=TaxKind.GST,
                symbol="",
                amount=_money(row, "salesTax", where),
                taxed_id=row.get("taxableTransactionID") or None,
            )
        )
    return items


def to_statement(root: ET.Element) -> Statement:
    """Validate a parsed Flex report against SECTIONS and map it onto the model."""
    statement = _statement_element(root)
    for field in ("accountId", "fromDate", "toDate"):
        if not statement.get(field):
            raise StatementError(f"the Flex report's statement has no {field}")
    rows = {spec.tag: _section_rows(statement, spec) for spec in SECTIONS}
    (account_info,) = rows["AccountInformation"]
    where = "Flex statement"
    return Statement(
        period_start=_date(statement, "fromDate", where),
        period_end=_date(statement, "toDate", where),
        account=statement.get("accountId"),
        base_currency=account_info.get("currency"),
        trades=_trades(rows["Trades"]),
        corporate_actions=_corporate_actions(rows["CorporateActions"]),
        cash_txns=_cash_txns(rows["CashTransactions"]) + _transfers(rows["Transfers"]),
        tax_items=_tax_items(rows["TransactionTaxes"], rows["SalesTaxes"]),
        cash_report=_cash_report(rows["CashReport"]),
    )
