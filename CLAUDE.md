# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Converts an Interactive Brokers **Activity Statement** CSV (multi-section export) into
**per-currency** transaction CSVs in the Xero bank-statement import format
(`templates/StatementImportTemplate.csv`). Requirements and milestones live in `TODO.md`.

The `examples/` folder (a real statement `U*_<start>_<end>.csv` plus reference outputs
`AUD.csv`/`USD.csv`, regenerated 2026-07-03 from verified output — the hand-made
originals are kept as `*.csv.orig`) contains **real account data and is gitignored —
never commit it or quote its contents** in committed files. Tests discover the statement
file by glob and skip if the folder is absent.

## Commands

```
pip install -e .[dev]          # install package + pytest
pytest                         # run all tests
pytest tests/test_convert.py -k name   # run a single test
ibkr2xero examples/<statement>.csv -o out/   # run the CLI
```

## Core rule: reject rather than emit wrong output

Per currency, `Starting Cash + Σ(output transactions) = Ending Cash` (from the statement's
`Cash Report` section) **must hold**. Any unexplained residual beyond rounding tolerance,
any unknown Cash Report component with a nonzero amount, or any unsupported trade asset
category ⇒ raise `ReconciliationError` and write **no output files at all**.
All money arithmetic uses `decimal.Decimal`; floats are forbidden for amounts.

## Architecture

Pipeline in `ibkr_to_xero/`: `parser.py` → `convert.py` → `reconcile.py` → `writer.py`,
driven by `cli.py`. The core is a pure library (no I/O besides parser/writer) so it can later
back a web/Telegram front-end or a Xero API adaptor (see TODO.md M4/M5).

- **parser.py** — IB statements are many CSV sections in one file. Each row is
  `SectionName,Header|Data,...`; a `Header` row defines the column names for the `Data` rows
  after it. A section can re-emit `Header` rows (e.g. `Trades` per asset category) — the parser
  keeps a running current-header per section. First cell carries a UTF-8 BOM.
- **convert.py** — maps in-scope sections (Trades, Deposits & Withdrawals, Fees, Dividends,
  Withholding Tax, Interest) to output rows per currency; builds synthetic rows
  (`MTM`, `ROUNDING` — tagged in the Reference column).
- **reconcile.py** — cross-checks section sums against `Cash Report` components
  (e.g. `Trades (Sales)+Trades (Purchase)+Commissions+GST == Σ trade cash`) and enforces the
  Starting/Ending identity.
- **writer.py** — emits `{CCY}.csv` per currency with activity; quiet currencies get no file.

## IB statement domain knowledge

- Currency rows are identified by a 3-uppercase-letter code; rows with `Total`,
  `Total in USD`, `Base Currency Summary` etc. are aggregates and must be skipped.
  Base currency and `Cash FX Translation Gain/Loss` are out of scope — never convert FX.
- The Cash Report is organised in **blocks** opened by `Starting Cash` rows (base summary
  first, then one block per currency). Rows must be bucketed by block, not by their own
  Currency cell — the base block can contain rows labelled with a real currency (e.g. GST).
- Trade cash impact = `Proceeds + Comm/Fee`. Comm/Fee embeds GST on commissions **and**
  per-trade transaction fees (HK stamp duty); the Cash Report splits these into
  `Commissions`, `GST` and `Transaction Fees` components. The Transaction Fees *section*
  is a breakdown of amounts already inside Comm/Fee — cross-check it, never re-emit it.
- For Australian accounts the Cash Report `GST` component = 10% × trade commissions on
  GST-liable exchanges (embedded in each trade's `Comm/Fee`; 0 for e.g. US exchanges)
  + 10% × fees for services supplied by IBKR Australia itself (withdrawal fees,
  market-data subscriptions — charged as separate cash with **no dated row** in this
  export). Third-party pass-through fees (ADR, dividend handling) attract none. Dated GST
  entries exist only in IB's separate *Statement of Funds* report; the statement's
  GST/HST/PST Details section currently covers Canadian taxes only. Reconciliation
  verifies both parts: embedded must be ≈0 or ≈10% of the `Commissions` component, and
  the unattributed gap must equal 10% of a subset of `Fees` rows (listed in the notes;
  ambiguous subset ⇒ accepted but not itemised) before it becomes the synthetic `GST`
  row. Unverifiable GST ⇒ reject, unless `--accept-unattributed-gst` is passed; the
  envelope check (same sign as, no larger than the component) always applies.
- Only `DataDiscriminator == "Order"` Trades rows are transactions
  (`SubTotal`/`Total` rows are aggregates).
- **Bonds** trade like stocks; coupons and purchase/sale accrued interest arrive as rows
  of the Interest section (`Bond Interest Paid and Received` component).
- **Futures** rows carry `Notional Value`, which never touches cash: only the per-trade
  commission is dated cash; the P/L arrives via `Cash Settling MTM` (one synthetic line,
  period end, tagged `MTM`).
- **Forex** trades are transfers between currency accounts: pair `BASE.QUOTE`, base leg =
  `Quantity`, quote leg (= row's Currency) = `Proceeds`, both tagged `FX` with a shared
  description; commission is charged in USD (`Comm in USD` column) → USD row tagged `FX-FEE`.
- **Corporate actions** (splits, mergers, ISIN changes) move shares and sometimes cash
  (`Proceeds`); their cash flows through the `Trades (Sales)/(Purchase)` components. All
  rows are emitted verbatim, tagged `CORP` (mostly 0-amount).
- Cash Report collateral lines (`Starting/Ending Collateral Value`,
  `Net Securities Lent Activity`, `Net (Settled) Cash Balance`) are SYEP information, not
  cash flow — ignored.
- Output descriptions — one grammar for all trades:
  `{+|-}{qty} {symbol} [({event})] price: {price} comm: {comm 2dp} [({qualifiers})]`.
  Zero fields are omitted (expiries show no price, free trades no comm); events come from
  the Trades `Code` column (`A` → `assigned`, `Ep` → `expired`; O/C/P stay silent);
  futures and forex-fee rows say `commission: {amt}` instead; forex legs share one
  description. Qualifiers inside one parenthesis after the comm: `incl. GST` is appended
  by reconcile.py only after the embedded-GST check verifies 10%-of-Commissions for that
  currency; `incl. stamp duty {amt}` comes from nonzero Transaction Fees rows joined on
  currency+symbol+date/time — a nonzero row must match exactly one trade or the input is
  rejected (zero rows are skipped; their Symbol cell is an exchange option code that
  matches no trade). The instrument type goes to the **Reference column**: `STOCK`/`BOND`/`OPTION`/
  `FUTURE`, alongside the existing `FX`/`FX-FEE`/`CORP`/`MTM`/`GST`/`ROUNDING` tags.
  All other sections pass the statement description through verbatim. `Payee` is always
  `Interactive Brokers`.
