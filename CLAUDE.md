# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Converts an Interactive Brokers **Activity Statement** CSV (multi-section export) into
**per-currency** transaction CSVs in the Xero bank-statement import format
(`templates/StatementImportTemplate.csv`). Requirements and milestones live in `TODO.md`.

The `examples/` folder (a real statement `U*_<start>_<end>.csv` plus hand-made reference
outputs `AUD.csv`/`USD.csv`) contains **real account data and is gitignored — never commit
it or quote its contents** in committed files. Tests discover the statement file by glob
and skip if the folder is absent.

## Commands

```
pip install -e .[dev]          # install package + pytest
pytest                         # run all tests
pytest tests/test_convert.py -k name   # run a single test
ib-connector examples/<statement>.csv -o out/   # run the CLI
```

## Core rule: reject rather than emit wrong output

Per currency, `Starting Cash + Σ(output transactions) = Ending Cash` (from the statement's
`Cash Report` section) **must hold**. Any unexplained residual beyond rounding tolerance,
any unknown Cash Report component with a nonzero amount, or any unsupported trade asset
category ⇒ raise `ReconciliationError` and write **no output files at all**.
All money arithmetic uses `decimal.Decimal`; floats are forbidden for amounts.

## Architecture

Pipeline in `ib_connector/`: `parser.py` → `convert.py` → `reconcile.py` → `writer.py`,
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
- Trade cash impact = `Proceeds + Comm/Fee` (Comm/Fee already includes GST; the Cash Report
  splits them into `Commissions` and `GST`).
- Only `DataDiscriminator == "Order"` Trades rows are transactions
  (`SubTotal`/`Total` rows are aggregates).
- `Cash Settling MTM` (futures cash mark-to-market) has no per-transaction rows —
  it is delivered as one synthetic output line dated at the statement period end.
- Output descriptions: stocks `{Qty} {Symbol} price: {T.Price} comm: {Comm/Fee 2dp}`;
  options `{Qty}x{Symbol}`; all other sections pass the statement description through verbatim.
  `Payee` is always `Interactive Brokers`.
