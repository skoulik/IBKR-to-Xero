import datetime as dt
from decimal import Decimal

import pytest

from ib_connector import StatementError, convert
from ib_connector.model import SectionRow, Statement


def test_currencies(statement):
    converted = convert(statement)
    # HKD has cash but no activity: it must produce no transactions at all.
    assert set(converted) == {"AUD", "USD"}


def test_trade_cash_is_proceeds_plus_commission(statement):
    converted = convert(statement)
    aud_trades = [r for r in converted["AUD"] if r.source_section == "Trades"]
    # -1xCSL 16JUL26 130 C: proceeds 32, comm/fee -3.091
    sold_call = next(r for r in aud_trades if r.description == "-1xCSL 16JUL26 130 C")
    assert sold_call.amount == Decimal("28.909")
    assert sold_call.date == dt.date(2026, 6, 29)


def test_stock_trade_description(statement):
    converted = convert(statement)
    descriptions = {r.description for r in converted["USD"]}
    assert "-100 ADP price: 217.5 comm: -0.47" in descriptions  # comm rounded to 2dp
    assert "100 ACN price: 148 comm: 0" in descriptions


def test_option_expiry_is_zero_cash(statement):
    converted = convert(statement)
    expiry = next(r for r in converted["AUD"] if r.description == "1xCSL 04JUN26 91 P")
    assert expiry.amount == 0


def test_simple_sections_pass_description_through(statement):
    converted = convert(statement)
    aud = {r.description: r for r in converted["AUD"]}
    disbursement = next(d for d in aud if d.startswith("Disbursement Initiated by"))
    assert aud[disbursement].amount == Decimal("-60000")
    assert aud["AUD Debit Interest for May-2026"].amount == Decimal("-1108.33")


def _statement_with_trade(category: str) -> Statement:
    header = [
        "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Date/Time",
        "Quantity", "T. Price", "Proceeds", "Comm/Fee",
    ]
    values = dict(zip(header, [
        "Order", category, "USD", "XYZ", "2026-06-15, 10:00:00", "1", "5", "500", "-1",
    ]))
    return Statement(
        sections={"Trades": [SectionRow("Trades", values, 1)]},
        period_start=dt.date(2026, 6, 1),
        period_end=dt.date(2026, 6, 30),
    )


def test_unsupported_asset_category_rejected():
    with pytest.raises(StatementError, match="unsupported trade asset category 'Futures'"):
        convert(_statement_with_trade("Futures"))


def test_unknown_discriminator_rejected():
    statement = _statement_with_trade("Stocks")
    statement.rows("Trades")[0].values["DataDiscriminator"] = "Wat"
    with pytest.raises(StatementError, match="unknown Trades DataDiscriminator 'Wat'"):
        convert(statement)
