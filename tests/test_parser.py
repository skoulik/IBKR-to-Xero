import datetime as dt
import re
from decimal import Decimal

import pytest

from ibkr_to_xero import StatementError, parse_statement
from ibkr_to_xero.model import AssetClass, CashKind
from ibkr_to_xero.parser import parse_sections


@pytest.fixture(scope="module")
def raw(statement_csv):
    return parse_sections(statement_csv)


def test_metadata(statement):
    assert re.fullmatch(r"U\d+", statement.account)
    assert statement.period_start == dt.date(2026, 6, 1)
    assert statement.period_end == dt.date(2026, 6, 30)
    assert re.fullmatch(r"[A-Z]{3}", statement.base_currency)


def test_bom_stripped(raw):
    # The first section's name carries a UTF-8 BOM in the raw file.
    assert "Statement" in raw.sections


def test_sections_parsed(raw):
    for section in (
        "Trades",
        "Cash Report",
        "Deposits & Withdrawals",
        "Fees",
        "Dividends",
        "Withholding Tax",
        "Interest",
    ):
        assert raw.rows(section), f"section {section} should have rows"


def test_running_headers_per_section(raw):
    # Trades re-emits a Header per asset category; both must be parsed.
    categories = {row["Asset Category"] for row in raw.rows("Trades")}
    assert len(categories) > 1


def test_aggregate_row_kinds_skipped(raw):
    # SubTotal/Total/Notes rows never become data rows.
    discriminators = {row["DataDiscriminator"] for row in raw.rows("Trades")}
    assert discriminators == {"Order"}


def test_model_records(statement):
    assert statement.trades and statement.cash_txns
    # Real currencies only: the base-currency summary block is excluded.
    assert statement.cash_report
    assert all(re.fullmatch(r"[A-Z]{3}", c) for c in statement.cash_report)
    assert all("Starting Cash" in c and "Ending Cash" in c
               for c in statement.cash_report.values())


def test_unknown_row_kind_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Statement,Header,Field Name,Field Value\n"
        "Statement,Data,Period,\"June 1, 2026 - June 30, 2026\"\n"
        "Trades,Mystery,foo\n",
        encoding="utf-8",
    )
    with pytest.raises(StatementError, match="unknown row kind 'Mystery'"):
        parse_statement(bad)


def test_data_before_header_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Trades,Data,Order,Stocks\n", encoding="utf-8")
    with pytest.raises(StatementError, match="before any Header"):
        parse_statement(bad)


# --- mapping sections onto the model (small synthetic statements) ---------

_PREAMBLE = (
    "Statement,Header,Field Name,Field Value\n"
    "Statement,Data,Period,\"June 1, 2026 - June 30, 2026\"\n"
    "Cash Report,Header,Currency Summary,Currency,Total\n"
    "Cash Report,Data,Starting Cash,USD,0\n"
    "Cash Report,Data,Ending Cash,USD,0\n"
)
_TRADES_HEADER = (
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
    "Quantity,T. Price,Proceeds,Comm/Fee,Code\n"
)


def _parse(tmp_path, body: str):
    path = tmp_path / "statement.csv"
    path.write_text(_PREAMBLE + body, encoding="utf-8")
    return parse_statement(path)


def test_trade_fields(tmp_path):
    statement = _parse(tmp_path, _TRADES_HEADER + (
        'Trades,Data,Order,Bonds,USD,XYZ,"2026-06-15, 10:00:00",'
        '"-20,000",98.75,19750,-2,C;P\n'
    ))
    (trade,) = statement.trades
    assert trade.asset_class is AssetClass.BOND
    assert trade.quantity == Decimal("-20000")
    assert trade.when == dt.datetime(2026, 6, 15, 10, 0, 0)
    assert trade.codes == {"C", "P"}
    assert trade.where == "line 7"


_FOREX_HEADER = (
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
    "Quantity,T. Price,Proceeds,{comm},Code\n"
)
_FOREX_ROW = 'Trades,Data,Order,Forex,USD,AUD.USD,"2026-06-15, 10:00:00",10000,0.6612,-6612,-2,\n'


def test_forex_commission_currency_comes_from_the_header(tmp_path):
    statement = _parse(tmp_path, _FOREX_HEADER.format(comm="Comm in AUD") + _FOREX_ROW)
    (trade,) = statement.trades
    assert (trade.commission, trade.commission_currency) == (Decimal("-2"), "AUD")


def test_forex_without_commission_currency_column_rejected(tmp_path):
    with pytest.raises(StatementError, match="'Comm in <currency>' column"):
        _parse(tmp_path, _FOREX_HEADER.format(comm="Comm/Fee") + _FOREX_ROW)


def test_unsupported_asset_category_rejected(tmp_path):
    with pytest.raises(StatementError, match="unsupported trade asset category 'Warrants'"):
        _parse(tmp_path, _TRADES_HEADER + (
            'Trades,Data,Order,Warrants,USD,XYZ,"2026-06-15, 10:00:00",1,5,500,-1,\n'
        ))


def test_execution_level_trade_rows_rejected(tmp_path):
    # D58: never seen in a real statement; beside an Order row they would double-count.
    with pytest.raises(StatementError, match="export it with trades at order level"):
        _parse(tmp_path, _TRADES_HEADER + (
            'Trades,Data,Order,Stocks,USD,XYZ,"2026-06-15, 10:00:00",1,5,-5,-1,\n'
            'Trades,Data,Trade,Stocks,USD,XYZ,"2026-06-15, 10:00:00",1,5,-5,-1,\n'
        ))


def test_unknown_discriminator_rejected(tmp_path):
    with pytest.raises(StatementError, match="unknown Trades DataDiscriminator 'Wat'"):
        _parse(tmp_path, _TRADES_HEADER + (
            'Trades,Data,Wat,Stocks,USD,XYZ,"2026-06-15, 10:00:00",1,5,500,-1,\n'
        ))


def test_section_aggregate_rows_skipped(tmp_path):
    statement = _parse(tmp_path, (
        "Commission Adjustments,Header,Currency,Date,Description,Amount,Code\n"
        "Commission Adjustments,Data,USD,2026-06-18,Commission Computed After Trade "
        "Reported (XYZ),-0.3079,\n"
        "Commission Adjustments,Data,Total,,,-0.3079,\n"
    ))
    (txn,) = statement.cash_txns
    assert txn.kind is CashKind.COMMISSION_ADJUSTMENT
    assert txn.amount == Decimal("-0.3079")


def test_missing_cash_report_rejected(tmp_path):
    path = tmp_path / "statement.csv"
    path.write_text(_PREAMBLE.split("Cash Report")[0], encoding="utf-8")
    with pytest.raises(StatementError, match="no Cash Report"):
        parse_statement(path)
