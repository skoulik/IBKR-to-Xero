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
      run the tool per statement (or loop in a script). The cross-statement continuity
      check (period N+1 Starting Cash == period N Ending Cash) is now part of M-FLEX, where
      every fetch is checked against the stored one before it.*
- [ ] **M3.3 — More asset classes**: investigate/add further trade categories such as
      warrants (also structured products, CFDs, mutual funds as they come up). No example
      CSV available yet — the strict converter will reject the statement when one first
      appears, which is the signal to add support.
- [x] **M3.4 — Forex commission currency** (2026-11-12): the Trades Forex header says
      `Comm in USD` on a USD-base account, so IB may charge it in the *base* currency with
      the header naming it (Sergei's expectation). The parser now takes the currency from
      the column's own name (`Comm in XXX`) and rejects a forex row without exactly one
      such column, so either way the fee row lands in the right file. Unverified against a
      non-USD-base statement. A Flex report states each trade's `ibCommissionCurrency`.
- [x] **M-GUI — Desktop GUI companion** (2026-07-08; plan in
      `planning/2026-07-08-gui-companion-plan.md`): a one-shot PySide6 window over the
      same engine. First the pipeline orchestration trapped in `cli.py` (output-dir
      default rule, zero-transaction filtering, report-text assembly) was extracted into a
      front-end-agnostic `engine.run(statement_path, RunOptions) -> RunResult`; the CLI is
      now a thin argv→options→run→print/exit shell. `ibkr_to_xero/gui.py` (`ibkr2xero-gui`
      entry point, optional `[gui]` extra) drives it on a worker thread with Idle/Busy/
      Done/Error states, drag-and-drop, and an overwrite-confirm re-run. M4's web/Telegram
      front-end and the M5 Xero adaptor reuse the same engine — the split is what makes
      them cheap.
- [ ] **M-FLEX — Flex Web Service fetch into a local store** (planned 2026-11-11;
      `planning/2026-11-11-flex-store-plan.md`): fetch from IBKR's Flex Web Service on each
      run into an encrypted per-user store holding several accounts, its periods checked to
      join up by cash, and export not-yet-imported transactions as Xero CSVs, counted once the
      user confirms the import. Engine and CLI first, then the GUI. Flex selects by report
      date, so periods join with no re-fetching; the field mapping and the query validator's
      requirements were established from real exports on 2026-11-12 (the plan's I1, I2).
- [ ] **M4 — Hosting** *(future)*: web (e.g. FastAPI) and/or Telegram bot front-end reusing the
      same library core (the `engine.run` entry point the GUI already uses); drag-n-drop/upload,
      or the M-FLEX store for direct fetch from IB
- [ ] **M5 — Xero adaptor** *(future)*: push results straight to Xero via API instead of CSV import
- [ ] **M6 — Distribution & release engineering**
  - [x] **LICENSE**: MIT, added 2026-07-03 (`LICENSE`, `license` in pyproject, README section)
  - [ ] **CI**: GitHub Actions workflow running `pytest` on push/PR (statement-dependent tests
        auto-skip since `examples/` is gitignored; consider a small synthetic fixture statement
        so CI exercises the full pipeline too). Partly covered: the release workflow already
        gates every build on `pytest`, so a tag cannot ship code that fails the suite —
        but ordinary pushes and PRs still run nothing.
  - [x] **Versioning**: bump `pyproject.toml` version per release (semver), tag `vX.Y.Z`.
        The release workflow refuses to build when the tag and `pyproject.toml` disagree.
        A CHANGELOG is still open; releases currently use `gh --generate-notes`.
  - [x] **Self-contained binaries** for non-tech-savvy users, 2026-08-11
        (`.github/workflows/release.yml` + `packaging/`): on-demand only — a `vX.Y.Z` tag
        builds a **draft** release, `workflow_dispatch` does a dry run that attaches the
        installers to the workflow run instead. Both platforms ship one installer offering
        the GUI, the CLI, or both: Inno Setup on Windows, a `productbuild` package on
        macOS — Apple silicon only since 2026-11-11 (Sergei's call: the Intel job's
        stapling kept losing its connection to Apple, and PySide6 has no universal2 wheels,
        so Intel needed a build of its own; `hostArchitectures="arm64"` makes Installer
        refuse an Intel Mac up front). A `.dmg` was considered
        and declined: a disk image is only a mounted folder, so it cannot install the CLI
        into `/usr/local/bin`, and Gatekeeper treats both formats identically while
        unsigned. The cost is that macOS has no uninstaller (the format provides none);
        the README documents the `rm` + `pkgutil --forget` steps instead.
      - Linux is deliberately **not** built: it would need an AppImage plus a
        glibc-compatible build container to be worth shipping, and Linux users can
        `pip install`. Revisit if anyone asks.
      - **macOS packages are signed and notarised** (2026-11-11), on every run, dry runs
        included: the app and the CLI by PyInstaller (`--codesign-identity`: hardened
        runtime, secure timestamp, inside-out), the product package by `productbuild
        --sign`, then `notarytool` with an App Store Connect API key, `stapler` and an
        `spctl` assessment; a last step runs the CLI and launches the app from the
        package's own payload. The identities are Sergei's Developer ID Application and
        Installer pair (team `Q4KANR5AC3`), selected by SHA-1 in the workflow because older
        certificates share their common names, and valid to 2027-12-09 — Apple issues them
        for about a year, so the fingerprints and the `MACOS_CERT_*` secrets are renewed
        yearly. Without the secrets the workflow fails rather than ship an unsigned
        package; `build-package.sh` alone still builds unsigned when given no identity.
      - **Windows is not code-signed**: SmartScreen warns on first run and the README
        documents the click-through. Clearing it needs an OV certificate on a hardware
        token (a few hundred a year), which is not held.
  - [x] **Identifiers on the `com.skoulik` prefix** (2026-11-11; Sergei's convention since
        2026-10-16): the macOS bundle and package identifiers are
        `com.skoulik.ibkr-to-xero[.gui|.cli]`, the Windows taskbar AppUserModelID
        `skoulik.ibkr2xero`. 0.1.0 installed under `com.sergeikulik.*` receipts; the
        README names only the new ones (Sergei's call). The GUI keeps no preferences, so
        no Qt organisation name is set and nothing else is keyed on either.
  - [ ] **Windows CLI: `--onefile` trips antivirus heuristics** (postponed 2026-08-11).
        Kaspersky deleted the built `ibkr2xero.exe` during a local build; the `--onedir`
        GUI beside it was untouched. The onefile bootloader extracts its DLLs to `%TEMP%`
        and loads them from there, which reads as a dropper to behavioural scanners — and
        it pays that unpack cost on every run. Switching the CLI to `--onedir` under
        `{app}\cli` removes the behaviour (build script, the `[Files]` entry, and `AppPath`
        in the installer's PATH code); macOS would stay onefile. Costs ~35-40 MB installed,
        much less in the installer thanks to solid compression.
  - [ ] Python users keep `pip install -e .` (already documented in README); pipx/PyPI
        publishing deliberately **not** pursued for now

## Input acquisition (stage 1)

Local files provided by the user on the command line or dropped on the GUI. Fetching from
IBKR's Flex Web Service is planned as M-FLEX; upload channels are deferred to M4. TWS and the
Client Portal trading API carry no statements.
