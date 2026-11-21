# IBKR-to-Xero — Requirements & Milestones

## Purpose

Convert Interactive Brokers account data into **per-currency** transaction CSVs in Xero's
bank-statement import format (`templates/StatementImportTemplate.csv`). Two sources: an
**Activity Statement** CSV downloaded from Client Portal, converted one at a time; and
**Activity Flex Query** reports fetched from IBKR's Flex Web Service into an encrypted local
store, from which whatever has not been imported yet is exported (M-FLEX). How it is built is
[ARCHITECTURE.md](ARCHITECTURE.md); how each decision was reached is in [planning/](planning/).

## Hard requirements

- **Reconcile or write nothing.** Per currency, `Starting Cash + Σ(output rows) = Ending Cash`
  from the Cash Report. An unexplained residual, an unknown Cash Report component or an
  unsupported input rejects the whole input with a per-currency report, and **no** file is
  written. Wrong output is worse than none.
- **One CSV per currency** with cash activity. Base-currency figures and FX translation
  gain/loss are ignored: nothing is converted between currencies.
- **`decimal.Decimal` for all money**, never float.

## Open

- [ ] **M-FLEX — Flex fetch into a local store** ([plan](planning/2026-11-11-flex-store-plan.md)).
      Steps 1–4 are done: the typed model, the Flex front end and validator, the Flex
      client, the store.
  - [ ] Step 5 — the CLI: fetch, re-fetch, export, confirm, accounts
        ([D15](planning/2026-11-11-flex-store-plan.md#d15)).
  - [ ] Step 6 — the GUI, then a release (D15).
  - [ ] Before that release: the GUI and the CLI in one folder over shared files
        ([D56](planning/2026-11-14-earlier-decisions.md#d56); AD-069, AD-076, AD-077);
        `keyring` checked in the frozen build on both platforms, once a CLI command opens a
        store.
  - [ ] Once step 5 is done: delete the local `examples/flex-prototype/`.
- [ ] **Waiting on real data** — each built when an example arrives:
  - Multi-account Flex reports ([D20](planning/2026-11-11-flex-store-plan.md#d20));
    rejected until then.
  - More asset classes — warrants, structured products, CFDs, mutual funds. The converter
    rejects a statement holding one, which is the signal.
  - A forex commission on a non-USD-base account: its currency is read from the
    `Comm in XXX` column's name, unverified on such a statement.
- [ ] **Activity Statement GST as the Flex path has it.** Where the attribution to fee rows
      is unambiguous, fold the GST into each fee row, as Flex reports do
      ([D19](planning/2026-11-11-flex-store-plan.md#d19)), instead of the period-end `GST`
      row; or accept IB's *Statement of Funds* as a companion input, which dates every GST
      charge ([research](planning/2026-07-03-unattributed-gst-research.md)). Undecided.
- [ ] **M6 — Release engineering, what remains:** the Developer ID certificates, their
      fingerprints in `release.yml` and the `MACOS_CERT_*` secrets renewed before 2027-12-09.
- [ ] **M4 — Hosting** *(future)*: a web or Telegram front end over `engine.run`, or over the
      M-FLEX store.
- [ ] **M5 — Xero adaptor** *(future)*: push to Xero's API instead of a CSV import; Sergei's
      own setup, a starting point, is [D4](planning/2026-11-11-flex-store-plan.md#d4).

## Not pursued

- **Several statements in one run** (2026-07-03): it would blur the per-statement
  all-or-nothing guarantee for little gain; the tool runs once per statement. The
  continuity check it would have brought is part of M-FLEX.
- **Activity Statement CSVs in the store**: they carry no transaction IDs, so they cannot be
  merged with Flex data reliably.
- **A CHANGELOG** (2026-11-21): the commit history is the changelog, and releases use
  `gh --generate-notes`.
- **M3.2, richer error reporting** (2026-11-21): dropped, with no concrete case behind it.
- **A Linux build** ([D51](planning/2026-11-14-earlier-decisions.md#d51)), **Windows code
  signing** ([D50](planning/2026-11-14-earlier-decisions.md#d50)), **pipx or PyPI** — a
  source install is `pip install -e .`.

## Done

- **M0–M2** — scaffolding, the Activity Statement parser, conversion, reconciliation, the
  writer and the CLI.
- **M3** — futures, forex, bonds, corporate actions, transaction fees, SYEP lines.
  **M3.1** — unattributed GST verified against fee rows. **M3.4** — a forex commission's
  currency from its column. **M3.5** — one grammar for trade descriptions.
- **M-GUI** — the desktop window over `engine.run`
  ([plan](planning/2026-07-08-gui-companion-plan.md)).
- **M6** — MIT licence; the version in `pyproject.toml`, which a release tag must match;
  installers built on demand as draft releases — a signed and notarised macOS package, an
  Inno Setup installer for Windows; identifiers on `com.skoulik`; the tests on every push to
  `main` and every pull request, on Linux, macOS and Windows.
- **v0.2.0** published 2026-11-11.
