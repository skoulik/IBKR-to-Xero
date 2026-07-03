import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import ReconciliationError, convert, parse_statement, reconcile
from ibkr_to_xero.model import OutputRow, SectionRow, Statement


def _pipeline(statement):
    return reconcile(statement, convert(statement))


def test_reconciles_and_skips_quiet_currencies(statement):
    results = _pipeline(statement)
    assert [r.currency for r in results] == ["AUD", "USD"]  # HKD: no file


def test_rows_sum_to_cash_delta_after_rounding(statement):
    for result in _pipeline(statement):
        total = sum(r.amount for r in result.rows)
        cent = Decimal("0.01")
        assert total == result.ending_cash.quantize(cent) - result.starting_cash.quantize(cent)


def test_synthetic_mtm_row(statement):
    usd = next(r for r in _pipeline(statement) if r.currency == "USD")
    mtm = [r for r in usd.rows if r.reference == "MTM"]
    assert len(mtm) == 1
    assert mtm[0].amount == Decimal("2575")
    assert mtm[0].date == statement.period_end
    aud = next(r for r in _pipeline(statement) if r.currency == "AUD")
    assert not [r for r in aud.rows if r.reference == "MTM"]


def test_rounding_row_only_when_needed(statement):
    results = {r.currency: r for r in _pipeline(statement)}
    assert not [r for r in results["AUD"].rows if r.reference == "ROUNDING"]
    rounding = [r for r in results["USD"].rows if r.reference == "ROUNDING"]
    assert len(rounding) == 1
    assert abs(rounding[0].amount) <= Decimal("0.005") * (len(results["USD"].rows) + 2)


def _tampered_statement(statement_csv, tmp_path, old: str, new: str):
    text = statement_csv.read_text(encoding="utf-8-sig")
    assert old in text
    tampered = tmp_path / "tampered.csv"
    tampered.write_text(text.replace(old, new), encoding="utf-8")
    return parse_statement(tampered)


def test_tampered_transaction_amount_rejected(statement_csv, tmp_path):
    # Change one AUD interest amount: section sum no longer matches Cash Report.
    statement = _tampered_statement(
        statement_csv,
        tmp_path,
        "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.33",
        "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.34",
    )
    with pytest.raises(ReconciliationError, match="AUD: section 'Interest'"):
        _pipeline(statement)


def test_tampered_ending_cash_rejected(statement_csv, tmp_path):
    statement = _tampered_statement(
        statement_csv,
        tmp_path,
        "Cash Report,Data,Ending Cash,AUD,-352051.388215234,",
        "Cash Report,Data,Ending Cash,AUD,-352061.388215234,",
    )
    with pytest.raises(ReconciliationError, match="AUD: Cash Report does not add up"):
        _pipeline(statement)


def test_unknown_nonzero_component_rejected(statement_csv, tmp_path):
    statement = _tampered_statement(
        statement_csv,
        tmp_path,
        "Cash Report,Data,Withdrawals,AUD,-60000,",
        "Cash Report,Data,Mystery Component,AUD,-60000,",
    )
    with pytest.raises(ReconciliationError, match="unmapped Cash Report component"):
        _pipeline(statement)


# --- GST attribution (synthetic fixtures; reconcile() called directly) -----

_JUN30 = dt.date(2026, 6, 30)


def _gst_statement(components: list[tuple[str, str]]) -> Statement:
    """A minimal one-currency Statement with just a Cash Report."""
    rows = [
        SectionRow(
            "Cash Report",
            {"Currency Summary": name, "Currency": "AUD", "Total": total},
            line_no=i + 1,
        )
        for i, (name, total) in enumerate(components)
    ]
    return Statement(
        sections={"Cash Report": rows},
        period_start=dt.date(2026, 6, 1),
        period_end=_JUN30,
    )


def _fee(amount: str, description: str) -> OutputRow:
    return OutputRow(
        date=dt.date(2026, 6, 10),
        amount=Decimal(amount),
        description=description,
        source_section="Fees",
    )


def _trade(amount: str) -> OutputRow:
    return OutputRow(
        date=dt.date(2026, 6, 10),
        amount=Decimal(amount),
        description="trade",
        source_section="Trades",
    )


def test_gst_attributed_to_all_fee_rows():
    statement = _gst_statement(
        [
            ("Starting Cash", "100"),
            ("Other Fees", "-22"),
            ("GST", "-2.2"),
            ("Ending Cash", "75.8"),
        ]
    )
    converted = {"AUD": [_fee("-15", "Withdrawal Fee"), _fee("-7", "Market data")]}
    (result,) = reconcile(statement, converted)
    gst_rows = [r for r in result.rows if r.reference == "GST"]
    assert len(gst_rows) == 1
    assert gst_rows[0].amount == Decimal("-2.2")
    assert gst_rows[0].date == _JUN30
    assert any("10% of all Fees row(s)" in n for n in result.notes)
    assert any("Withdrawal Fee: -15.00 -> GST -1.50" in n for n in result.notes)
    assert any("Market data: -7.00 -> GST -0.70" in n for n in result.notes)


