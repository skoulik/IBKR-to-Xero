"""Flex report front end: the D16 validator, the mapping, and equivalence with
the Activity Statement path on the real examples (skipped when absent)."""

import collections
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from ibkr_to_xero import ReconciliationError, StatementError, convert, parse_statement, reconcile
from ibkr_to_xero.flex import CASH_COMPONENTS
from ibkr_to_xero.model import AssetClass, CashKind, TaxKind

from .conftest import EXAMPLES

# --- synthetic reports ---------------------------------------------------------

_SECTIONS = ("AccountInformation", "CashReport", "Trades", "TransactionTaxes",
             "CorporateActions", "CashTransactions", "Transfers", "SalesTaxes")


def _cash_report_row(currency="USD", **amounts):
    attrs = {name: "0" for name in CASH_COMPONENTS}
    attrs.update({k: str(v) for k, v in amounts.items()})
    return ("CashReportCurrency", {"currency": currency, "levelOfDetail": "Currency", **attrs})


def _trade(**fields):
    attrs = {
        "levelOfDetail": "ORDER", "assetCategory": "STK", "currency": "USD",
        "symbol": "XYZ", "description": "XYZ CORP", "dateTime": "20260615;100000",
        "reportDate": "20260615", "quantity": "1", "tradePrice": "5", "proceeds": "-5",
        "ibCommission": "-1", "ibCommissionCurrency": "USD", "taxes": "0", "notes": "",
        "ibOrderID": "900",
    }
    attrs.update(fields)
    return ("Order", attrs)


def _cash_txn(**fields):
    attrs = {
        "levelOfDetail": "DETAIL", "type": "Other Fees", "currency": "USD",
        "dateTime": "20260602", "settleDate": "20260602", "reportDate": "20260602",
        "description": "MARKET DATA FOR JUN 2026", "amount": "-1.5", "transactionID": "100",
    }
    attrs.update(fields)
    return ("CashTransaction", attrs)


def _gst(**fields):
    attrs = {
        "currency": "USD", "date": "20260602", "taxType": "GST", "salesTax": "-0.15",
        "taxableTransactionID": "100", "transactionID": "101",
    }
    attrs.update(fields)
    return ("SalesTax", attrs)


def _report(tmp_path, *, drop=(), statements=1, **sections):
    """Write a Flex report: every required section, rows given per section tag."""
    root = ET.Element("FlexQueryResponse", queryName="test", type="AF")
    container = ET.SubElement(root, "FlexStatements", count=str(statements))
    for _ in range(statements):
        statement = ET.SubElement(container, "FlexStatement", accountId="U0000000",
                                  fromDate="20260601", toDate="20260630")
        for tag in _SECTIONS:
            if tag in drop:
                continue
            if tag == "AccountInformation":
                ET.SubElement(statement, tag, accountId="U0000000", currency="USD")
                continue
            section = ET.SubElement(statement, tag)
            default = [_cash_report_row(startingCash=100, endingCash=100)] if tag == "CashReport" else []
            for row_tag, attrs in sections.get(tag, default):
                ET.SubElement(section, row_tag, attrs)
    path = tmp_path / "report.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


# --- validator (plan D16) ------------------------------------------------------


def test_minimal_report_parses(tmp_path):
    statement = parse_statement(_report(tmp_path))  # recognised as XML by content
    assert statement.account == "U0000000"
    assert statement.base_currency == "USD"
    assert statement.cash_report == {"USD": {"Starting Cash": Decimal(100),
                                             "Ending Cash": Decimal(100)}}


def test_missing_section_rejected(tmp_path):
    with pytest.raises(StatementError, match="section 'Transfers' is missing"):
        parse_statement(_report(tmp_path, drop=("Transfers",)))


def test_missing_field_rejected(tmp_path):
    row_tag, attrs = _cash_txn()
    del attrs["settleDate"]
    with pytest.raises(StatementError, match="'Cash Transactions' lacks field.*settleDate"):
        parse_statement(_report(tmp_path, CashTransactions=[(row_tag, attrs)]))


