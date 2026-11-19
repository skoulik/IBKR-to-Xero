"""database.py: data.db built from kept reports, and the export record.

Synthetic Flex reports throughout; the real ones in examples/flex, when present,
are checked against the converter's own output. Needs DuckDB with httpfs linked
in (our build): PyPI's cannot write encrypted files offline, so on such an
install - CI's Linux job - every test here skips (plan D65).

Implements: AD-111, AD-112, AD-113, AD-114, AD-115, AD-116
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from decimal import Decimal

import duckdb
import pytest

from ibkr_to_xero import database, parse_statement
from ibkr_to_xero.convert import convert
from ibkr_to_xero.database import BuildFailed, Database, DatabaseError, read_report
from ibkr_to_xero.model import StatementError
from ibkr_to_xero.reconcile import reconcile

from .conftest import EXAMPLES
from .test_flex import _SECTIONS, _cash_report_row, _cash_txn, _trade


def _httpfs_linked() -> bool:
    con = duckdb.connect(config={"autoload_known_extensions": False})
    found = con.execute(
        "SELECT install_mode FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
    ).fetchone()
    con.close()
    return found is not None and "STATIC" in str(found[0]).upper()


pytestmark = pytest.mark.skipif(
    not _httpfs_linked(),
    reason="this DuckDB has no httpfs linked in (PyPI's?): the store needs our build",
)

KEY = "ab" * 32
GST = Decimal("0.1")


def _report(*, start="20260601", end="20260630", generated="20260701;090000",
            account="U0000000", **sections) -> bytes:
    root = ET.Element("FlexQueryResponse", queryName="test", type="AF")
    container = ET.SubElement(root, "FlexStatements", count="1")
    statement = ET.SubElement(container, "FlexStatement", accountId=account, fromDate=start,
                              toDate=end, period="", whenGenerated=generated)
    for tag in _SECTIONS:
        if tag == "AccountInformation":
            ET.SubElement(statement, tag, accountId=account, currency="USD")
            continue
        section = ET.SubElement(statement, tag)
        default = [_cash_report_row(startingCash=100, endingCash=100)] if tag == "CashReport" else []
        for row_tag, attrs in sections.get(tag, default):
            ET.SubElement(section, row_tag, attrs)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _month(month: int, *, trade_id: str, fee_id: str, starting: Decimal, fee="-1.5",
           generated=None) -> bytes:
    """A month with one stock purchase (5 + 1 commission) and one fee."""
    first = dt.date(2026, month, 1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    day = f"{first:%Y%m}15"
    ending = starting - 6 + Decimal(fee)
    return _report(
        start=f"{first:%Y%m%d}", end=f"{last:%Y%m%d}",
        generated=generated or f"{last + dt.timedelta(days=1):%Y%m%d};090000",
        CashReport=[_cash_report_row(startingCash=starting, netTradesPurchases="-5",
                                     commissions="-1", otherFees=fee, endingCash=ending)],
        Trades=[_trade(ibOrderID=trade_id, dateTime=f"{day};100000", reportDate=day)],
        CashTransactions=[_cash_txn(transactionID=fee_id, amount=fee, dateTime=day,
                                    settleDate=day, reportDate=day,
                                    description=f"MARKET DATA {month}")],
    )


JUNE = _month(6, trade_id="901", fee_id="101", starting=Decimal(100))
JULY = _month(7, trade_id="902", fee_id="102", starting=Decimal("92.5"))


class Kept(dict):
    """The store's originals, as ensure_current sees them: names and a reader."""

    def build(self, db, gst_rate=GST):
        return db.ensure_current(sorted(self), self.__getitem__, gst_rate)


@pytest.fixture
def db(tmp_path):
    folder = tmp_path / "O'Brien's ledger"  # an apostrophe in the path, as a user name may have
    folder.mkdir()
    with Database.open(folder / "data.db", folder / "exports.db", KEY) as opened:
        yield opened


def _lines(db, to=dt.date(2026, 12, 31), **kwargs):
    return db.unexported_lines(to, **kwargs)


def _keys(db, **kwargs):
    return [line.key for lines in _lines(db, **kwargs).values() for line in lines]


# --- Building ---------------------------------------------------------------------


def test_reports_become_keyed_lines_in_output_order(db):
    kept = Kept(june=JUNE, july=JULY)
    assert kept.build(db).rebuilt
    assert _keys(db) == [
        "U0000000:T:901", "U0000000:C:101", "U0000000:T:902", "U0000000:C:102",
    ]
    (first, *_), = _lines(db).values()
    assert (first.currency, first.date, first.amount, first.description, first.reference) == (
        "USD", dt.date(2026, 6, 15), Decimal("-6"), "+1 XYZ price: 5 comm: -1", "STOCK"
    )
    assert first.report_date == dt.date(2026, 6, 15)


