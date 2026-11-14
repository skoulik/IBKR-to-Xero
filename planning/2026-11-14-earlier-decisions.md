# Earlier decisions — recorded

*Written 2026-11-14 with Sergei. The decisions behind the converter as built from 2026-07-02 to
2026-11-14, before decisions were numbered, lifted from where they were made — `TODO.md`'s hard
requirements and milestones, the GUI companion plan, the GST research, `CLAUDE.md` and commit
messages — so [ARCHITECTURE.md](../ARCHITECTURE.md) can cite them. Each states what was decided,
when, where it was recorded, and the reason given there; where none was recorded, it says so
rather than supplying one.*

*Decision numbers are one space across all plans: the Flex plan holds D1–D22
([2026-11-11-flex-store-plan.md](2026-11-11-flex-store-plan.md)), this record D23 onwards. The
GUI companion plan's own D1–D3 are D42–D44 here. A new decision, in whichever plan, takes the
next unused number; `tests/test_architecture.py` rejects a number defined twice.*

## Reconciliation and money

- <a id="d23"></a>**D23 — Reconcile, or reject the whole input.** Per currency, Starting Cash plus
  the output's transactions equals Ending Cash from the Cash Report. An unexplained mismatch, an
  unknown Cash Report component or an unsupported or unknown format rejects the whole input, with
  a report of every problem, and writes no file. *Why: "Wrong output is worse than no output."
  (`TODO.md`, hard requirements; 2026-07-02.)*
- <a id="d24"></a>**D24 — Money is `Decimal`, never a float.** *(`TODO.md`, hard requirements;
  2026-07-02. No reason recorded.)*
- <a id="d25"></a>**D25 — One Xero bank-statement CSV per currency.** In the template's columns
  (`*Date,*Amount,Payee,Description,Reference,Cheque Number`), the payee always "Interactive
  Brokers"; a currency without cash activity gets no file. *(`TODO.md`, purpose and hard
  requirements; `CLAUDE.md`; 2026-07-02. Why per currency: each currency's CSV goes to its own
  Xero bank account, as [D10](2026-11-11-flex-store-plan.md#d10) puts it.)*
- <a id="d26"></a>**D26 — No foreign exchange conversion.** Base-currency figures and Cash FX
  Translation Gain/Loss are ignored. *(`TODO.md`, hard requirements; 2026-07-02. No reason
  recorded.)*
- <a id="d27"></a>**D27 — Strict input: support what has been seen, reject the rest.** The
  sections in scope are listed and extended as statements need them; an unknown asset category,
  row kind or transaction type rejects. *Why: "the strict converter will reject the statement
  when one first appears, which is the signal to add support" (`TODO.md` M3.3; 2026-07-02).*
- <a id="d28"></a>**D28 — The hand-made reference outputs define the expected output.** The
  first CSVs were validated against the hand-made `examples/AUD.csv` and `USD.csv`; their
  row order — trades, corporate actions, then cash transactions by kind — is the output's.
  *(`TODO.md` M2; regenerated from verified output 2026-07-03, M3.5, the originals kept.)*
- <a id="d29"></a>**D29 — Cash Settling MTM is one synthetic line.** Futures' cash
  mark-to-market has no per-transaction rows, so it becomes one line per currency, described
  "Cash Settling MTM (futures)", tagged `MTM`, dated the period's end. *(`TODO.md`, hard
  requirements; 2026-07-02.)*
- <a id="d30"></a>**D30 — A rounding residual within tolerance is one synthetic line.** Amounts
  are rounded to the cent; a residual within tolerance becomes a line tagged `ROUNDING`, beyond
  it the input is rejected. *(`TODO.md`, hard requirements; 2026-07-02.)* `TODO.md` gives the
  tolerance as half a cent per rounded row; the code allows two half-cents more, for rounding
  Starting and Ending Cash themselves.

## Statement content

