"""Typed model of a statement period, shared by every front end, and converter output.

The Activity Statement CSV parser (parser.py) and, later, the Flex report parser
each produce a Statement; convert.py and reconcile.py only ever see this model,
never a source format.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

PAYEE = "Interactive Brokers"

# A currency cell is a 3-letter ISO-4217 code; anything else in a currency
# column ("Total", "Total in USD", "Base Currency Summary", "") is an aggregate.
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class StatementError(Exception):
    """The input statement is malformed or uses an unsupported format."""


class ReconciliationError(Exception):
    """Cash does not reconcile; the input must be rejected.

    Carries a human-readable multi-line report in str(exc).
    """


def fmt_number(value: Decimal) -> str:
    """Render a Decimal like the reference outputs: plain, no trailing zeros."""
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def parse_money(text: str) -> Decimal:
    """Parse an IB numeric cell ('1,234.5678' style) into a Decimal."""
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        raise StatementError(f"empty numeric field (expected an amount): {text!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise StatementError(f"cannot parse amount {text!r}") from exc


@dataclass
class SectionRow:
    """One Data row of an Activity Statement CSV section, keyed by its Header columns."""

    section: str
    values: dict[str, str]
    line_no: int  # 1-based line number in the source file, for error reports

    def __getitem__(self, key: str) -> str:
        try:
            return self.values[key]
        except KeyError:
            raise StatementError(
                f"line {self.line_no}: section {self.section!r} row is missing "
                f"expected column {key!r} (columns: {list(self.values)})"
            ) from None


class AssetClass(enum.Enum):
    """Trade instrument types; the value is the output's Reference tag."""

    STOCK = "STOCK"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    BOND = "BOND"
    FOREX = "FX"


class CashKind(enum.Enum):
    """Cash transactions by the bucket the Cash Report checks them in.

    The value doubles as the bucket's name in reports and errors, and fixes the
    output order (this declaration order).
    """

    FEE = "Fees"
    DEPOSIT_WITHDRAWAL = "Deposits & Withdrawals"
    DIVIDEND = "Dividends"
    WITHHOLDING = "Withholding Tax"
    INTEREST = "Interest"
    COMMISSION_ADJUSTMENT = "Commission Adjustments"


class TaxKind(enum.Enum):
    STAMP_DUTY = "stamp_duty"  # per-trade transaction tax, already inside its commission


@dataclass(frozen=True)
class Record:
    """Fields every source record carries.

    id: the source's identifier — IBKR's transaction ID where it has one, else
        the source line number. where: how an error message points at it
        ("line 57"). report_date: when IBKR reported it, where the source says.
    """

    id: str
    where: str
    report_date: dt.date | None


@dataclass(frozen=True)
class Trade(Record):
    when: dt.datetime
    currency: str  # the trade's cash currency (a forex pair's quote currency)
    asset_class: AssetClass
    symbol: str
    quantity: Decimal
    price: Decimal | None
    proceeds: Decimal | None  # None where the source leaves it empty (futures)
    commission: Decimal  # as charged, any GST and transaction taxes included
    commission_currency: str
    codes: frozenset[str]  # lifecycle codes: "A" assigned, "Ep" expired, ...


@dataclass(frozen=True)
class CorporateAction(Record):
    when: dt.datetime
    currency: str
    description: str
    proceeds: Decimal


@dataclass(frozen=True)
class CashTxn(Record):
    date: dt.date
    currency: str
    kind: CashKind
    description: str
    amount: Decimal


@dataclass(frozen=True)
class TaxItem(Record):
    """A tax already inside some other row's amount, itemised for display and checks."""

    when: dt.datetime
    currency: str
    kind: TaxKind
    symbol: str
    amount: Decimal


@dataclass
class Statement:
    """One statement period, as every front end delivers it.

    Records keep source order within each list. cash_report maps currency ->
    Cash Report component -> amount, components named as in the Activity
    Statement ("Starting Cash", "Trades (Sales)", ...); the base-currency
    summary is not included.
    """

    period_start: dt.date
    period_end: dt.date
    account: str = ""
    base_currency: str = ""
    trades: list[Trade] = field(default_factory=list)
    corporate_actions: list[CorporateAction] = field(default_factory=list)
    cash_txns: list[CashTxn] = field(default_factory=list)
    tax_items: list[TaxItem] = field(default_factory=list)
    cash_report: dict[str, dict[str, Decimal]] = field(default_factory=dict)


@dataclass
class OutputRow:
    """One transaction line of the Xero import CSV."""

    date: dt.date
    amount: Decimal  # unrounded; rounding happens once, during reconciliation
    description: str
    payee: str = PAYEE
    reference: str = ""  # "" for real transactions, "MTM"/"ROUNDING" for synthetic
    source_section: str = ""  # statement section the row came from (not written to CSV)


@dataclass
class CurrencyResult:
    """Validated, ready-to-write outcome for one currency."""

    currency: str
    rows: list[OutputRow]
    starting_cash: Decimal
    ending_cash: Decimal
    notes: list[str] = field(default_factory=list)
