"""A stand-in for IBKR's Flex Web Service: an account whose history is a list of fee
transactions, answering any date range with a report that reconciles, and refusing
ranges as IBKR does (plan I3): past the last closed day, or holding no business day,
with error 1003. Synthetic data only."""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal

from ibkr_to_xero.flex_client import FlexNotAvailable

from .test_flex import _SECTIONS, _cash_report_row, _cash_txn


@dataclass
class Event:
    id: str
    date: dt.date
    amount: Decimal
    report_date: dt.date | None = None  # its own date when None
    currency: str = "USD"

    @property
    def reported(self) -> dt.date:
        return self.report_date or self.date


@dataclass
class FakeFlex:
    """Answers `fetch` like FlexClient; `requests` records each (from, to) asked."""

    closed: dt.date  # the last day IBKR has closed
    events: list[Event] = field(default_factory=list)
    starting: Decimal = Decimal(1000)
    funded: dt.date = dt.date(2000, 1, 1)  # no statement before (assumed refused like 1003)
    account: str = "U0000000"
    requests: list[tuple[dt.date, dt.date]] = field(default_factory=list)
    generated: int = 0

    def __call__(self, token: str) -> FakeFlex:  # the client factory
        assert token == "TOKEN"
        return self

    def fee_every_weekday(self, start: dt.date, end: dt.date, amount="-1.5") -> None:
        day = start
        while day <= end:
            if day.weekday() < 5:
                self.events.append(Event(f"{day:%Y%m%d}", day, Decimal(amount)))
            day += dt.timedelta(days=1)

    def fetch(self, query_id, from_date, to_date, *, progress=None, max_wait=None) -> bytes:
        assert query_id == "1234"
        self.requests.append((from_date, to_date))
        days = [from_date + dt.timedelta(days=n) for n in range((to_date - from_date).days + 1)]
        if to_date > self.closed or from_date < self.funded or all(d.weekday() >= 5 for d in days):
            raise FlexNotAvailable("SendRequest: Statement is not available. (error 1003)", 1003)
        self.generated += 1
        return self.report(from_date, to_date)

    def report(self, start: dt.date, end: dt.date) -> bytes:
        currencies = sorted({e.currency for e in self.events} | {"USD"})
        rows, cash = [], []
        for currency in currencies:
            mine = [e for e in self.events if e.currency == currency]
            base = self.starting if currency == "USD" else Decimal(0)
            before = base + sum((e.amount for e in mine if e.reported < start), Decimal(0))
            inside = [e for e in mine if start <= e.reported <= end]
            fees = sum((e.amount for e in inside), Decimal(0))
            cash.append(_cash_report_row(currency, startingCash=before, otherFees=fees,
                                         endingCash=before + fees))
            rows += [
                _cash_txn(transactionID=e.id, currency=currency, amount=str(e.amount),
                          dateTime=f"{e.date:%Y%m%d}", settleDate=f"{e.date:%Y%m%d}",
                          reportDate=f"{e.reported:%Y%m%d}", description=f"FEE {e.id}")
                for e in inside
            ]
        root = ET.Element("FlexQueryResponse", queryName="test", type="AF")
        container = ET.SubElement(root, "FlexStatements", count="1")
        when = dt.datetime(2026, 1, 1) + dt.timedelta(minutes=self.generated)
        statement = ET.SubElement(container, "FlexStatement", accountId=self.account,
                                  fromDate=f"{start:%Y%m%d}", toDate=f"{end:%Y%m%d}", period="",
                                  whenGenerated=f"{when:%Y%m%d;%H%M%S}")
        for tag in _SECTIONS:
            if tag == "AccountInformation":
                ET.SubElement(statement, tag, accountId=self.account, currency="USD")
                continue
            section = ET.SubElement(statement, tag)
            for row_tag, attrs in {"CashReport": cash, "CashTransactions": rows}.get(tag, []):
                ET.SubElement(section, row_tag, attrs)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
