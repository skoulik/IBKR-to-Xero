import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import ReconciliationError, convert, parse_statement, reconcile
from ibkr_to_xero.model import CashKind, OutputRow, Statement

from . import builders


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
    assert mtm[0].amount == statement.cash_report["USD"]["Cash Settling MTM"]
    assert mtm[0].date == statement.period_end
    aud = next(r for r in _pipeline(statement) if r.currency == "AUD")
    assert not [r for r in aud.rows if r.reference == "MTM"]


def test_rounding_row_only_when_needed(statement):
    results = {r.currency: r for r in _pipeline(statement)}
    assert not [r for r in results["AUD"].rows if r.reference == "ROUNDING"]
    rounding = [r for r in results["USD"].rows if r.reference == "ROUNDING"]
    assert len(rounding) == 1
    assert abs(rounding[0].amount) <= Decimal("0.005") * (len(results["USD"].rows) + 2)


# Tampering edits for the real statement (tests never quote its contents).
_BUMP_AUD_INTEREST = (builders.data_row("Interest", "AUD"), builders.add_to_cell(-1, "-0.01"))
_CASH_REPORT_TOTAL = 4  # Cash Report,Data,{component},{currency},{Total},


def _bump_ending_cash(currency):
    return (builders.data_row("Cash Report", "Ending Cash", currency),
            builders.add_to_cell(_CASH_REPORT_TOTAL, "10"))


def test_tampered_transaction_amount_rejected(statement_csv, tmp_path):
    # Change one AUD interest amount: section sum no longer matches Cash Report.
    tampered = builders.tampered_csv(statement_csv, tmp_path, _BUMP_AUD_INTEREST)
    with pytest.raises(ReconciliationError, match="AUD: section 'Interest'"):
        _pipeline(parse_statement(tampered))


def test_tampered_ending_cash_rejected(statement_csv, tmp_path):
    tampered = builders.tampered_csv(statement_csv, tmp_path, _bump_ending_cash("AUD"))
    with pytest.raises(ReconciliationError, match="AUD: Cash Report does not add up"):
        _pipeline(parse_statement(tampered))


def test_unknown_nonzero_component_rejected(statement_csv, tmp_path):
    def nonzero_aud_flow(cells):
        return (
            cells[:2] == ["Cash Report", "Data"]
            and cells[3] == "AUD"
            and cells[2] not in ("Starting Cash", "Ending Cash", "Ending Settled Cash")
            and Decimal(cells[_CASH_REPORT_TOTAL]) != 0
        )

    def rename(cells):
        return cells[:2] + ["Mystery Component"] + cells[3:]

    tampered = builders.tampered_csv(statement_csv, tmp_path, (nonzero_aud_flow, rename))
    with pytest.raises(ReconciliationError, match="unmapped Cash Report component"):
        _pipeline(parse_statement(tampered))


# --- GST attribution (synthetic fixtures; reconcile() called directly) -----

_JUN30 = dt.date(2026, 6, 30)