def test_missing_level_of_detail_rejected(tmp_path):
    # Executions only: the query lacks the Orders option the converter reads.
    execution = ("Trade", {**_trade()[1], "levelOfDetail": "EXECUTION"})
    with pytest.raises(StatementError, match="tick 'Orders'"):
        parse_statement(_report(tmp_path, Trades=[execution]))


def test_wrong_date_format_rejected(tmp_path):
    with pytest.raises(StatementError, match="Date Format to yyyyMMdd"):
        parse_statement(_report(tmp_path, Trades=[_trade(dateTime="2026-06-15, 10:00:00")]))


def test_multi_account_report_rejected(tmp_path):
    with pytest.raises(StatementError, match="exactly one account"):
        parse_statement(_report(tmp_path, statements=2))


def test_unknown_cash_transaction_type_rejected(tmp_path):
    with pytest.raises(StatementError, match="unsupported cash transaction type 'Advisor Fees'"):
        parse_statement(_report(tmp_path, CashTransactions=[_cash_txn(type="Advisor Fees")]))


def test_unknown_cash_report_component_is_passed_to_reconcile(tmp_path):
    row_tag, attrs = _cash_report_row(startingCash=100, endingCash=105)
    statement = parse_statement(_report(tmp_path, CashReport=[(row_tag, {**attrs, "mystery": "5"})]))
    with pytest.raises(ReconciliationError, match="unmapped Cash Report component 'mystery'"):
        reconcile(statement, convert(statement))


def test_running_totals_and_segment_splits_are_not_components(tmp_path):
    row_tag, attrs = _cash_report_row(startingCash=100, endingCash=100)
    attrs.update(otherFeesYTD="-7", otherFeesSec="-7", depositWithdrawals="9")
    statement = parse_statement(_report(tmp_path, CashReport=[(row_tag, attrs)]))
    assert set(statement.cash_report["USD"]) == {"Starting Cash", "Ending Cash"}


# --- mapping ---------------------------------------------------------------------


def test_trade_mapping(tmp_path):
    statement = parse_statement(_report(tmp_path, Trades=[_trade(
        assetCategory="OPT", symbol="XYZQA8", description="XYZ 18JUN26 95 C",
        ibCommission="-2.81", taxes="-0.281", notes="A;O",
    )]))
    (trade,) = statement.trades
    assert trade.asset_class is AssetClass.OPTION
    assert trade.symbol == "XYZ 18JUN26 95 C"  # readable contract, not the exchange code
    assert trade.commission == Decimal("-3.091")  # taxes on the commission included
    assert trade.codes == {"A", "O"}
    assert trade.id == "900" and trade.where == "Flex Order 900"


def test_commission_in_another_currency_rejected_unless_forex(tmp_path):
    with pytest.raises(StatementError, match="only forex commissions may differ"):
        parse_statement(_report(tmp_path, Trades=[_trade(ibCommissionCurrency="AUD")]))
    statement = parse_statement(_report(tmp_path, Trades=[_trade(
        assetCategory="CASH", symbol="AUD.USD", currency="USD", ibCommissionCurrency="AUD")]))
    assert statement.trades[0].commission_currency == "AUD"


def test_cash_transfer_description_rebuilt(tmp_path):
    transfer = ("Transfer", {
        "levelOfDetail": "TRANSFER", "type": "INTERNAL", "direction": "OUT",
        "assetCategory": "CASH", "currency": "USD", "date": "20260605",
        "reportDate": "20260604", "account": "U0000001", "cashTransfer": "-10",
        "transactionID": "200", "description": "--",
    })
    (txn,) = parse_statement(_report(tmp_path, Transfers=[transfer])).cash_txns
    assert txn.kind is CashKind.DEPOSIT_WITHDRAWAL
    assert txn.description == "Internal Transfer Out To Account U0000001"
    assert txn.amount == Decimal("-10")


