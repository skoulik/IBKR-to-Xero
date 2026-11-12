import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import StatementError, convert
from ibkr_to_xero.model import AssetClass, CashKind, fmt_number

from . import builders


# Tests on the real example check properties linking the parsed model to the
# output, never its values: the example is real account data and stays out of
# the repository.

def _cash_trade_pairs(statement):
    """(trade, output row) for every cash trade, matched by output order."""
    converted = convert(statement)
    pairs = []
    for currency, rows in converted.items():
        trades = [t for t in statement.trades if t.currency == currency
                  and t.asset_class not in (AssetClass.FOREX, AssetClass.FUTURE)]
        trade_rows = [r for r in rows if r.source_section == "Trades"
                      and r.reference not in ("FX", "FX-FEE", "FUTURE")]
        assert len(trades) == len(trade_rows)
        pairs += zip(trades, trade_rows)
    assert pairs
    return pairs


def test_currencies(statement):
    # A currency with cash but no activity must produce no transactions at all.
    active = {r.currency for r in statement.trades + statement.cash_txns}
    assert set(convert(statement)) == active
    assert set(statement.cash_report) - active, "fixture lost its quiet currency"


def test_trade_cash_is_proceeds_plus_commission(statement):
    for trade, row in _cash_trade_pairs(statement):
        assert row.amount == trade.proceeds + trade.commission
        assert row.date == trade.when.date()
        assert row.reference == trade.asset_class.value


def test_trade_description_grammar(statement):
    for trade, row in _cash_trade_pairs(statement):
        qty = format(trade.quantity, "f")
        assert row.description.startswith(f"{qty if qty[0] == '-' else '+' + qty} {trade.symbol}")
        assert ("(assigned)" in row.description) == ("A" in trade.codes)
        assert ("(expired)" in row.description) == ("Ep" in trade.codes)
        assert (" price: " in row.description) == bool(trade.price)
        # commission rounded to 2dp; a zero commission omitted entirely
        if trade.commission:
            comm = fmt_number(trade.commission.quantize(Decimal("0.01")))
            assert f" comm: {comm}" in row.description
        else:
            assert " comm: " not in row.description


def test_option_expiry_is_zero_cash(statement):
    expiries = [(t, r) for t, r in _cash_trade_pairs(statement) if "Ep" in t.codes]
    assert expiries, "fixture lost its expiries"
    assert all(r.amount == 0 and r.reference == "OPTION" for _, r in expiries)


def test_cash_transactions_pass_description_through(statement):
    converted = convert(statement)
    for txn in statement.cash_txns:
        assert any(
            r.description == txn.description and r.amount == txn.amount and r.date == txn.date
            for r in converted[txn.currency]
        )


def _only_row(converted, currency="USD"):
    (row,) = converted[currency]
    return row


def _convert_trade(**fields):
    return convert(builders.statement(trades=[builders.trade(**fields)]))


def test_buy_gets_explicit_plus_sign():
    row = _only_row(_convert_trade())
    assert row.description == "+1 XYZ price: 5 comm: -1"
    assert row.reference == "STOCK"


def test_sell_keeps_minus_sign_and_bond_tag():
    row = _only_row(_convert_trade(asset_class=AssetClass.BOND, quantity="-20000",
                                   price="98.75", proceeds="19750", commission="-2"))
    assert row.description == "-20000 XYZ price: 98.75 comm: -2"
    assert row.reference == "BOND"


def test_option_expiry_marker_omits_zero_fields():
    row = _only_row(_convert_trade(asset_class=AssetClass.OPTION, symbol="XYZ 18JUN26 95 C",
                                   price="0", proceeds="0", commission="0", codes="C;Ep"))
    assert row.description == "+1 XYZ 18JUN26 95 C (expired)"
    assert row.reference == "OPTION"
    assert row.amount == 0


def test_assignment_marker():
    row = _only_row(_convert_trade(quantity="100", price="95", proceeds="-9500",
                                   commission="0", codes="A;O"))
    assert row.description == "+100 XYZ (assigned) price: 95"