def _gst_statement(components: list[tuple[str, str]]) -> Statement:
    """A minimal one-currency Statement with just a Cash Report."""
    return Statement(
        period_start=dt.date(2026, 6, 1),
        period_end=_JUN30,
        cash_report={"AUD": {name: Decimal(total) for name, total in components}},
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
    assert any(
        "unattributed GST -2.20 automatically attributed as 10% of all Fees row(s)" in n
        for n in result.notes
    )
    assert any("Withdrawal Fee: -15.00 -> GST -1.50" in n for n in result.notes)
    assert any("Market data: -7.00 -> GST -0.70" in n for n in result.notes)
    # the attribution is also written onto the fee rows themselves
    descriptions = [r.description for r in result.rows]
    assert "Withdrawal Fee (excl. GST 1.50)" in descriptions
    assert "Market data (excl. GST 0.70)" in descriptions


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
    assert any(
        "unattributed GST -1.50 automatically attributed as 10% of 1 of 2 Fees row(s)"
        in n
        for n in result.notes
    )
    assert any("Withdrawal Fee: -15.00 -> GST -1.50" in n for n in result.notes)
    assert not any("Market data" in n for n in result.notes)
    # only the contributing row is annotated
    descriptions = [r.description for r in result.rows]
    assert "Withdrawal Fee (excl. GST 1.50)" in descriptions
    assert "Market data" in descriptions


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
    assert not any("excl. GST" in r.description for r in result.rows)


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
    assert not any("excl. GST" in r.description for r in result.rows)


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


def _trade_with(description: str, amount: str) -> OutputRow:
    return OutputRow(
        date=dt.date(2026, 6, 10),
        amount=Decimal(amount),
        description=description,
        source_section="Trades",
    )


def test_verified_embedded_gst_marks_commissions_incl_gst():
    # Once the embedded check passes, every trade row that displays a
    # commission gains the qualifier; rows without one stay untouched.
    statement = _gst_statement(
        [
            ("Starting Cash", "0"),
            ("Trades (Sales)", "100"),
            ("Commissions", "-10"),
            ("GST", "-1"),
            ("Ending Cash", "89"),
        ]
    )
    converted = {
        "AUD": [
            _trade_with("-1 XYZ price: 55 comm: -8", "44"),
            _trade_with("+2 QRST price: 20 comm: -3 (incl. stamp duty -2)", "43"),
            _trade_with("+1 WXYZ 18JUN26 95 C (expired)", "2"),
        ]
    }
    (result,) = reconcile(statement, converted)
    descriptions = [r.description for r in result.rows]
    assert "-1 XYZ price: 55 comm: -8 (incl. GST)" in descriptions
    # merges into an existing qualifier parenthesis, GST first
    assert (
        "+2 QRST price: 20 comm: -3 (incl. GST, incl. stamp duty -2)" in descriptions
    )
    # no commission displayed -> no marker
    assert "+1 WXYZ 18JUN26 95 C (expired)" in descriptions


def test_zero_embedded_gst_leaves_descriptions_alone():
    # GST exists but none of it is embedded in trades (fees-only GST):
    # commissions in this currency attract no GST => no qualifier.
    statement = _gst_statement(
        [
            ("Starting Cash", "100"),
            ("Trades (Sales)", "50"),
            ("Commissions", "-10"),
            ("Other Fees", "-15"),
            ("GST", "-1.5"),
            ("Ending Cash", "123.5"),
        ]
    )
    converted = {
        "AUD": [
            _trade_with("-1 XYZ price: 50 comm: -10", "40"),
            _fee("-15", "Withdrawal Fee"),
        ]
    }
    (result,) = reconcile(statement, converted)
    assert any(r.description == "-1 XYZ price: 50 comm: -10" for r in result.rows)
    assert not any("incl. GST" in r.description for r in result.rows)


def test_all_errors_reported_at_once(statement_csv, tmp_path):
    # Two independent problems in two currencies: both must be in one report.
    tampered = builders.tampered_csv(
        statement_csv, tmp_path, _BUMP_AUD_INTEREST, _bump_ending_cash("USD")
    )
    with pytest.raises(ReconciliationError) as excinfo:
        _pipeline(parse_statement(tampered))
    message = str(excinfo.value)
    assert "AUD: section 'Interest'" in message
    assert "USD: Cash Report does not add up" in message


# --- post-trade commission adjustments ------------------------------------


def _commission_adjustment_statement(with_section: bool) -> Statement:
    """One AUD trade whose commission was billed separately afterwards.

    The Cash Report folds the adjustment into the Commissions component, so
    the adjustment row has to be checked inside the trades bucket.
    """
    adjustments = [
        builders.cash_txn(
            kind=CashKind.COMMISSION_ADJUSTMENT,
            currency="AUD",
            date=dt.date(2026, 6, 11),
            description="Commission Computed After Trade Reported (XYZ)",
            amount="-0.31",
            line_no=20,
        )
    ]
    return builders.statement(
        cash_report={
            "AUD": [
                ("Starting Cash", "100"),
                ("Trades (Sales)", "500"),
                ("Commissions", "-1.31"),
                ("Ending Cash", "598.69"),
            ]
        },
        trades=[
            builders.trade(currency="AUD", quantity="-100", price="5", proceeds="500",
                           commission="-1", when=dt.datetime(2026, 6, 10, 10), line_no=10)
        ],
        cash_txns=adjustments if with_section else [],
    )


def test_commission_adjustment_counts_against_commissions_component():
    (result,) = _pipeline(_commission_adjustment_statement(with_section=True))
    assert [r.amount for r in result.rows] == [Decimal("499"), Decimal("-0.31")]
    assert not [r for r in result.rows if r.reference == "ROUNDING"]


def test_missing_commission_adjustment_is_rejected():
    # Sanity check that the trades bucket really is what catches this.
    with pytest.raises(ReconciliationError, match=r"section 'Trades'.*off by 0.31"):
        _pipeline(_commission_adjustment_statement(with_section=False))
