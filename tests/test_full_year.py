"""Full-financial-year statement: forex, futures, bonds, corporate actions,
transaction fees embedded in trade commissions, and unattributed GST.

The statement is real account data (gitignored examples/), so these tests
check properties linking the parsed model to the output — never its values.
"""

from decimal import Decimal

from ibkr_to_xero import convert, reconcile
from ibkr_to_xero.model import AssetClass, TaxKind

_CENT = Decimal("0.01")


def _results(fy_statement):
    return {r.currency: r for r in reconcile(fy_statement, convert(fy_statement))}


def _trades(fy_statement, asset_class):
    trades = [t for t in fy_statement.trades if t.asset_class is asset_class]
    assert trades, f"fixture lost its {asset_class.name} trades"
    return trades


def test_reconciles_every_active_currency(fy_statement):
    active = {r.currency for r in fy_statement.trades + fy_statement.cash_txns}
    assert set(_results(fy_statement)) == active


def test_rows_sum_to_cash_delta(fy_statement):
    for result in _results(fy_statement).values():
        total = sum(r.amount for r in result.rows)
        assert total == result.ending_cash.quantize(_CENT) - result.starting_cash.quantize(_CENT)


def test_forex_legs_mirror_each_other(fy_statement):
    results = _results(fy_statement)
    for trade in _trades(fy_statement, AssetClass.FOREX):
        base, _, quote = trade.symbol.partition(".")
        # One leg in each currency's file, sharing a description.
        base_legs = {r.description for r in results[base].rows if r.reference == "FX"}
        quote_legs = {r.description for r in results[quote].rows if r.reference == "FX"}
        shared = {d for d in base_legs & quote_legs if f" {trade.symbol} " in d}
        assert shared
    # Each base currency moves by exactly the quantities traded.
    for base in {t.symbol.partition(".")[0] for t in _trades(fy_statement, AssetClass.FOREX)}:
        expected = sum(t.quantity for t in fy_statement.trades
                       if t.asset_class is AssetClass.FOREX and t.symbol.startswith(base + "."))
        legs = [r for r in results[base].rows if r.reference == "FX"
                and f" {base}." in f" {r.description.split(' ', 1)[1]}"]
        assert sum(r.amount for r in legs) == expected.quantize(_CENT)


def test_forex_fee_rows(fy_statement):
    results = _results(fy_statement)
    charged = [t for t in _trades(fy_statement, AssetClass.FOREX) if t.commission]
    assert charged, "fixture lost its forex commissions"
    for currency, result in results.items():
        fees = [r.amount for r in result.rows if r.reference == "FX-FEE"]
        expected = [t.commission.quantize(_CENT) for t in charged
                    if t.commission_currency == currency]
        assert fees == expected


def test_futures_emit_commissions_only(fy_statement):
    results = _results(fy_statement)
    futures = _trades(fy_statement, AssetClass.FUTURE)
    rows = [r for r in results[futures[0].currency].rows if r.reference == "FUTURE"]
    assert len(rows) == len(futures)
    assert all(" commission: " in r.description for r in rows)
    # Only commissions: notional (and proceeds) never leak into the output.
    assert [r.amount for r in rows] == [t.commission.quantize(_CENT) for t in futures]
    # The futures P/L arrives via the MTM synthetic row instead.
    currency = futures[0].currency
    mtm = [r.amount for r in results[currency].rows if r.reference == "MTM"]
    assert mtm == [fy_statement.cash_report[currency]["Cash Settling MTM"].quantize(_CENT)]


def test_corporate_actions(fy_statement):
    results = _results(fy_statement)
    actions = fy_statement.corporate_actions
    assert actions, "fixture lost its corporate actions"
    assert any(a.proceeds for a in actions), "fixture lost its cash-paying action"
    assert any(not a.proceeds for a in actions), "fixture lost its share-only actions"
    for currency, result in results.items():
        rows = [(r.description, r.amount) for r in result.rows if r.reference == "CORP"]
        assert rows == [(a.description, a.proceeds.quantize(_CENT))
                        for a in actions if a.currency == currency]


def test_bond_trades_and_interest(fy_statement):
    bonds = _trades(fy_statement, AssetClass.BOND)
    converted = convert(fy_statement)
    rows = [r for r in converted[bonds[0].currency] if r.reference == "BOND"]
    assert len(rows) == len(bonds)
    assert all(" price: " in r.description for r in rows)  # same format as stocks
    # Coupons and accrued interest arrive as Interest-section rows.
    descriptions = [r.description for r in converted[bonds[0].currency]]
    assert any(d.startswith("Purchase Accrued Interest ") for d in descriptions)
    assert any(d.startswith("Bond Coupon Payment ") for d in descriptions)


def test_gst_synthetic_rows(fy_statement):
    results = _results(fy_statement)
    with_gst = {c for c, comps in fy_statement.cash_report.items() if comps.get("GST")}
    for currency, result in results.items():
        gst_rows = [r for r in result.rows if r.reference == "GST"]
        if currency not in with_gst:
            assert not gst_rows
            continue
        (gst_row,) = gst_rows
        annotated = [r for r in result.rows if "(excl. GST " in r.description]
        if any("automatically attributed" in n for n in result.notes):
            # Attributed unambiguously: the row is exactly 10% of the named fees,
            # and each fee row names its own share.
            assert annotated
            assert gst_row.amount == sum(
                (r.amount * Decimal("0.1")).quantize(_CENT) for r in annotated
            )
            for r in annotated:
                share = (abs(r.amount) * Decimal("0.1")).quantize(_CENT)
                assert r.description.endswith(f"(excl. GST {share})")
        else:
            assert not annotated
    # The example exercises both outcomes.
    notes = [n for r in results.values() for n in r.notes]
    assert any("automatically attributed" in n for n in notes)
    assert any("multiple fee-row combinations match" in n for n in notes)


def test_transaction_fees_not_double_counted(fy_statement):
    # Stamp duty is embedded in trade commissions; no separate rows.
    converted = convert(fy_statement)
    for rows in converted.values():
        assert not [r for r in rows if r.source_section == "Transaction Fees"]


def test_stamp_duty_shown_inside_commission(fy_statement):
    # Each nonzero stamp-duty item enriches exactly one trade description; the
    # amounts stay inside the commission (display only).
    duties = [i for i in fy_statement.tax_items
              if i.kind is TaxKind.STAMP_DUTY and i.amount]
    assert duties, "fixture lost its stamp duty"
    results = _results(fy_statement)
    for currency, result in results.items():
        shown = [r for r in result.rows if "incl. stamp duty" in r.description]
        assert len(shown) == len([d for d in duties if d.currency == currency])


def test_gst_qualifier_only_where_embedded_gst_verifies(fy_statement):
    # Where the embedded GST verifies as 10% of commissions, every trade row
    # displaying a commission is marked; nowhere else.
    results = _results(fy_statement)
    verified = {c for c, r in results.items()
                if any("= 10% of Commissions component" in n for n in r.notes)}
    assert verified, "fixture lost its verified embedded GST"
    for currency, result in results.items():
        with_comm = [r for r in result.rows
                     if r.source_section == "Trades" and " comm: " in r.description]
        marked = [r for r in with_comm if "incl. GST" in r.description]
        assert marked == (with_comm if currency in verified else [])