def test_futures_emit_commission_row_only():
    row = _only_row(_convert_trade(asset_class=AssetClass.FUTURE, symbol="MNOP",
                                   quantity="2", price="6013.25", proceeds=None,
                                   commission="-4.15", codes="C;Ep"))
    assert row.description == "+2 MNOP (expired) commission: -4.15"
    assert row.reference == "FUTURE"
    assert row.amount == Decimal("-4.15")  # notional never touches cash


def test_forex_legs_and_fee_wording():
    converted = _convert_trade(asset_class=AssetClass.FOREX, symbol="AUD.USD",
                               quantity="10000", price="0.6612", proceeds="-6612",
                               commission="-2", commission_currency="USD")
    aud = _only_row(converted, "AUD")
    usd_leg, usd_fee = converted["USD"]
    assert aud.description == usd_leg.description == "+10000 AUD.USD price: 0.6612"
    assert (aud.reference, usd_leg.reference) == ("FX", "FX")
    assert aud.amount == Decimal("10000")
    assert usd_leg.amount == Decimal("-6612")
    assert usd_fee.description == "AUD.USD commission: -2"
    assert usd_fee.reference == "FX-FEE"


def test_forex_pair_must_quote_in_the_trade_currency():
    with pytest.raises(StatementError, match="cannot understand forex pair 'AUD.JPY'"):
        _convert_trade(asset_class=AssetClass.FOREX, symbol="AUD.JPY")


def test_stamp_duty_qualifier_from_transaction_fees():
    statement = builders.statement(
        trades=[builders.trade(commission="-22.15")],
        tax_items=[builders.stamp_duty()],
    )
    row = _only_row(convert(statement))
    assert row.description == "+1 XYZ price: 5 comm: -22.15 (incl. stamp duty -15.66)"


def test_unmatched_nonzero_transaction_fee_rejected():
    statement = builders.statement(
        trades=[builders.trade()],
        tax_items=[builders.stamp_duty(symbol="OTHER")],
    )
    with pytest.raises(StatementError, match="line 9: .* matches 0 trades"):
        convert(statement)


def test_zero_transaction_fee_rows_are_ignored():
    # Zero rows display nothing and often use exchange option codes that
    # match no trade: they must not reject the input.
    statement = builders.statement(
        trades=[builders.trade()],
        tax_items=[builders.stamp_duty(symbol="XYZZA8", amount="0")],
    )
    row = _only_row(convert(statement))
    assert "stamp duty" not in row.description


def test_commission_adjustment_is_a_dated_row_of_its_own():
    # IB prices some commissions after the trade is reported: the trade's
    # Comm/Fee is 0 and the money arrives here instead.
    statement = builders.statement(
        trades=[builders.trade(commission="0")],
        cash_txns=[
            builders.cash_txn(
                kind=CashKind.COMMISSION_ADJUSTMENT,
                date=dt.date(2026, 6, 18),
                description="Commission Computed After Trade Reported (XYZ)",
                amount="-0.3079",
            )
        ],
    )
    trade, adjustment = convert(statement)["USD"]
    assert trade.description == "+1 XYZ price: 5"  # zero comm omitted
    assert adjustment.amount == Decimal("-0.3079")
    assert adjustment.date == dt.date(2026, 6, 18)
    assert adjustment.description == "Commission Computed After Trade Reported (XYZ)"
    assert adjustment.source_section == "Commission Adjustments"


def test_cash_transactions_ordered_by_kind_then_source():
    statement = builders.statement(cash_txns=[
        builders.cash_txn(kind=CashKind.INTEREST, description="interest"),
        builders.cash_txn(kind=CashKind.FEE, description="fee 1"),
        builders.cash_txn(kind=CashKind.DIVIDEND, description="dividend"),
        builders.cash_txn(kind=CashKind.FEE, description="fee 2"),
    ])
    assert [r.description for r in convert(statement)["USD"]] == [
        "fee 1", "fee 2", "dividend", "interest",
    ]
