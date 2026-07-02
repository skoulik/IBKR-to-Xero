"""Full-financial-year statement: forex, futures, bonds, corporate actions,
transaction fees embedded in trade commissions, and unattributed GST."""

from decimal import Decimal

from ibkr_to_xero import convert, reconcile


def _results(fy_statement):
    return {r.currency: r for r in reconcile(fy_statement, convert(fy_statement))}


def test_reconciles_all_currencies(fy_statement):
    assert sorted(_results(fy_statement)) == ["AUD", "GBP", "HKD", "USD"]


def test_rows_sum_to_cash_delta(fy_statement):
    cent = Decimal("0.01")
    for result in _results(fy_statement).values():
        total = sum(r.amount for r in result.rows)
        assert total == result.ending_cash.quantize(cent) - result.starting_cash.quantize(cent)


def test_forex_legs_mirror_each_other(fy_statement):
    results = _results(fy_statement)
    aud_legs = [r for r in results["AUD"].rows if r.reference == "FX"]
    usd_legs = [r for r in results["USD"].rows if r.reference == "FX"]
    # Every AUD.USD trade has one leg in each file with the same description.
    aud_descriptions = {r.description for r in aud_legs if "AUD.USD" in r.description}
    usd_descriptions = {r.description for r in usd_legs if "AUD.USD" in r.description}
    assert aud_descriptions == usd_descriptions
    # Base side sums to the net AUD bought via forex over the year.
    assert sum(r.amount for r in aud_legs) == Decimal("250000")


def test_forex_fee_rows(fy_statement):
    results = _results(fy_statement)
    fees = [r for r in results["USD"].rows if r.reference == "FX-FEE"]
    assert fees, "forex commissions (charged in USD) must appear as FX-FEE rows"
    assert all(f.amount == Decimal("-2") for f in fees)
    # No other currency file may carry FX-FEE rows.
    for currency, result in results.items():
        if currency != "USD":
            assert not [r for r in result.rows if r.reference == "FX-FEE"]


def test_futures_emit_commissions_only(fy_statement):
    results = _results(fy_statement)
    futures = [r for r in results["USD"].rows if "futures commission" in r.description]
    assert len(futures) == 6
    assert sum(r.amount for r in futures) == Decimal("-22.23")
    # Notional must never leak into the output (individual trades are ~70k).
    assert all(abs(r.amount) < 10 for r in futures)
    # The futures P/L arrives via the MTM synthetic row instead.
    mtm = [r for r in results["USD"].rows if r.reference == "MTM"]
    assert [r.amount for r in mtm] == [Decimal("-1368")]


def test_corporate_actions(fy_statement):
    results = _results(fy_statement)
    corp_usd = [r for r in results["USD"].rows if r.reference == "CORP"]
    corp_aud = [r for r in results["AUD"].rows if r.reference == "CORP"]
    assert len(corp_aud) == 4  # AMC split + ISIN change, two legs each
    assert all(r.amount == 0 for r in corp_aud)
    # WBA cash-and-stock merger pays USD 11.45 x 100 shares.
    merger = [r for r in corp_usd if r.amount != 0]
    assert len(merger) == 1
    assert merger[0].amount == Decimal("1145")
    assert "Cash and Stock Merger" in merger[0].description


def test_bond_trades_and_interest(fy_statement):
    converted = convert(fy_statement)
    bonds = [r for r in converted["USD"] if "TII 2 3/8" in r.description and "price:" in r.description]
    assert len(bonds) == 2  # buy + sell, same format as stocks
    descriptions = {r.description for r in converted["USD"]}
    assert "Purchase Accrued Interest TII 2 3/8 02/15/55" in descriptions
    assert (
        "Bond Coupon Payment (TII 2 3/8 02/15/55 - United States Treasury TII 2 3/8 02/15/55)"
        in descriptions
    )


def test_gst_synthetic_rows(fy_statement):
    results = _results(fy_statement)
    aud_gst = [r for r in results["AUD"].rows if r.reference == "GST"]
    usd_gst = [r for r in results["USD"].rows if r.reference == "GST"]
    assert [r.amount for r in aud_gst] == [Decimal("-4.50")]
    assert [r.amount for r in usd_gst] == [Decimal("-0.75")]
    # HKD/GBP have no GST component: no synthetic row.
    assert not [r for r in results["HKD"].rows if r.reference == "GST"]
    assert not [r for r in results["GBP"].rows if r.reference == "GST"]


def test_transaction_fees_not_double_counted(fy_statement):
    # HKD stamp duty is embedded in trade Comm/Fee; no separate rows.
    converted = convert(fy_statement)
    assert not [
        r for r in converted["HKD"] if r.source_section == "Transaction Fees"
    ]
