"""operations.py: fetching into the store, exporting, and the answers between, against
a fake Flex Web Service (tests/flex_fake.py). Needs our DuckDB build, as the database's
tests do: on PyPI's (CI's Linux job) every test here skips (plan D65).

Implements: AD-120, AD-121, AD-122, AD-123, AD-124, AD-125, AD-126, AD-127, AD-128, AD-130
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import operations as ops_module
from ibkr_to_xero import parse_statement, vault
from ibkr_to_xero.convert import convert
from ibkr_to_xero.database import Database
from ibkr_to_xero.flex_client import FlexNotAvailable
from ibkr_to_xero.reconcile import reconcile
from ibkr_to_xero.operations import (
    CurrenciesUnanswered,
    DoesNotJoin,
    NotAvailable,
    NothingStored,
    NotSetUp,
    Operations,
    OutsideStoredRange,
    PendingFiles,
    WrongAccount,
)
from ibkr_to_xero.store import Store

from .conftest import EXAMPLES
from .flex_fake import Event, FakeFlex
from .test_database import _httpfs_linked

pytestmark = pytest.mark.skipif(
    not _httpfs_linked(),
    reason="this DuckDB has no httpfs linked in (PyPI's?): the store needs our build",
)

CHEAP = vault.Cost(n=2**4, r=8, p=1)
D = dt.date
# Saturday 21 November 2026, 02:49 UTC: 21:49 on the Friday in New York (plan I3).
SATURDAY = dt.datetime(2026, 11, 21, 2, 49, tzinfo=dt.timezone.utc)
THURSDAY = D(2026, 11, 19)
YEAR_BEFORE = D(2025, 11, 20)


@pytest.fixture(autouse=True)
def _no_real_credential_store(memory_keyring):
    return memory_keyring


@pytest.fixture
def store(tmp_path):
    return Store.create(tmp_path / "store", "pw", cost=CHEAP)


@pytest.fixture
def fake():
    flex = FakeFlex(closed=THURSDAY)
    flex.fee_every_weekday(D(2024, 1, 1), D(2026, 12, 31))
    return flex


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


@pytest.fixture
def clock():
    return Clock(SATURDAY)


@pytest.fixture
def ops(store, fake, clock, tmp_path):
    ledger = store.add_ledger("My IBKR")
    ledger.save_settings(dataclasses.replace(ledger.settings(), flex_token="TOKEN",
                                             flex_query_id="1234"))
    return Operations(store, ledger, client_factory=fake, now=clock,
                      exports_root=tmp_path / "Documents" / "IBKR to Xero")


def _rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))[1:]


# --- Fetching ---------------------------------------------------------------------------


def test_the_last_closed_day_is_yesterday_in_new_york(ops, clock):
    assert ops.last_closed() == THURSDAY
    clock.when = dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc)  # 01:00 Saturday in NY
    assert ops.last_closed() == D(2026, 11, 20)


def test_the_first_fetch_takes_the_last_365_days(ops, fake, store):
    result = ops.fetch()
    assert fake.requests == [(YEAR_BEFORE, THURSDAY)]
    assert result.added == [(YEAR_BEFORE, THURSDAY)]
    assert (result.through, result.to_ask) == (THURSDAY, ["USD"])
    assert ops.ledger.settings().accounts == {"U0000000": "USD"}
    assert store.ledgers()[0].accounts == ("U0000000",)
    assert len(ops.ledger.originals()) == 1


def test_a_catch_up_steps_back_to_the_last_day_ibkr_has_closed(ops, fake, clock):
    ops.fetch()
    clock.when = dt.datetime(2026, 11, 23, 10, 0, tzinfo=dt.timezone.utc)  # Monday morning NY
    fake.closed = D(2026, 11, 20)  # Friday closed; the weekend "not yet"
    fake.requests.clear()
    result = ops.fetch()
    assert fake.requests == [
        (D(2026, 11, 20), D(2026, 11, 22)), (D(2026, 11, 20), D(2026, 11, 21)),
        (D(2026, 11, 20), D(2026, 11, 20)),
    ]
    assert result.added == [(D(2026, 11, 20), D(2026, 11, 20))]


def test_nothing_closed_since_the_stored_end_is_up_to_date(ops, fake, clock):
    ops.fetch()
    fake.requests.clear()
    assert ops.fetch().up_to_date and fake.requests == []  # yesterday in NY is stored already
    clock.when = dt.datetime(2026, 11, 23, 3, 0, tzinfo=dt.timezone.utc)  # Sunday evening NY
    result = ops.fetch()
    assert result.up_to_date and result.through == THURSDAY
    assert fake.requests == [(D(2026, 11, 20), D(2026, 11, 21)), (D(2026, 11, 20), D(2026, 11, 20))]


def test_a_weekend_joins_the_period_after_it(ops, fake, clock):
    fake.closed = D(2026, 11, 20)
    clock.when = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.timezone.utc)  # Friday closed
    ops.fetch()
    clock.when = dt.datetime(2026, 11, 23, 3, 0, tzinfo=dt.timezone.utc)
    fake.closed = D(2026, 11, 22)
    assert ops.fetch().up_to_date  # Saturday alone holds no business day
    clock.when = dt.datetime(2026, 11, 25, 3, 0, tzinfo=dt.timezone.utc)
    fake.closed = D(2026, 11, 23)
    assert ops.fetch().added == [(D(2026, 11, 21), D(2026, 11, 23))]


def test_a_long_absence_is_caught_up_a_year_at_a_time(ops, fake, clock):
    fake.closed = D(2025, 6, 30)
    clock.when = dt.datetime(2025, 7, 1, 12, tzinfo=dt.timezone.utc)
    ops.fetch()
    fake.closed, clock.when = THURSDAY, SATURDAY
    result = ops.fetch()
    assert result.added == [(D(2025, 7, 1), D(2026, 6, 30)), (D(2026, 7, 1), THURSDAY)]


def test_a_report_that_does_not_join_by_cash_is_not_kept(ops, fake, clock):
    ops.fetch()
    fake.events[10].amount = Decimal("-9")  # IBKR changed a day already stored
    fake.closed = D(2026, 11, 20)
    clock.when = dt.datetime(2026, 11, 21, 10, tzinfo=dt.timezone.utc)
    with pytest.raises(DoesNotJoin, match=r"2026-11-19 and 2026-11-20: USD ends at .*re-fetch"):
        ops.fetch()
    assert len(ops.ledger.originals()) == 1


def test_a_report_of_another_ib_account_is_refused(ops, fake, clock):
    ops.fetch()
    fake.account = "U9999999"
    fake.closed = D(2026, 11, 20)
    clock.when = dt.datetime(2026, 11, 21, 10, tzinfo=dt.timezone.utc)
    with pytest.raises(WrongAccount, match="U9999999.*U0000000"):
        ops.fetch()


def test_an_account_younger_than_a_year_starts_later(ops, fake):
    fake.funded = D(2026, 6, 1)
    with pytest.raises(NotAvailable, match="start later"):
        ops.fetch()
    assert ops.fetch(start=D(2026, 6, 1)).added == [(D(2026, 6, 1), THURSDAY)]
    with pytest.raises(ops_module.OperationError, match="first fetch only"):
        ops.fetch(start=D(2026, 6, 1))


def test_without_a_token_nothing_is_fetched(ops):
    ops.ledger.save_settings(dataclasses.replace(ops.ledger.settings(), flex_token=""))
    with pytest.raises(NotSetUp):
        ops.fetch()


def test_a_report_the_database_refuses_is_taken_out_again(ops, fake, clock):
    ops.fetch()
    fake.events.append(Event("20261119", D(2026, 11, 20), Decimal("-1")))  # a stored ID again
    fake.closed = D(2026, 11, 20)
    clock.when = dt.datetime(2026, 11, 21, 10, tzinfo=dt.timezone.utc)
    with pytest.raises(Exception, match="repeats a transaction"):
        ops.fetch()
    assert len(ops.ledger.originals()) == 1


# --- The first run's questions (D12, D13) ----------------------------------------------


def test_fetching_back_to_the_earliest_xero_date(ops, fake):
    ops.fetch()
    ops.set_xero_dates({"USD": D(2024, 6, 30)})
    fake.requests.clear()
    result = ops.fetch_back()
    assert fake.requests == [(D(2024, 11, 20), D(2025, 11, 19)), (D(2024, 7, 1), D(2024, 11, 19))]
    assert result.added == [(D(2024, 7, 1), D(2024, 11, 19)), (D(2024, 11, 20), D(2025, 11, 19))]
    assert ops.status().first == D(2024, 7, 1)
    fake.requests.clear()
    assert ops.fetch_back().added == [] and fake.requests == []


def test_nothing_in_xero_needs_no_fetching_back(ops, fake):
    ops.fetch()
    ops.set_xero_dates({"USD": None})
    assert ops.fetch_back().added == []
    assert ops.status().to_ask == []


def test_a_currency_found_going_back_is_asked_about(ops, fake):
    fake.events.append(Event("AUD1", D(2025, 3, 3), Decimal("-2"), currency="AUD"))
    ops.fetch()  # the AUD balance is in the last year's Cash Report too
    assert ops.status().to_ask == ["AUD", "USD"]
    ops.set_xero_dates({"USD": D(2025, 1, 31), "AUD": None})
    assert ops.fetch_back().to_ask == []


# --- Exporting ---------------------------------------------------------------------------


def test_an_export_writes_files_named_for_the_account_and_records_them(ops, tmp_path):
    ops.fetch()
    ops.set_xero_dates({"USD": D(2026, 11, 13)})
    exported = ops.export()
    folder = tmp_path / "Documents" / "IBKR to Xero" / "My IBKR"
    assert exported.folder.parent == folder
    assert exported.folder.name.endswith(" up to 2026-11-19")
    (file,) = exported.files
    assert file.path.name == "My IBKR USD.csv"
    assert [row[0] for row in _rows(file.path)] == [
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert (exported.folder / "ibkr2xero_report.txt").read_text(encoding="utf-8") == exported.report
    assert "My IBKR USD.csv: 4 transactions" in exported.report
    (pending,) = ops.pending()
    assert pending.filename == "My IBKR USD.csv"


def test_an_export_waits_for_an_answer_on_every_pending_file(ops):
    ops.fetch()
    ops.set_xero_dates({"USD": D(2026, 11, 13)})
    first = ops.export()
    with pytest.raises(PendingFiles) as refused:
        ops.export()
    (pending,) = refused.value.files
    ops.resolve({pending.id: False})  # not imported: its lines go out again
    again = ops.export()
    assert again.folder.name == first.folder.name + " 2"
    assert _rows(again.files[0].path) == _rows(first.files[0].path)
    with pytest.raises(Exception, match="discarded"):
        ops.resolve({pending.id: True})
    ops.resolve({ops.pending()[0].id: True})
    assert ops.export().files == []


def test_an_export_needs_every_currency_answered(ops):
    ops.fetch()
    with pytest.raises(CurrenciesUnanswered, match="USD"):
        ops.export()


def test_an_export_before_any_fetch_is_refused(ops):
    with pytest.raises(NothingStored):
        ops.export()


def test_zero_amount_lines_can_be_left_out(ops, fake):
    fake.events.append(Event("Z1", D(2026, 11, 18), Decimal(0)))
    ops.fetch()
    ops.set_xero_dates({"USD": D(2026, 11, 13)})
    exported = ops.export(skip_zero=True)
    assert len(_rows(exported.files[0].path)) == 4
    assert "1 zero-amount skipped" in exported.files[0].summary


def test_the_first_export_lists_lines_either_side_of_the_xero_date(ops, fake):
    fake.events += [
        Event("LATE", D(2026, 11, 2), Decimal("-3"), report_date=D(2026, 11, 17)),
        Event("EARLY", D(2026, 11, 18), Decimal("-4"), report_date=D(2026, 11, 12)),
    ]
    ops.fetch()
    ops.set_xero_dates({"USD": D(2026, 11, 13)})
    exported = ops.export()
    assert len(exported.to_check) == 2
    assert "FEE LATE: dated by 2026-11-13 but reported 2026-11-17, so included" in exported.to_check[0]
    assert "FEE EARLY: reported 2026-11-12, by 2026-11-13, so left out" in exported.to_check[1]
    ops.resolve({ops.pending()[0].id: False})
    assert ops.export().to_check == []  # the first export only


def test_a_re_export_writes_a_range_and_records_nothing(ops):
    ops.fetch()
    ops.set_xero_dates({"USD": None})
    exported = ops.reexport(D(2026, 11, 2), D(2026, 11, 6))
    assert exported.folder.name.endswith("re-export 2026-11-02 to 2026-11-06")
    assert len(_rows(exported.files[0].path)) == 5
    assert ops.pending() == []


# --- Re-fetching (D66, D69) -------------------------------------------------------------


def _daily(ops, fake, clock, last):
    """Catch up one business day at a time to `last`, as a user fetching daily would."""
    day = fake.closed
    while day < last:
        day += dt.timedelta(days=1)
        fake.closed = day
        clock.when = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(12),
                                         tzinfo=dt.timezone.utc)
        ops.fetch()


def test_a_re_fetch_repeats_the_stored_periods_and_says_what_changed(ops, fake, clock):
    ops.fetch()
    _daily(ops, fake, clock, D(2026, 11, 27))
    ops.set_xero_dates({"USD": D(2026, 11, 13)})
    exported = ops.export()
    ops.resolve({ops.pending()[0].id: True})
    changed = next(e for e in fake.events if e.date == D(2026, 11, 24))
    changed.amount = Decimal("-2")
    fake.requests.clear()
    refetched = ops.refetch(D(2026, 11, 24), D(2026, 11, 27))
    assert fake.requests == [(D(2026, 11, 24), D(2026, 11, 24)), (D(2026, 11, 25), D(2026, 11, 25)),
                             (D(2026, 11, 26), D(2026, 11, 26)), (D(2026, 11, 27), D(2026, 11, 27))]
    first = refetched.periods[0]
    (change,) = first.changes
    assert (change.kind, change.old.amount, change.new.amount, change.exported) == (
        "changed", Decimal("-1.5"), Decimal("-2"), "confirmed")
    (moved,) = first.cash
    assert moved.startswith("Ending Cash: USD was ")
    assert all(p.cash and not p.changes for p in refetched.periods[1:])  # later cash moves too
    assert refetched.breaks == []
    ops.keep(refetched)
    assert exported  # the line stays exported: Xero is corrected by hand
    lines = ops.reexport(D(2026, 11, 24), D(2026, 11, 24)).files[0].path
    assert _rows(lines)[0][1] == "-2"


def test_a_re_fetch_that_leaves_a_break_cannot_be_kept(ops, fake, clock):
    ops.fetch()
    _daily(ops, fake, clock, D(2026, 11, 27))
    next(e for e in fake.events if e.date == D(2026, 11, 24)).amount = Decimal("-2")
    refetched = ops.refetch(D(2026, 11, 24), D(2026, 11, 24))
    (gap,) = refetched.breaks
    assert gap.startswith("at 2026-11-24: USD ends at") and "widen the re-fetch to end after" in gap
    with pytest.raises(DoesNotJoin):
        ops.keep(refetched)


def test_a_re_fetch_takes_whole_periods_and_stays_inside_the_stored_data(ops, fake):
    ops.fetch()
    fake.requests.clear()
    refetched = ops.refetch(D(2026, 3, 1), D(2026, 3, 14))
    assert fake.requests == [(YEAR_BEFORE, THURSDAY)]
    assert not refetched.changed
    with pytest.raises(OutsideStoredRange, match="2025-11-20 to 2026-11-19"):
        ops.refetch(D(2026, 11, 1), D(2026, 11, 20))


# --- The real reports ---------------------------------------------------------------------


def _real_months():
    folder = EXAMPLES / "flex"
    found = {}
    for month in ("20260501_20260531", "20260601_20260630"):
        paths = sorted(folder.glob(f"flex_{month}_*.xml")) if folder.is_dir() else []
        if paths:
            found[month] = paths[-1]
    return found if len(found) == 2 else None


class RealFlex:
    """Serves the real month reports in examples/flex for their exact ranges."""

    def __init__(self, months):
        self.months = {
            (D(int(k[:4]), int(k[4:6]), int(k[6:8])), D(int(k[9:13]), int(k[13:15]), int(k[15:17]))): p
            for k, p in months.items()
        }

    def __call__(self, token):
        return self

    def fetch(self, query_id, start, end, *, progress=None, max_wait=None):
        if (start, end) not in self.months:
            raise FlexNotAvailable("not available", 1003)
        return self.months[(start, end)].read_bytes()


@pytest.mark.skipif(not _real_months(), reason="no real month reports in examples/flex (gitignored)")
def test_catching_up_across_real_months_gives_the_converters_lines(store, tmp_path):
    months = _real_months()
    ledger = store.add_ledger("Real")
    ledger.save_settings(dataclasses.replace(ledger.settings(), flex_token="T", flex_query_id="Q"))
    clock = Clock(dt.datetime(2026, 6, 1, 12, tzinfo=dt.timezone.utc))
    ops = Operations(store, ledger, client_factory=RealFlex(months), now=clock,
                     exports_root=tmp_path / "exports")
    assert ops.fetch(start=D(2026, 5, 1)).added == [(D(2026, 5, 1), D(2026, 5, 31))]
    clock.when = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)
    assert ops.fetch().added == [(D(2026, 6, 1), D(2026, 6, 30))]  # the cash joins
    with Database.open(ledger.data_db, ledger.exports_db, ledger.database_key) as db:
        stored = [db.period_lines(p.id) for p in db.periods()]
    for lines, path in zip(stored, months.values()):
        statement = parse_statement(path)
        expected = [(r.date, r.amount, r.description, r.reference)
                    for result in reconcile(statement, convert(statement)) for r in result.rows]
        assert [(l.date, l.amount, l.description, l.reference) for l in lines] == expected


# --- Status -----------------------------------------------------------------------------


def test_status(ops):
    assert ops.status().first is None
    ops.fetch()
    status = ops.status()
    assert (status.name, status.first, status.last, status.periods, status.set_up) == (
        "My IBKR", YEAR_BEFORE, THURSDAY, 1, True)
    assert status.to_ask == ["USD"] and status.pending == []


def test_documents_folder_exists():
    assert ops_module.documents_folder().name  # Windows: the known folder; elsewhere ~/Documents