def test_gst_on_a_fee_is_folded_into_its_row(tmp_path):
    statement = parse_statement(_report(
        tmp_path,
        CashReport=[_cash_report_row(startingCash=100, otherFees="-1.5", salesTax="-0.15",
                                     endingCash="98.35")],
        CashTransactions=[_cash_txn()],
        SalesTaxes=[_gst()],
    ))
    assert [i.kind for i in statement.tax_items] == [TaxKind.GST]
    (result,) = reconcile(statement, convert(statement))
    (fee,) = result.rows
    assert fee.amount == Decimal("-1.65")
    assert fee.description == "MARKET DATA FOR JUN 2026 (incl. GST 0.15)"
    assert not [r for r in result.rows if r.reference == "GST"]
    assert "GST -0.15 on fees folded into their rows" in result.notes


def test_gst_on_a_non_fee_rejected(tmp_path):
    statement = parse_statement(_report(
        tmp_path, CashTransactions=[_cash_txn(type="Dividends", amount="10")], SalesTaxes=[_gst()],
    ))
    with pytest.raises(StatementError, match="only GST on fees"):
        convert(statement)


# --- equivalence with the Activity Statement on the real examples -------------
#
# Real account data: compared through properties, never quoted.

def _example_pairs():
    pairs = []
    if not EXAMPLES.is_dir():
        return pairs
    for csv_path in sorted(EXAMPLES.glob("*.csv")):
        if re.fullmatch(r"[A-Z]{3}", csv_path.stem):
            continue
        csv_statement = parse_statement(csv_path)
        stem = f"flex_{csv_statement.period_start:%Y%m%d}_{csv_statement.period_end:%Y%m%d}_"
        reports = sorted((EXAMPLES / "flex").glob(stem + "*.xml"))
        if reports:
            pairs.append(pytest.param(csv_path, reports[-1], id=stem.strip("_")))
    return pairs


_PAIRS = _example_pairs()
_INCL_GST = re.compile(r" \(incl\. GST (\d+\.\d\d)\)$")


def _results(path):
    statement = parse_statement(path)
    return {r.currency: r for r in reconcile(statement, convert(statement))}


@pytest.mark.skipif(not _PAIRS, reason="no example statement with a Flex report for its period")
@pytest.mark.parametrize("csv_path, flex_path", _PAIRS)
def test_flex_matches_the_activity_statement(csv_path, flex_path):
    csv_results, flex_results = _results(csv_path), _results(flex_path)
    assert set(csv_results) == set(flex_results)
    for currency, csv_result in csv_results.items():
        flex_result = flex_results[currency]
        assert (flex_result.starting_cash, flex_result.ending_cash) == \
            (csv_result.starting_cash, csv_result.ending_cash)

        def rows(result, kinds):
            return [r for r in result.rows if r.source_section in kinds]

        # Trades: same rows; descriptions too, except a bond's symbol, which
        # Flex gives without the statement's trailing rate (plan D17).
        def trade_key(r, with_description):
            described = with_description and r.reference != "BOND"
            return (r.date, r.amount, r.reference, r.description if described else None)

        for with_description in (False, True):
            assert collections.Counter(trade_key(r, with_description) for r in rows(csv_result, {"Trades"})) \
                == collections.Counter(trade_key(r, with_description) for r in rows(flex_result, {"Trades"}))

        # Cash transactions and corporate actions: same dates and amounts;
        # Flex's own wording (D17). Fees carry their GST in Flex (D19).
        def cash_key(r):
            match = _INCL_GST.search(r.description)
            gst = Decimal(match.group(1)) if match else Decimal(0)
            base = r.amount - gst.copy_sign(r.amount) if gst else r.amount
            return (r.source_section, r.date, base)

        others = {k.value for k in CashKind} | {"Corporate Actions"}
        assert collections.Counter(cash_key(r) for r in rows(csv_result, others)) \
            == collections.Counter(cash_key(r) for r in rows(flex_result, others))

        # Synthetic rows: the same MTM; GST is folded into fees rather than a
        # period-end row, so Flex has none.
        def synthetic(result, ref):
            return [r.amount for r in result.rows if r.reference == ref]

        assert synthetic(flex_result, "MTM") == synthetic(csv_result, "MTM")
        assert synthetic(flex_result, "GST") == []