- <a id="d31"></a>**D31 — Futures: commission rows only.** Their notional never moves cash; the
  P/L arrives as the MTM line ([D29](#d29)). *(`TODO.md` M3; 2026-07-02.)*
- <a id="d32"></a>**D32 — Forex trades are transfers between currency accounts.** A leg in each
  currency's file, both tagged `FX`, and the commission as its own row tagged `FX-FEE`, in the
  currency its `Comm in XXX` column names. *(`TODO.md` M3, 2026-07-02; M3.4, 2026-11-12: IB may
  charge it in the base currency, the header naming it — Sergei's expectation.)*
- <a id="d33"></a>**D33 — Bonds trade like stocks.** Coupons and accrued interest arrive through
  the Interest section. *(`TODO.md` M3; 2026-07-02.)*
- <a id="d34"></a>**D34 — Corporate actions are emitted as they are.** Every row, tagged `CORP`,
  mostly with no amount; their cash is checked with the trades. *(`TODO.md` M3; 2026-07-02.)*
- <a id="d35"></a>**D35 — Transaction fees are a breakdown, never extra cash.** Stamp duty is
  inside a trade's commission: the section is cross-checked against its Cash Report component
  and shown on its trade as "(incl. stamp duty …)", never emitted as rows. *(`TODO.md` M3,
  2026-07-02; M3.5, 2026-07-03.)*
- <a id="d36"></a>**D36 — Securities-lending collateral lines are information.** They sit
  outside the cash flow and are excluded from the sums. *(`TODO.md` M3; 2026-07-02.)*
- <a id="d37"></a>**D37 — GST must verify, or the input is rejected.** The part inside trade
  commissions must be zero or 10% of the Commissions component; the rest — GST on account fees,
  which the Activity Statement does not itemise — must equal 10% of a subset of the Fees rows,
  listed in the report, or of several subsets, noted only. Unverifiable GST rejects unless
  `--accept-unattributed-gst` is given. *Why: the Activity Statement holds only the period's
  total; the exact-match condition "keeps the core reject-don't-guess guarantee"
  ([GST research](2026-07-03-unattributed-gst-research.md); `TODO.md` M3.1; 2026-07-03).*
- <a id="d38"></a>**D38 — One grammar for every trade's description.**
  `{+|-}{qty} {symbol} [({event})] price: {price} comm: {comm} [({qualifiers})]`, zero fields
  left out, events from the trade's codes; the instrument type goes to the Reference column.
  *(`TODO.md` M3.5; 2026-07-03.)*
- <a id="d58"></a>**D58 — Trades are read at order level only.** An Activity Statement's
  execution-level `Trade` rows reject the input, asking for order-level detail; they had been
  accepted alongside `Order` rows on an unrecorded assumption that some exports use them.
  *Why: no real statement has shown one, and beside `Order` rows they would count each trade
  twice — rejected as a cash mismatch that names the wrong cause; the Flex side reads `Order`
  rows only* (Sergei, 2026-11-14; [D27](#d27)).

## Runs and output

- <a id="d39"></a>**D39 — One statement per run.** Several statements in one run were dropped:
  *"it would blur the per-statement all-or-nothing guarantee and the
  one-statement/one-folder/one-report output model for little gain"* (`TODO.md` M3.2;
  2026-07-03).
- <a id="d40"></a>**D40 — Output beside the statement, never overwritten unasked.** By default a
  subfolder next to the statement, named after it; an existing output file aborts the whole run
  before anything is written, unless forced. *(Commits `bbf0fd1`, 2026-07-02, and `cf56c85`,
  2026-07-03. No reason recorded for the subfolder.)*
- <a id="d41"></a>**D41 — The run report is an output file.** Saved beside the CSVs by default,
  mirroring the console with bare file names; reserved in the overwrite check and never written
  on rejection. *(Commit `9aa102f`, 2026-07-03.)*

## Front ends

- <a id="d42"></a>**D42 — One engine behind every front end.** The GUI calls a shared library
  entry point rather than wrapping the CLI. *Why: a wrapper would talk through argv, exit codes
  and printed text, which the GUI would have to re-parse; and the core is meant to back other
  front ends (`TODO.md` M4, M5) anyway.* (GUI companion plan, its D1; 2026-07-08.)
- <a id="d43"></a>**D43 — The GUI is PySide6, an optional extra.** *Why: native drag and drop,
  `QTextBrowser`'s rich text and links, HiDPI and threading helpers; optional so a CLI-only
  install does not pull it. Tkinter and pywebview were considered.* (GUI companion plan, its D2;
  2026-07-08.)
- <a id="d44"></a>**D44 — The GUI's entry point is `ibkr2xero-gui`.** A separate GUI script,
  console-less on Windows; the CLI's contract untouched. (GUI companion plan, its D3;
  2026-07-08.)
- <a id="d45"></a>**D45 — The GUI is one-shot and stateless.** Options reset on every launch;
  the report shown verbatim; renaming the report stays a CLI option. *Why: "matching the
  one-shot CLI philosophy"; the report is the same artefact everywhere* (GUI companion plan,
  2026-07-08).
- <a id="d46"></a>**D46 — Zero-amount rows are dropped only on request.** Off by default, and
  only after reconciliation, *"so zero rows (which contribute nothing to the sums) can never mask
  a mismatch"* (commit `0e0c40f`; 2026-07-02).

## Distribution

- <a id="d47"></a>**D47 — Releases are built on demand, as drafts.** A `v*` tag builds a draft
  release; a manual run is a dry run; nothing is published automatically. *(`TODO.md` M6;
  2026-08-11.)*
- <a id="d48"></a>**D48 — One installer per platform.** Inno Setup on Windows; a signed
  `productbuild` package on macOS — a disk image was declined, as it cannot install the CLI into
  `/usr/local/bin`. *(`TODO.md` M6; 2026-08-11.)* It offered the GUI, the CLI or both, built as
  separate programs, until [D56](#d56) replaced that on 2026-11-14.
- <a id="d49"></a>**D49 — macOS on Apple silicon only.** *Why: the Intel job's stapling kept
  losing its connection to Apple, and PySide6 has no universal2 wheels, so Intel needed a build
  of its own* (Sergei; commit `564db7c`; 2026-11-11).
- <a id="d50"></a>**D50 — macOS packages are signed and notarised; the Windows installer is
  not signed.** On every run, dry runs included. *Why Windows is not: an OV certificate on a
  hardware token costs a few hundred a year, which is not held* (`TODO.md` M6; 2026-11-11).
- <a id="d51"></a>**D51 — No Linux build.** *Why: it would need an AppImage and a
  glibc-compatible build container to be worth shipping; Linux users can install from source*
  (`TODO.md` M6; 2026-08-11).
- <a id="d52"></a>**D52 — Identifiers on the `com.skoulik` prefix.** The macOS bundle and
  package identifiers `com.skoulik.ibkr-to-xero[.gui|.cli]`, the Windows taskbar ID
  `skoulik.ibkr2xero`. *(Sergei's convention since 2026-10-16; `TODO.md` M6; 2026-11-11.)*
- <a id="d53"></a>**D53 — `pyproject.toml` holds the version, and the tag must match it.** The
  release workflow refuses a tag that disagrees. *(`TODO.md` M6.)*
- <a id="d54"></a>**D54 — Real statements never enter the repository.** `examples/` is
  gitignored; tests needing it skip without it. *(`CLAUDE.md`; since 2026-07-02.)*
- <a id="d55"></a>**D55 — macOS 14.4 is the floor.** Qt 6.12's own minimum: built lower, it
  loads and then crashes at the first contended mutex. The installer refuses older systems, and
  native builds (DuckDB, [D22](2026-11-11-flex-store-plan.md#d22)) target 14.4. *(Sergei,
  2026-11-14.)*
- <a id="d56"></a>**D56 — The GUI and the CLI share one set of installed files.** One PyInstaller
  build puts both executables in one folder over one shared `_internal` — on macOS both inside
  the `.app`. The program is always installed; the installer offers the GUI's shortcuts and the
  CLI's command — on Windows the folder on `PATH`, on macOS a `/usr/local/bin` link into the
  app. A CLI-only install carries the GUI's Qt libraries; the folder is not split per component.
  *Why: the one-file CLI unpacked everything on every run — more than 50 MB once DuckDB is in
  ([D22](2026-11-11-flex-store-plan.md#d22)) — and its unpacking of libraries into `%TEMP%`
  had Kaspersky delete it as a dropper (`TODO.md` M6, 2026-08-11); a folder build unpacks once,
  at install, and one shared set keeps a single copy of Python and DuckDB. BrokerAI ships the
  same layout, signed and notarised, including the fix for the app being marked background-only
  by its console program.* (Sergei, 2026-11-14.)
