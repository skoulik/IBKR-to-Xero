# IBKR-to-Xero — Requirements & Milestones

## Purpose

Convert an Interactive Brokers **Activity Statement** CSV (multi-section export from IB Portal)
into **per-currency** transaction CSVs in the Xero bank-statement import format
(`templates/StatementImportTemplate.csv`: `*Date,*Amount,Payee,Description,Reference,Cheque Number`).

## Hard requirements

- **Reconciliation is mandatory.** Per currency, from the `Cash Report` section:
  `Starting Cash + Σ(output transactions) = Ending Cash`.
  Any unexplained mismatch, unknown Cash Report component, or unsupported/unknown format
  ⇒ **reject the whole input** with a detailed per-currency report and write **no** output files.
  Wrong output is worse than no output.
- **Sections in scope:** Trades (Stocks, Equity and Index Options — extensible later),
  Deposits & Withdrawals, Fees, Dividends, Withholding Tax, Interest.
- **Per-currency output files** (`AUD.csv`, `USD.csv`, ...). Base-currency figures and
  `Cash FX Translation Gain/Loss` are ignored — no FX conversion.
- **Cash Settling MTM** (futures cash mark-to-market; no per-transaction rows in the statement)
  ⇒ one synthetic line per currency: Description `Cash Settling MTM (futures)`,
  Reference `MTM`, dated at the statement period end.
- **Rounding residual** (from rounding amounts to 2 dp): if within tolerance
  (½ cent × number of rounded rows), append one synthetic line tagged `ROUNDING`;
  if beyond tolerance ⇒ reject.
- Currencies with no cash activity (Starting = Ending, no rows) ⇒ no file.
- All money arithmetic in `decimal.Decimal` — never float.

## Milestones

- [x] **M0 — Scaffolding**: pyproject, `ibkr_to_xero` package, pytest, git init, TODO.md, CLAUDE.md
- [x] **M1 — Parser**: multi-section IB CSV → typed model (`parser.py`, `model.py`)
- [x] **M2 — Converter + reconciliation + writer + CLI**: per-currency CSVs validated
      against `examples/AUD.csv` / `examples/USD.csv`; end-to-end tests
- [x] **M3 — More asset categories** *(full-FY statement)*: futures (commission rows +
      Cash Settling MTM), forex (transfer legs in both currency files, tagged `FX`, USD
      commission tagged `FX-FEE`), bonds (like stocks; coupons/accrued interest via the
      Interest section), corporate actions (all rows tagged `CORP`, cash via the trades
      bucket), transaction fees (embedded in trade Comm/Fee — cross-checked, not re-emitted),
      SYEP collateral lines ignored
- [x] **M3.1 — Investigate unattributed GST**: research (2026-07-03; findings in
      `planning/2026-07-03-unattributed-gst-research.md`) confirmed the Activity Statement
      genuinely does not itemise GST on account fees — dated GST entries exist only in
      IB's separate *Statement of Funds* report, and the GST/HST/PST Details statement
      section currently covers Canadian taxes only. Implemented instead: the unattributed
      GST must verify as exactly 10% GST on a subset of `Fees` rows (contributing rows are
      listed in the run report; ambiguous combinations accepted but not itemised) and the
      embedded part as ≈0 or ≈10% of the `Commissions` component; anything unverifiable
      rejects the input unless `--accept-unattributed-gst` is passed. Future option:
      accept a Statement of Funds export as a companion input so the synthetic period-end
      `GST` line can be replaced by dated rows.
- [x] **M3.5 — Unified trade descriptions** (2026-07-03): one grammar for all trades —
      `{+|-}{qty} {symbol} [({event})] price: {price} comm: {comm} [({qualifiers})]`
      with zero fields omitted, lifecycle events from the `Code` column (`assigned`,
      `expired`), verified `incl. GST` / `incl. stamp duty` commission qualifiers, and the
      instrument type moved to the Reference column (`STOCK`/`BOND`/`OPTION`/`FUTURE`).
      Reference outputs in `examples/` regenerated from verified output (Date/Amount
      proven identical to the hand-made originals, kept as `*.csv.orig`).
- [ ] **M3.2 — Robustness**: richer error reporting. *Multiple statements in one run was
      dropped (2026-07-03): it would blur the per-statement all-or-nothing guarantee and
      the one-statement/one-folder/one-report output model for little gain — users can
      run the tool per statement (or loop in a script). Possible future item instead: a
      cross-statement continuity check (statement N+1 Starting Cash == statement N
      Ending Cash) to catch a missing period — a verification aid, not batch conversion.*
- [ ] **M3.3 — More asset classes**: investigate/add further trade categories such as
      warrants (also structured products, CFDs, mutual funds as they come up). No example
      CSV available yet — the strict converter will reject the statement when one first
      appears, which is the signal to add support.
- [ ] **M3.4 — Forex commission currency**: the Trades Forex header says `Comm in USD`,
      but the account's base currency *is* USD, so it may really mean "Comm in *base
      currency*" with a dynamic header. The base currency is available in the statement
      (`Account Information` → `Base Currency`). When a non-USD-base example exists,
      check whether the column is named after the base currency and route the commission
      row to the base-currency file instead of hardcoding USD
      (`ibkr_to_xero/convert.py`, `_convert_forex_trade`).
- [ ] **M4 — Hosting** *(future)*: web (e.g. FastAPI) and/or Telegram bot front-end reusing the
      same library core; drag-n-drop/upload or direct fetch from IB
- [ ] **M5 — Xero adaptor** *(future)*: push results straight to Xero via API instead of CSV import
- [ ] **M6 — Distribution & release engineering**
  - [x] **LICENSE**: MIT, added 2026-07-03 (`LICENSE`, `license` in pyproject, README section)
  - [ ] **CI**: GitHub Actions workflow running `pytest` on push/PR (statement-dependent tests
        auto-skip since `examples/` is gitignored; consider a small synthetic fixture statement
        so CI exercises the full pipeline too)
  - [ ] **Versioning**: bump `pyproject.toml` version per release (semver), tag `vX.Y.Z`,
        keep a short CHANGELOG
  - [ ] **Self-contained binaries** for non-tech-savvy users: **on-demand releases only** —
        the workflow must not run on ordinary commits/pushes. Trigger manually
        (`workflow_dispatch`) or by explicitly pushing a `vX.Y.Z` tag when a release is
        decided; it then builds single-file executables (e.g. PyInstaller `--onefile`) on a
        Windows/macOS/Linux matrix and attaches them to the GitHub Release. Core is
        stdlib-only, so bundles should stay small.
  - [ ] Python users keep `pip install -e .` (already documented in README); pipx/PyPI
        publishing deliberately **not** pursued for now

## Input acquisition (stage 1)

Local files provided by the user on the command line. Other channels (upload, drag-n-drop,
IB Flex Query / TWS fetch) are deliberately deferred — see M4.
