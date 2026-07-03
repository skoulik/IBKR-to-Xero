# Unallocated GST — research findings

*Investigated 2026-07-03 against a real full-FY Activity Statement (period
2025-07-01 → 2026-06-30) and the June-2026 monthly statement. This is a sanitized copy:
account identifiers and exact transaction dates are removed. Aggregate component values
are quoted where they are the evidence.*

## TL;DR

The remaining GST is **genuinely not itemised anywhere in this CSV export** — the string
`GST` appears in exactly 4 rows of the whole file (3 Cash Report component rows + 1
Change in NAV summary row), and no ignored section hides dated GST entries. However, it is
now **fully explained to the last digit**: the GST component = 10% × (AUD trade commissions
+ the GST-liable Fees rows), where "GST-liable" means services supplied by IBKR Australia —
withdrawal fees and market-data subscriptions — but *not* third-party pass-through charges
(ADR fees, dividend-handling fees). Dated GST entries do exist at IBKR, in the **Statement
of Funds** report (a complete dated cash ledger, available in Client Portal and as a Flex
Query), which this Activity Statement export simply does not include.

## Evidence

### 1. Full section inventory (26 sections)

In-scope today: Trades, Deposits & Withdrawals, Fees, Dividends, Withholding Tax, Interest,
Cash Report, Transaction Fees. Ignored and re-read for this investigation: Statement,
Account Information, Net Asset Value, Change in NAV, Mark-to-Market Performance Summary,
Total P/L for Statement Period, Realized & Unrealized Performance Summary, Open Positions,
Forex Balances, Net Stock Position Summary, Corporate Actions, Interest Accruals, Change in
Dividend Accruals, IBKR Stock Yield Enhancement Program Derivatives Activities (+ its
Consideration Summary), Financial Instrument Information, Codes, Notes/Legal Notes.
**None contains dated GST rows.** A word-boundary regex over every row
(`\bGST\b|goods and services|tax invoice|value added|\bVAT\b`) matches only:

```
Change in NAV,Data,GST,-13.827647305
Cash Report,Data,GST,USD,-13.827647305,   <- base-summary block (USD-labelled quirk)
Cash Report,Data,GST,AUD,-19.327,
Cash Report,Data,GST,USD,-0.75,
```

### 2. AUD: GST = 10% × (commissions + withdrawal fees), exact

The AUD Fees section is exactly three `Withdrawal Fee` rows of −15.00 each
(Sep 2025, Dec 2025, Mar 2026 — exact dates redacted).

With Cash Report `Commissions AUD = -148.27`:

    10% × (-148.27 + -45.00) = -19.327  =  GST component   (residual: 0.0000)

The unattributed −4.50 is precisely 10% of the three withdrawal fees (−1.50 each, in the
months above). The embedded −14.827 is 10% of AUD (ASX) trade commissions — note the Cash Report
`Commissions` component is **ex-GST**; the trade rows' `Comm/Fee` carries commission+GST,
which is why the reconciler finds the GST inside the Trades bucket.

### 3. USD: GST = 10% × OPRA market-data net, exact

USD Fees classified (Decimal arithmetic):

| Group | Net | 10% |
|---|---|---|
| OPRA NP L1 subscription (12 charges − 7 cancels) | −7.50 | **−0.750** |
| ADR fees | −29.00 | (no GST) |
| Dividend-handling fees | −13.22 | (no GST) |
| Global Snapshot (every charge exactly reversed) | 0.00 | 0 |

    10% × -7.50 = -0.75  =  GST component   (residual: 0.000)

So GST applies only to IBKR-supplied services (market data subscriptions), not to
depositary/agent pass-through fees. USD trade commissions (non-ASX) attract no GST.

The OPRA rows give the natural dated attribution: −0.15 per monthly charge, +0.15 per
`Cancel[...]` reversal (charge/re-charge pairs land on the same day and net out).

### 4. Monthly statement cross-check (June 2026)

- AUD: `GST = -0.281` = exactly 10% × June `Commissions AUD = -2.81` (no AUD fees in June).
- USD: **no GST row at all** — June OPRA netted 0 (cancel +1.50 / charge −1.50 same day)
  and ADR fees (−3.64) attract none. Matches the model's prediction of 0.
- Base-summary GST −0.19352189 USD ≈ AUD −0.281 converted; the June GST row appears in the
  base block labelled `USD` — the known Cash-Report block-bucketing quirk.

This also pins the timing: GST is assessed **in the same month as the underlying item**,
so per-item same-date attribution is sound at least to monthly granularity.

## Web findings

- IBKR Australia confirms the mechanism but not the placement: "Certain of IBA's services
  will be subject to GST … where GST was payable on a service … this will be disclosed in
  the statements and reports IBKR makes available to you."
  ([Tax FAQs](https://www.interactivebrokers.com.au/en/support/tax-faqs.php),
  [Tax management & reporting](https://www.interactivebrokers.com.au/en/support/tax-management-and-reporting.php) —
  both 403 to scripted fetch; quotes via search snippets.)
- **[Statement of Funds — Default Activity Statement](https://www.ibkrguides.com/reportingreference/reportguide/statementoffunds_default.htm)**:
  "a ledger that displays all cash-related transactions for the statement time period",
  every credit/debit as single dated line items with running balance. Optional section of
  the Default/Legacy Full statements; also a standalone report in Client Portal. Since GST
  is real cash, its dated debits must appear here for the running balance to tie.
- **[Statement of Funds — Flex Query](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm)**:
  same ledger with machine-friendly fields incl. `Activity Code`, `Activity Description`,
  `Debit`, `Credit`, `Balance`, `Currency` — the right vehicle for programmatic dated GST.
- **[GST/HST/PST Details — Default Activity Statement](https://www.ibkrguides.com/reportingreference/reportguide/gsthstpstdetails_default.htm)**:
  a dated, per-item tax section (Date, Description, Taxable Amount, Tax Rate, tax amount)
  — exactly what we'd want — but the docs say it covers *Canadian* sales taxes ("Country –
  currently limited to Canada"). It may or may not ever populate for Australian accounts;
  the converter's `TODO(M3)` should watch for it appearing in exports.
- No public forum/blog discussion of this exact Activity-Statement GST reconciliation gap
  was found; this analysis appears to be new ground.

## Recommendation for the converter

1. **Keep the synthetic GST row and the envelope check** — for this export format the
   period-total truly is all the data there is; rejecting on any mismatch remains right.
2. **Optional enhancement (dated GST heuristic):** when the unattributed gap equals
   exactly 10% of an identifiable subset of Fees rows (`Withdrawal Fee`, market-data
   subscription rows incl. their `Cancel[...]` reversals), emit dated GST rows (10% of
   each such row, same date) instead of one period-end lump; otherwise fall back to the
   synthetic row. The exactness condition keeps the core reject-don't-guess guarantee.
   Caveat: the GST-liable classification is description-based and IB could add new fee
   types; the exact-match guard is what makes it safe.
3. **Structural fix (best):** accept a Statement of Funds export (or Flex Query) as an
   optional companion input to source true dated GST rows — also the natural path for
   TODO M4/M5 automation.
4. **User workaround today:** monthly statements bound each GST charge to its month
   (June proves monthly GST components are exact); or enable/download the Statement of
   Funds report in Client Portal to see the real dated GST debits.