def test_new_reports_are_added_without_a_rebuild(db):
    kept = Kept(june=JUNE)
    kept.build(db)
    kept["july"] = JULY
    result = kept.build(db)
    assert (result.rebuilt, result.added) == (False, 1)
    assert kept.build(db) == database.BuildResult(rebuilt=False, added=0)
    assert len(_keys(db)) == 4


def test_a_new_gst_rate_or_parser_version_rebuilds(db, monkeypatch):
    kept = Kept(june=JUNE)
    kept.build(db)
    assert kept.build(db, gst_rate=Decimal("0.10")).rebuilt is False  # the same rate
    assert kept.build(db, gst_rate=Decimal("0.15")).rebuilt
    monkeypatch.setattr(database, "PARSER_VERSION", 999)
    assert kept.build(db, gst_rate=Decimal("0.15")).rebuilt
    assert _keys(db) == ["U0000000:T:901", "U0000000:C:101"]


def test_synthetic_and_forex_lines_have_their_own_keys(db):
    forex = _report(
        CashReport=[
            _cash_report_row("AUD", startingCash=0, netTradesSales=100, endingCash=100),
            _cash_report_row("USD", startingCash=100, netTradesPurchases="-66.005",
                             commissions="-2", endingCash="31.995"),
        ],
        Trades=[_trade(assetCategory="CASH", symbol="AUD.USD", quantity="100", tradePrice="0.66",
                       proceeds="-66.005", ibCommission="-2", ibOrderID="950")],
    )
    Kept(fx=forex).build(db)
    assert _keys(db) == [
        "U0000000:T:950:fx-base",
        "U0000000:T:950:fx-quote", "U0000000:T:950:fx-fee", "ROUNDING:U0000000:20260601-20260630:USD",
    ]


def test_a_report_that_does_not_reconcile_leaves_the_database_as_it_was(db, tmp_path):
    kept = Kept(june=JUNE)
    kept.build(db)
    broken = _month(7, trade_id="902", fee_id="102", starting=Decimal("92.5"), fee="-2")
    broken = broken.replace(b'endingCash="84.5"', b'endingCash="80"')
    kept["july"] = broken
    with pytest.raises(BuildFailed, match=r"U0000000, 2026-07-01 to 2026-07-31.*reconcile"):
        kept.build(db)
    assert len(_keys(db)) == 2
    with pytest.raises(BuildFailed):
        kept.build(db, gst_rate=Decimal("0.15"))  # a rebuild fails the same way
    assert len(_keys(db)) == 2
    assert not list(tmp_path.rglob("*.new*"))


def test_of_two_reports_of_one_period_the_later_generated_wins(db):
    earlier = _month(6, trade_id="901", fee_id="101", starting=Decimal(100), fee="-1.5",
                     generated="20260701;090000")
    later = _month(6, trade_id="901", fee_id="101", starting=Decimal(100), fee="-2",
                   generated="20260705;090000")
    Kept(a_later=later, b_earlier=earlier).build(db)  # names out of time order
    fee = _lines(db)["USD"][1]
    assert fee.amount == Decimal("-2")


def test_overlapping_periods_are_refused(db):
    quarter = _report(start="20260601", end="20260731")
    with pytest.raises(BuildFailed, match="overlap"):
        Kept(june=JUNE, quarter=quarter).build(db)


def test_a_report_overlapping_a_stored_one_is_refused_too(db):
    kept = Kept(june=JUNE)
    kept.build(db)
    kept["quarter"] = _report(start="20260601", end="20260731")
    with pytest.raises(BuildFailed, match="overlap"):
        kept.build(db)
    assert len(_keys(db)) == 2


def test_a_transaction_in_two_reports_is_refused(db):
    july_again = _month(7, trade_id="901", fee_id="102", starting=Decimal("92.5"))
    with pytest.raises(BuildFailed, match="repeats a transaction"):
        Kept(june=JUNE, july=july_again).build(db)


def test_more_places_than_the_store_keeps_rejects():
    report = _report(
        CashReport=[_cash_report_row(startingCash=100, otherFees="-1.123456789012",
                                     endingCash="98.876543210988")],
        CashTransactions=[_cash_txn(amount="-1.123456789012")],
    )
    with pytest.raises(StatementError, match="more than 11 decimal places"):
        read_report(report, GST)


