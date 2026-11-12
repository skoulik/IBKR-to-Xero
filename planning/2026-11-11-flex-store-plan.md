# Flex Web Service and a local store — plan

*Agreed 2026-11-11, revised 2026-11-12 after real Flex exports. Fetch account data from
IBKR's Flex Web Service into a local store whose date ranges join up, and generate the
Xero CSVs from the store instead of from a hand-downloaded Activity Statement. Same
contract as today: reconcile or write nothing. Every design question is decided
(D1–D19), and both facts the engine depended on are established (I1, I2): the engine
work can start.*

## Background

- **The Flex Web Service is IBKR's only API that returns statement data.** The user creates
  a token (Client Portal → Settings → Flex Web Service; expires after 6 hours to 1 year)
  and an *Activity Flex Query* naming the sections and columns wanted (Performance &
  Reports → Flex Queries, or IBKR's "Configure Flex Queries with AI"). `SendRequest`
  (token, query ID, optional `fd`/`td` dates) returns a reference code; `GetStatement`
  returns the report once generated. One request covers at most 365 days. Data refreshes
  once a day at close of business.
- **The output is not the Activity Statement CSV** — XML, or a differently laid-out CSV,
  with its own section and field names. It needs a second front end into the converter.
- **Flex rows carry stable IDs** (trade and transaction IDs), which the Activity Statement
  CSV lacks. That is what makes a store workable: the same transaction arriving in two
  fetches is recognisable, and so is a transaction already exported.
- **A fetch selects rows by report date (I1).** A transaction belongs to the period that
  holds its report date, whatever its own date, so consecutive fetches join with no gap and
  no overlap, and a period once reported never changes.
- **Ranges are checked by cash, not only by dates.** Each fetch has Starting and Ending
  Cash per currency; a fetch's Starting Cash must equal the previous one's Ending Cash
  (TODO M3.2's continuity check, enforced on every run).

## Decided (Sergei, 2026-11-11)

- **D1 — On demand, no scheduler.** The user runs the tool as often as they like; each run
  catches up from the end of the stored data.
- **D2 — Complete days only.** A run fetches up to the last day IBKR has closed, never a
  day still in progress.
- **D3 — Synthetic rows per stored period.** `MTM`, `GST` and `ROUNDING` lines are worked
  out per fetch period, so an export over several periods carries one of each per period.
  Shorter periods also make GST attribution to specific fee rows easier.
- **D4 — Pushing to Xero (M5) is out of scope here.** It is more than a bank feed: a trade
  touches several Xero accounts. Sergei's own setup, for when M5 is planned — and possibly
  the model a full integration will require: each stock is an inventory item with its own
  tracking account, posting to share purchases and share sales; options share one
  inventory account, with purchases and sales posted by manual journal. Many nuances, to
  be discussed then.
- **D5 — Periods join by report date; backdated transactions need no special handling**
  *(revised 2026-11-12)*. Backdated transactions are old-dated rows turning up in a newer
  report (Sergei's experience, confirmed by I1), never an old period rewritten. Since a fetch
  selects by report date, each run fetches from the day after the stored data ends, and a
  backdated row simply arrives in it. **Output rows carry their own dates** (Sergei), as the
  Activity Statement path does: a refund dated a year back is exported dated a year back.
  The cash continuity check stays as a tripwire: if a fetch's Starting Cash ever disagrees
  with the stored Ending Cash, the run rejects, saying so — nothing re-fetches
  automatically. **A manual re-fetch is available** (Sergei): the user names a stored range,
  the tool fetches it again and compares it with the store by transaction ID, and reports
  what was added, removed or changed. Nothing is stored unless the user then confirms; a
  confirmed re-fetch is kept beside the originals it overlaps (D6) and, being newer,
  supersedes them for its range whenever the database is built. A changed transaction
  already exported is listed for the user to correct in Xero by hand.
- **D6 — Originals as the record, a database for speed.** Every fetched report is kept
  exactly as received, one immutable file per fetch, self-describing (a Flex report carries
  its own date range and generation time; D14). Parsed transactions live in an SQLite
  database (`sqlite3`, standard library — no new dependency) for fast queries. **The
  database is derived and disposable:** it records the version of the parsing it was built
  with, and when a release changes that version — a fixed bug, a newly mapped field — the
  tool rebuilds it from the originals.
  *One exception:* the record of what has been exported (D10) cannot be rebuilt from the
  originals, so it lives apart from the derived tables and survives a rebuild — losing it
  would let the next export re-send transactions already in Xero.
- **D7 — A hidden per-user folder holding several accounts.** The store lives in the
  platform's per-user application-data location, not a folder the user picks. It holds
  any number of IB accounts, each kept separately; the CLI and the GUI select which one a
  run works on. The folder's structure is D14.
  *Consequence:* Windows File History does not back up application data by default, so
  the originals there are not a backup in themselves. They can be re-fetched from IBKR,
  year by year. The export record (D6) can be restored approximately from Xero (Sergei):
  the date of the last statement line in each currency's Xero bank account marks what was
  imported. *Gap:* the mark is an own date, while transactions are counted by report date
  (D5) — a backdated transaction reported after the mark but dated before it is ambiguous.
  A restore treats transactions reported after the mark as not imported, and lists the
  ones dated before it for the user to check against Xero.
- **D8 — Compressed, then encrypted at rest: the originals and the database.** Every
  stored file is compressed with `zlib` (standard library) and then encrypted — never the
  other way round, since ciphertext does not compress. Flex XML compresses about 18×
  (a year of an active options account: about 10 MB as received, under 1 MB
  compressed). Python's standard library
  has no symmetric encryption, so this adds a dependency (`cryptography`, AES-GCM); the
  converter itself stays standard-library only. The database cannot be encrypted by the
  standard `sqlite3` (SQLCipher would mean a native build to package and sign), so it is
  held in memory while the tool runs and stored as a compressed, encrypted image:
  decrypted and loaded with `Connection.deserialize` on start, written back with
  `serialize` on save (both Python 3.11+, already the minimum). At a few MB per account
  that costs nothing noticeable.
- **D9 — The key lives in the OS credential store, with no recovery key.** A random key
  per store in Windows Credential Manager or the macOS Keychain, through the `keyring`
  package; nothing to type. It protects copies of the folder and a disk read without the
  user's login, not code running as the user — which could as well take the Flex token
  from the same store and fetch the data from IBKR. The Flex token and query ID of each
  account are kept there too, entered once; an expired token gives a plain message saying
  how to make a new one, and users are told the token can only fetch reports. Known ways
  to lose the key: a password *reset* without the old password (both platforms), a new
  Windows PC (Credential Manager does not migrate; Migration Assistant and Time Machine do
  carry the Mac keychain). Recovery
  deliberately reuses machinery the tool needs anyway: re-fetch the history from IBKR and
  restore the export record from Xero (D7). A recovery key was considered and declined —
  one more secret for the user to keep, for a loss that is rebuildable.
  On macOS a keychain access prompt can appear after an update; signed builds with the
  same identity should not trigger it, builds run from source (as Python) may.
- **D10 — Exports: everything not yet imported, counted once the user confirms.** An export
  takes an end date and by default contains every transaction up to it that has not been
  imported — so a backdated transaction (D5) lands in the next export despite its old
  date, and nothing goes out twice. Written files are *pending* until the user confirms
  they were imported into Xero; the next run asks, and a transaction left unconfirmed is
  exported again. Confirmation is per file, since each currency's CSV goes to its own Xero
  bank account and one can be imported without another. Re-exporting a range regardless
  of the record is always possible. Later (M5) the tool could check Xero itself instead
  of asking.
- **D11 — Flex reports in XML.** Named attributes, a transaction ID on every row, a fixed
  structure to read against; Flex CSV would be a layout of its own to learn anyway, and
  the stored copies are encrypted, so a spreadsheet-readable format buys the user nothing.
- **D12 — The first run asks, per currency, for the last date already in Xero.** The tool
  fetches from the earliest of those dates and treats everything up to each currency's
  date as imported; a shortcut fills every currency with the same date. This is the same
  feature as the restore after a lost key (D7, D9), written and tested once. A user with
  nothing in Xero gives a start date instead and nothing counts as imported. Fetching goes
  back in chunks of up to 365 days, with the cash continuity check across each boundary.
- **D13 — Currencies are discovered by fetching first.** The first run fetches the last 365
  days (one request) and asks about each currency in its Cash Report — those with activity
  or a balance that year. If the earliest date given is further back, it fetches backwards
  to it in 365-day chunks, and a currency appearing only in older data is asked about when
  found. Typed-in currency codes were declined: a missed one would mean a missing CSV with
  nothing to flag it. The Flex Cash Report lists every currency holding a balance, even
  one with no activity in the period, which the Activity Statement leaves out; such a
  currency is asked about too and produces no file.
- **D14 — Folder structure: everything encrypted, nothing readable by name.** Someone
  holding a copy of the folder learns neither which accounts it holds nor what periods —
  only file sizes and times.

  ```
  Windows: %LOCALAPPDATA%\IBKR to Xero\     macOS: ~/Library/Application Support/IBKR to Xero/
    index.enc                       accounts: IB account ID, display name → folder
    <random id>/                    one per IB account
      account.enc                   base currency, currencies and their dates (D12)
      originals/<content hash>.enc  one per fetch (D6)
      data.db.enc                   database image, rebuildable (D6)
      exports.db.enc                export record, not rebuildable (D6, D10)
  ```

  Local rather than roaming application data on Windows, so the data stays on the machine
  whose Credential Manager holds its key (D9); Time Machine includes the macOS folder.
  Originals are named by a hash of their content, which gives unique names that reveal
  nothing — not recognition of a repeated fetch, since every report carries its own
  generation time; their date ranges live inside them, so a database rebuild (D6) reads each file to learn
  it. The export record is a file of its own, out of a rebuild's reach. One key covers the
  whole store (D9). **Exported CSVs are not kept here:** the user has to find them to import
  into Xero, so they go to a visible folder of the user's choosing — by default one under
  Documents, a subfolder per export.
- **D15 — Engine and CLI first, then the GUI; released once both have it.** The CLI is the
  quickest way to build and test the engine against real data (the order the GUI companion
  followed); inexperienced users, who need the GUI's guided first run most, never meet a
  CLI-only version. CLI: `ibkr2xero fetch` (with a range for a manual re-fetch, D5),
  `ibkr2xero export [--to DATE]` (D10; a range for a re-export), confirming imports,
  adding and selecting accounts (D7); the existing
  one-statement mode unchanged. GUI, a first sketch (Sergei): a tab per account, and a tab
  for converting a hand-downloaded Activity Statement as today; adding or editing an
  account has fields for its Flex token and query ID (Sergei, 2026-11-12), written
  straight to the credential store (D9) and never displayed again in full.
- **D16 — A schema validator checks the user's Flex query** *(Sergei, 2026-11-12)*. Every
  fetch is checked before anything is stored: the required sections (table below) present,
  each with its required attributes on every row, the required levels of detail, XML
  format and `yyyyMMdd`/`HHmmss`/`;` date formats; anything missing rejects, naming the
  section, field or option and what to tick. IBKR's AI configurator makes this
  essential: it timed out on "every section", and on a narrower prompt picked the
  sections but only a handful of columns in each. *What the XML cannot show:* a section
  with no rows in the period carries no attributes, so its columns are checked when a row
  first appears; an unticked cash-transaction type (e.g. Commission Adjustments) leaves no
  trace, but its cash is still in the Cash Report, so reconciliation rejects the period.
- **D17 — Descriptions are Flex's own wording** *(Sergei, 2026-11-12)*. Pass-through rows (cash
  transactions, corporate actions) differ from the Activity Statement's: upper case and
  abbreviated (`USD DEBIT INT FOR JAN-2026` for `USD Debit Interest for Jan-2026`; ADR fees
  gain ` - FEE`). No Xero rule matches on them, and reproducing the statement's wording
  would be guesswork. Trade descriptions follow the existing grammar and come out
  identical, except a bond's symbol, which Flex gives without the statement's trailing
  rate (`T 4 1/2 05/15/38`, not `T 4 1/2 05/15/38 0.0%`).
- **D18 — One typed model for both front ends, persisted as the database** *(Sergei,
  2026-11-12)*. The conversion logic is format-independent — trade cash, forex legs and fee,
  futures, event codes, the description grammar, the stamp-duty join, the Cash Report
  checks, MTM and rounding all carried over to Flex unchanged — so `convert` and
  `reconcile` run on a typed model rather than on Activity-Statement-shaped rows, and the
  CSV parser and the Flex parser each produce it. The CSV path builds it in memory as
  today (line numbers as IDs, no report dates); the store persists it. GST is where the
  sources differ: the CSV path keeps inferring it, Flex itemises it.
  `data.db` (derived, rebuildable — D6), amounts as decimal **text** since SQLite has no
  exact decimal type:

  ```
  meta              key, value      -- parser version (rebuild trigger), account, base currency
  period            from_date, to_date, original, generated_at, superseded_by
  cash_report       period, currency, component, amount     -- our component names
  trade             id, period, report_date, datetime, currency, asset_class, symbol,
                    quantity, price, proceeds, commission, commission_currency, taxes, codes
  cash_txn          id, period, report_date, date, currency, kind, description, amount
  corporate_action  id, period, report_date, datetime, currency, description, proceeds
  tax_item          id, period, date, currency, kind (gst|stamp_duty), amount, taxed_id
  line              key, period, currency, date, amount, description, reference, source_id
  ```

  `line` holds the Xero rows `convert` and `reconcile` produce per period, synthetic ones
  included. Its `key` is stable across rebuilds — IBKR's transaction ID, plus a leg for
  forex (`fx-base`, `fx-quote`, `fx-fee`); period and currency for synthetic rows
  (`MTM:20260601-20260630:USD`) — because it is what `exports.db` (not rebuildable) records:

  ```
  export            id, created_at, to_date, folder
  export_file       id, export, currency, filename, status (pending|confirmed), confirmed_at
  exported_line     line_key, export_file
  ```

- **D19 — GST on fees folds into the fee row when the source links it** *(Sergei,
  2026-11-12)*. Flex's GST Details (`SalesTaxes`) names the fee each GST charge was levied
  on (`taxableTransactionID`). Such GST is added to its fee row's amount, and the
  description gains `(incl. GST {amt})` — one line per fee, as trade commissions already
  carry theirs, and what Xero's GST-inclusive coding expects. Reconciliation checks the
  folded GST with the fees and leaves the rest of the GST component to the trades. GST on
  trade commissions is already in the trade's `taxes` and so in its commission (D18);
  its GST Details row names an ID absent from the report, so GST naming nothing is left
  to the existing embedded-GST check (10% of Commissions, the `(incl. GST)` qualifier).
  GST naming a cash transaction that is not a fee rejects. The Activity Statement path
  keeps its inference (subset search, period-end `GST` row); bringing it in line where
  the attribution is unambiguous is open.

## Build order

Each step ends with the full test suite passing and the Activity Statement path's output
unchanged byte for byte against `examples/`.

1. **Typed model.** `convert` and `reconcile` move onto it; the CSV parser produces it.
   No behaviour change. *Done 2026-11-12* (`511a365`).
2. **Flex front end and validator** (D16, the I2 table as one declarative table driving
   both). `parse_statement` recognises a Flex report by content, so the existing CLI and
   GUI convert a fetched XML file; the equivalence with the Activity Statement becomes a
   test. *Done 2026-11-12* (`flex.py`, `tests/test_flex.py`).
3. **Flex client** — request, poll, back off, typed errors; the token a parameter.
   *Done 2026-11-12* (`flex_client.py`, `tests/test_flex_client.py`; checked live).
4. **Store** — `data.db`/`exports.db`, compression and encryption (D8), the key and token
   in `keyring` (D9), the folder layout (D14).
5. **CLI** — fetch (incl. manual re-fetch), export, confirm, accounts (D15).
6. **GUI**, then release (D15).

## Established from real Flex exports (2026-11-12)

Sergei's Activity Flex Query `ibkr2xero` (XML), built by IBKR's AI configurator and
completed by hand (Select All in each section; Account Information and Transfers added).
Reports for the two periods in `examples/` (a month and a financial year) were fetched,
converted through a prototype adaptor into the converter's statement model, and compared
with the Activity Statements for the same periods. The reports are real account data,
kept in the gitignored `examples/flex/`; nothing below quotes them.
Fetching a month took about 5 s; a year about 8 min (one attempt gave up after 10).
Two clients polling one token hit the rate limit (error 1018) and must back off.

### I1 — Backdated entries

- **`fd`/`td` select by report date.** Every row of a fetch has its `reportDate` inside
  the window, while some withholding-tax rows in the financial year carry own dates
  months before it. A row dated in early February of one year and reported in early
  February of the next is in a ten-day fetch around its report date, not in one around its
  own date — which holds the original charge it corrects.
- **What is late:** withholding-tax refunds and corrections, reported 1–12 months after
  their own date, most at the turn of the year. Every other kind of row is reported within
  −1…+3 days of its own date.
- **Cash joins across a boundary:** one month's Ending Cash equals the next month's
  Starting Cash in every currency, and the year reconciles with the late rows inside it.
- **The Activity Statement does the same:** the year's statement, downloaded four months
  before the fetch, holds the same late rows and matches it row for row.

### I2 — Mapping onto the converter

**Proven by conversion:** both periods (several currencies; thousands of rows covering
stocks, options, futures, bonds, forex legs and fees, HK stamp duty, corporate actions, GST
verification and attribution, MTM and rounding) give the same rows, dates,
amounts, Reference tags and report notes from both sources; descriptions differ only as
D17 says. These are also the sections the validator requires (D16):

| Converter needs | Flex section and rows | Fields |
|---|---|---|
| Period, account | `FlexStatement` | `fromDate`, `toDate`, `accountId` |
| Base currency | `AccountInformation` — attributes on the element, no rows | `currency` |
| Cash Report | `CashReport` › `CashReportCurrency`, one per currency (`levelOfDetail="Currency"`); no base summary | one attribute per component: `startingCash`, `endingCash`, `endingSettledCash`, `commissions`, `salesTax` (GST), `transactionTax` (Transaction Fees), `netTradesSales`, `netTradesPurchases`, `deposits`, `withdrawals`, `accountTransfers`, `dividends`, `paymentInLieu`, `withholdingTax`, `brokerInterest`, `bondInterest`, `otherFees`, `cashSettlingMtm`, `fxTranslationGainLoss`; `slb*` are the SYEP collateral lines. Not components: `depositWithdrawals` (deposits + withdrawals), `…Com`/`…Sec` (per-segment splits), `…MTD`/`…YTD` (running totals, on long periods). Any other nonzero attribute is an unknown component ⇒ reject. |
| Trades | `Trades` › `Order` rows (`Trade` rows are its executions) | `assetCategory` STK/OPT/FUT/BOND/CASH; Symbol = `symbol`, but `description` for options (their `symbol` is the exchange's option code, e.g. `XYZQA8`); `dateTime`, `quantity`, `tradePrice`, `proceeds`; Comm/Fee = `ibCommission` + `taxes` (GST on the commission, stamp duty); Code = `notes`; a forex commission is `ibCommission` in `ibCommissionCurrency` |
| Transaction Fees | `TransactionTaxes`, `SUMMARY` rows — one per taxed trade; the statement's zero rows do not appear | `currency`, `symbol`, `date` (a date-time), `taxAmount` |
| Corporate Actions | `CorporateActions` › `DETAIL` | `currency`, `dateTime`, `proceeds`, `actionDescription` |
| Fees, Dividends, Withholding Tax, Interest, Commission Adjustments, Deposits & Withdrawals | `CashTransactions` › `DETAIL`, routed by `type`: Other Fees; Dividends, Payment In Lieu Of Dividends; Withholding Tax; Broker/Bond Interest Paid/Received; Commission Adjustments (none seen yet); Deposits/Withdrawals | `currency`, `dateTime` (date part; `settleDate` for deposits and withdrawals), `description`, `amount`. An unknown `type` ⇒ reject. |
| Internal transfers (Deposits & Withdrawals) | `Transfers` › `TRANSFER` rows with `assetCategory="CASH"`; position transfers carry no cash | `currency`, `date` (its `settleDate` is a day earlier than the statement's), `cashTransfer`. The description is `--`; rebuilt as `Internal Transfer In From Account {account}` (`Out To` unverified). |

**GST Details** (`SalesTaxes`) itemises GST with a date, and each row names the taxed item
by `taxableTransactionID` — fee rows directly, which D19 relies on; GST on a trade
commission names an ID found in no Activity Flex section (Commission Details, tried for it,
links its rows by `tradeID` only and has no transaction ID) but describes the trade
(`AU Goods and Services Tax (Sell 1 XYZQA8)`), and is already inside that trade's `taxes`.
Statement of Funds and Option Exercises/Assignments/Expirations are in the query but
unused.

## Out of scope

- Pushing to Xero (D4, M5).
- Hosting (M4) — although a store fed by Flex is what a hosted or Telegram front end
  would need.
- Feeding hand-downloaded Activity Statement CSVs into the store: different IDs (none),
  so they cannot be merged with Flex data reliably. They remain a separate, one-shot path.
