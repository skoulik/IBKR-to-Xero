import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import StatementError, convert
from ibkr_to_xero.model import SectionRow, Statement


def test_currencies(statement):
    converted = convert(statement)
    # HKD has cash but no activity: it must produce no transactions at all.
    assert set(converted) == {"AUD", "USD"}


def test_trade_cash_is_proceeds_plus_commission(statement):
    converted = convert(statement)
    aud_trades = [r for r in converted["AUD"] if r.source_section == "Trades"]
    # sold CSL call: proceeds 32, comm/fee -3.091 (the GST qualifier is
    # appended later, by reconcile, once the embedded check verifies it)
    sold_call = next(
        r for r in aud_trades
        if r.description == "-1 CSL 16JUL26 130 C price: 0.32 comm: -3.09"
    )
    assert sold_call.amount == Decimal("28.909")
    assert sold_call.date == dt.date(2026, 6, 29)
    assert sold_call.reference == "OPTION"


def test_stock_trade_description(statement):
    converted = convert(statement)
    descriptions = {r.description for r in converted["USD"]}
    # comm rounded to 2dp; zero comm omitted entirely
    assert "-100 ADP (assigned) price: 217.5 comm: -0.47" in descriptions
    assert "+100 ACN (assigned) price: 148" in descriptions


def test_option_expiry_is_zero_cash(statement):
    converted = convert(statement)
    expiry = next(
        r for r in converted["AUD"]
        if r.description == "+1 CSL 04JUN26 91 P (expired)"
    )
    assert expiry.amount == 0
    assert expiry.reference == "OPTION"


def test_simple_sections_pass_description_through(statement):
    converted = convert(statement)
    aud = {r.description: r for r in converted["AUD"]}
    disbursement = next(d for d in aud if d.startswith("Disbursement Initiated by"))
    assert aud[disbursement].amount == Decimal("-60000")
    assert aud["AUD Debit Interest for May-2026"].amount == Decimal("-1108.33")


_TRADE_HEADER = [
    "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Date/Time",
    "Quantity", "T. Price", "Proceeds", "Comm/Fee", "Code",
]


def _trade_row(
    category="Stocks", symbol="XYZ", quantity="1", price="5", proceeds="500",
    comm="-1", code="", currency="USD", when="2026-06-15, 10:00:00", line_no=1,
) -> SectionRow:
    values = dict(zip(_TRADE_HEADER, [
        "Order", category, currency, symbol, when, quantity, price, proceeds,
        comm, code,
    ]))
    return SectionRow("Trades", values, line_no)


def _statement(sections: dict[str, list[SectionRow]]) -> Statement:
    return Statement(
        sections=sections,
        period_start=dt.date(2026, 6, 1),
        period_end=dt.date(2026, 6, 30),
    )


def _statement_with_trade(category: str) -> Statement:
    return _statement({"Trades": [_trade_row(category=category)]})


def _only_row(converted, currency="USD"):
    (row,) = converted[currency]
    return row


def test_buy_gets_explicit_plus_sign():
    row = _only_row(convert(_statement_with_trade("Stocks")))
    assert row.description == "+1 XYZ price: 5 comm: -1"
    assert row.reference == "STOCK"


def test_sell_keeps_minus_sign_and_bond_tag():
    statement = _statement(
        {"Trades": [_trade_row(category="Bonds", quantity="-20,000", price="98.75",
                               proceeds="19750", comm="-2")]}
    )
    row = _only_row(convert(statement))
    assert row.description == "-20000 XYZ price: 98.75 comm: -2"
    assert row.reference == "BOND"


def test_option_expiry_marker_omits_zero_fields():
    statement = _statement(
        {"Trades": [_trade_row(category="Equity and Index Options",
                               symbol="XYZ 18JUN26 95 C", price="0", proceeds="0",
                               comm="0", code="C;Ep")]}
    )
    row = _only_row(convert(statement))
    assert row.description == "+1 XYZ 18JUN26 95 C (expired)"
    assert row.reference == "OPTION"
    assert row.amount == 0


def test_assignment_marker():
    statement = _statement(
        {"Trades": [_trade_row(quantity="100", price="95", proceeds="-9500",
                               comm="0", code="A;O")]}
    )
    row = _only_row(convert(statement))
    assert row.description == "+100 XYZ (assigned) price: 95"


def test_futures_emit_commission_row_only():
    statement = _statement(
        {"Trades": [_trade_row(category="Futures", symbol="MNOP", quantity="2",
                               price="6013.25", proceeds="", comm="-4.15",
                               code="C;Ep")]}
    )
    row = _only_row(convert(statement))
    assert row.description == "+2 MNOP (expired) commission: -4.15"
    assert row.reference == "FUTURE"
    assert row.amount == Decimal("-4.15")  # notional never touches cash


def test_forex_legs_and_fee_wording():
    header = _TRADE_HEADER[:8] + ["Comm in USD", "Code"]
    values = dict(zip(header, [
        "Order", "Forex", "USD", "AUD.USD", "2026-06-15, 10:00:00",
        "10,000", "0.6612", "-6612", "-2", "",
    ]))
    converted = convert(_statement({"Trades": [SectionRow("Trades", values, 1)]}))
    aud = _only_row(converted, "AUD")
    usd_leg, usd_fee = converted["USD"]
    assert aud.description == usd_leg.description == "+10000 AUD.USD price: 0.6612"
    assert (aud.reference, usd_leg.reference) == ("FX", "FX")
    assert aud.amount == Decimal("10000")
    assert usd_leg.amount == Decimal("-6612")
    assert usd_fee.description == "AUD.USD commission: -2"
    assert usd_fee.reference == "FX-FEE"


def _transaction_fee_row(symbol="XYZ", amount="-15.66",
                         when="2026-06-15, 10:00:00", currency="USD", line_no=9):
    values = {
        "Asset Category": "Stocks", "Currency": currency, "Date/Time": when,
        "Symbol": symbol, "Description": "Xyz Corp", "Quantity": "1",
        "Trade Price": "5", "Amount": amount, "Code": "",
    }
    return SectionRow("Transaction Fees", values, line_no)


def test_stamp_duty_qualifier_from_transaction_fees():
    statement = _statement(
        {
            "Trades": [_trade_row(comm="-22.15")],
            "Transaction Fees": [_transaction_fee_row()],
        }
    )
    row = _only_row(convert(statement))
    assert row.description == "+1 XYZ price: 5 comm: -22.15 (incl. stamp duty -15.66)"


def test_unmatched_nonzero_transaction_fee_rejected():
    statement = _statement(
        {
            "Trades": [_trade_row()],
            "Transaction Fees": [_transaction_fee_row(symbol="OTHER")],
        }
    )
    with pytest.raises(StatementError, match="matches 0 trades"):
        convert(statement)


def test_zero_transaction_fee_rows_are_ignored():
    # Zero rows display nothing and often use exchange option codes that
    # match no trade: they must not reject the input.
    statement = _statement(
        {
            "Trades": [_trade_row()],
            "Transaction Fees": [_transaction_fee_row(symbol="XYZZA8", amount="0")],
        }
    )
    row = _only_row(convert(statement))
    assert "stamp duty" not in row.description


def test_unsupported_asset_category_rejected():
    with pytest.raises(StatementError, match="unsupported trade asset category 'Warrants'"):
        convert(_statement_with_trade("Warrants"))


def test_unknown_discriminator_rejected():
    statement = _statement_with_trade("Stocks")
    statement.rows("Trades")[0].values["DataDiscriminator"] = "Wat"
    with pytest.raises(StatementError, match="unknown Trades DataDiscriminator 'Wat'"):
        convert(statement)