def test_gst_attributed_to_unique_subset():
    statement = _gst_statement(
        [
            ("Starting Cash", "100"),
            ("Other Fees", "-22"),
            ("GST", "-1.5"),
            ("Ending Cash", "76.5"),
        ]
    )
    converted = {"AUD": [_fee("-15", "Withdrawal Fee"), _fee("-7", "Market data")]}
    (result,) = reconcile(statement, converted)
    assert any("10% of 1 of 2 Fees row(s)" in n for n in result.notes)
    assert any("Withdrawal Fee: -15.00 -> GST -1.50" in n for n in result.notes)
    assert not any("Market data" in n for n in result.notes)


def test_gst_ambiguous_attribution_accepted_but_not_itemised():
    # Two identical fees; the gap is 10% of either one: no honest row list.
    statement = _gst_statement(
        [
            ("Starting Cash", "100"),
            ("Other Fees", "-30"),
            ("GST", "-1.5"),
            ("Ending Cash", "68.5"),
        ]
    )
    converted = {"AUD": [_fee("-15", "Withdrawal Fee"), _fee("-15", "Withdrawal Fee")]}
    (result,) = reconcile(statement, converted)
    assert [r.amount for r in result.rows if r.reference == "GST"] == [Decimal("-1.5")]
    assert any("multiple fee-row combinations match" in n for n in result.notes)
    assert not any("-> GST" in n for n in result.notes)


def test_gst_unverifiable_rejected_unless_flag():
    # Gap -2 cannot be 10% of any subset of a single -15 fee row.
    statement = _gst_statement(
        [
            ("Starting Cash", "100"),
            ("Other Fees", "-15"),
            ("GST", "-2"),
            ("Ending Cash", "83"),
        ]
    )
    converted = {"AUD": [_fee("-15", "Withdrawal Fee")]}
    with pytest.raises(ReconciliationError) as excinfo:
        reconcile(statement, converted)
    message = str(excinfo.value)
    assert "unattributed GST -2.00 cannot be verified" in message
    assert "'Withdrawal Fee' -15.00 (10%: -1.50)" in message

    (result,) = reconcile(
        statement, {"AUD": [_fee("-15", "Withdrawal Fee")]}, accept_unattributed_gst=True
    )
    assert [r.amount for r in result.rows if r.reference == "GST"] == [Decimal("-2")]
    assert any("accepted unverified" in n for n in result.notes)


def test_gst_embedded_must_be_zero_or_ten_percent_of_commissions():
    # Trades bucket expects 100 - 10 - 1.5 = 88.5; actual 89.2 leaves a GST
    # gap of -0.7 (10% of the -7 fee: fine), but the embedded part -0.8 is
    # neither zero nor 10% of Commissions (-1.0).
    components = [
        ("Starting Cash", "0"),
        ("Trades (Sales)", "100"),
        ("Commissions", "-10"),
        ("GST", "-1.5"),
        ("Other Fees", "-7"),
        ("Ending Cash", "81.5"),
    ]
    converted = {"AUD": [_trade("89.2"), _fee("-7", "Market data")]}
    with pytest.raises(ReconciliationError, match="neither zero nor 10% of the Commissions"):
        reconcile(_gst_statement(components), converted)

    (result,) = reconcile(
        _gst_statement(components),
        {"AUD": [_trade("89.2"), _fee("-7", "Market data")]},
        accept_unattributed_gst=True,
    )
    assert any("accepted unverified" in n for n in result.notes)
    total = sum(r.amount for r in result.rows)
    assert total == Decimal("81.5")  # identity still holds via the GST row


def test_gst_fully_embedded_notes_commission_arithmetic():
    statement = _gst_statement(
        [
            ("Starting Cash", "0"),
            ("Trades (Sales)", "100"),
            ("Commissions", "-10"),
            ("GST", "-1"),
            ("Ending Cash", "89"),
        ]
    )
    (result,) = reconcile(statement, {"AUD": [_trade("89")]})
    assert not [r for r in result.rows if r.reference == "GST"]
    assert any("= 10% of Commissions component -10" in n for n in result.notes)


def test_all_errors_reported_at_once(statement_csv, tmp_path):
    # Two independent problems in two currencies: both must be in one report.
    text = statement_csv.read_text(encoding="utf-8-sig")
    text = text.replace(
        "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.33",
        "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.34",
    ).replace(
        "Cash Report,Data,Ending Cash,USD,8253.729513866,",
        "Cash Report,Data,Ending Cash,USD,8263.729513866,",
    )
    tampered = tmp_path / "tampered.csv"
    tampered.write_text(text, encoding="utf-8")
    with pytest.raises(ReconciliationError) as excinfo:
        _pipeline(parse_statement(tampered))
    message = str(excinfo.value)
    assert "AUD: section 'Interest'" in message
    assert "USD: Cash Report does not add up" in message
