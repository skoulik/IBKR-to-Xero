"""Typed model for a parsed IB Activity Statement and converter output."""

from __future__ import annotations

import datetime as dt
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
    """One Data row of a statement section, keyed by its Header columns."""

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


@dataclass
class Statement:
    """A parsed multi-section IB Activity Statement."""

    sections: dict[str, list[SectionRow]]
    period_start: dt.date
    period_end: dt.date
    account: str = ""

    def rows(self, section: str) -> list[SectionRow]:
        return self.sections.get(section, [])


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