def test_values_too_large_or_too_long_reject_rather_than_crash():
    from ibkr_to_xero.database import _check_numbers
    from ibkr_to_xero.model import Statement

    def check(value):
        statement = Statement(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                              cash_report={"USD": {"Starting Cash": Decimal(value)}})
        _check_numbers(statement, [])

    check("99999999999999999999999999.12345678901")  # 26 + 11 digits: fits
    with pytest.raises(StatementError, match="too large"):
        check("1" + "0" * 27)
    with pytest.raises(StatementError, match="more than 11"):
        check("99999999999999999999999999.123456789012")


def test_trailing_zeros_past_the_scale_are_not_rounding():
    report = _report(
        CashReport=[_cash_report_row(startingCash=100, otherFees="-1.500000000000",
                                     endingCash="98.5")],
        CashTransactions=[_cash_txn(amount="-1.500000000000")],
    )
    assert read_report(report, GST).lines


# --- Opening ----------------------------------------------------------------------


def test_the_files_are_encrypted(tmp_path):
    with Database.open(tmp_path / "data.db", tmp_path / "exports.db", KEY) as db:
        Kept(june=JUNE).build(db)
    for name in ("data.db", "exports.db"):
        content = (tmp_path / name).read_bytes()
        assert b"MARKET DATA" not in content and b"U0000000" not in content
    with pytest.raises(DatabaseError):
        Database.open(tmp_path / "data.db", tmp_path / "exports.db", "cd" * 32)
    with Database.open(tmp_path / "data.db", tmp_path / "exports.db", KEY) as db:
        assert len(_keys(db)) == 2


# --- The export record ------------------------------------------------------------


def test_an_export_is_pending_until_confirmed(db):
    Kept(june=JUNE, july=JULY).build(db)
    june_keys = _keys(db, to=dt.date(2026, 6, 30))
    ids = db.record_export(dt.date(2026, 6, 30), "C:/exports/2026-06", {"USD": ("USD.csv", june_keys)},
                           created_at=dt.datetime(2026, 7, 2, 10))
    (pending,) = db.pending_files()
    assert (pending.id, pending.currency, pending.filename, pending.to_date) == (
        ids["USD"], "USD", "USD.csv", dt.date(2026, 6, 30))
    assert len(_keys(db)) == 4  # not confirmed: exported again (D10)
    db.confirm(ids["USD"])
    db.confirm(ids["USD"])  # twice is no error
    assert db.pending_files() == []
    assert _keys(db) == ["U0000000:T:902", "U0000000:C:102"]


def test_the_export_record_survives_a_rebuild(db):
    kept = Kept(june=JUNE, july=JULY)
    kept.build(db)
    ids = db.record_export(dt.date(2026, 6, 30), "x", {"USD": ("USD.csv", _keys(db, to=dt.date(2026, 6, 30)))})
    db.confirm(ids["USD"])
    assert kept.build(db, gst_rate=Decimal("0.15")).rebuilt
    assert _keys(db) == ["U0000000:T:902", "U0000000:C:102"]


def test_already_in_xero_counts_by_report_date(db):
    Kept(june=JUNE, july=JULY).build(db)
    assert _keys(db, imported_through={"USD": dt.date(2026, 6, 30)}) == [
        "U0000000:T:902", "U0000000:C:102",
    ]
    assert len(_keys(db, imported_through={"AUD": dt.date(2026, 6, 30)})) == 4


def test_an_export_names_only_lines_of_the_account(db):
    Kept(june=JUNE).build(db)
    with pytest.raises(ValueError, match="not lines"):
        db.record_export(dt.date(2026, 6, 30), "x", {"USD": ("USD.csv", ["U0000000:T:nope"])})
    with pytest.raises(DatabaseError):
        db.confirm(42)
    assert db.pending_files() == []


# --- The real reports ---------------------------------------------------------------


def _example_reports():
    folder = EXAMPLES / "flex"
    return sorted(folder.glob("*.xml")) if folder.is_dir() else []


@pytest.mark.skipif(not _example_reports(), reason="no Flex reports in examples/flex (gitignored)")
def test_real_reports_build_to_the_converters_own_rows(tmp_path):
    built = 0
    for index, path in enumerate(_example_reports()):
        try:
            statement = parse_statement(path)
            results = reconcile(statement, convert(statement))
        except Exception:
            continue  # a report the converter itself rejects
        folder = tmp_path / str(index)
        folder.mkdir()
        with Database.open(folder / "data.db", folder / "exports.db", KEY) as db:
            Kept(report=path.read_bytes()).build(db)
            lines = db.unexported_lines(statement.period_end + dt.timedelta(days=3650))
        for result in results:
            expected = [(r.date, r.amount, r.description, r.reference) for r in result.rows]
            got = [(l.date, l.amount, l.description, l.reference) for l in lines.get(result.currency, [])]
            assert got == expected, (path.name, result.currency)
        built += 1
    assert built
