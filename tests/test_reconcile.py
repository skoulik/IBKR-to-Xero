from decimal import Decimal

import pytest

from ib_connector import ReconciliationError, convert, parse_statement, reconcile


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
