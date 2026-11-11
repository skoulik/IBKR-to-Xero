# Flex Web Service and a local store — plan

*Agreed 2026-11-11. Fetch account data from IBKR's Flex Web Service into a local store
whose date ranges join up, and generate the Xero CSVs from the store instead of from a
hand-downloaded Activity Statement. Same contract as today: reconcile or write nothing.
Every design question is decided (D1–D15); what remains is to investigate (I1, I2), and
that needs a real Flex export.*

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
- **D5 — Backdated transactions: re-fetch a year, only when the cash does not add up.**
  IBKR adds backdated transactions that were not there before, up to about half a year
  back (Sergei's experience). A run normally fetches only the days since the last one. A
  backdated entry that moves cash makes that fetch's Starting Cash disagree with the
  stored Ending Cash; the run then re-fetches the **trailing 365 days** — one request,
  twice the observed horizon — and compares it with the store by transaction ID. New IDs
  are backdated additions and go into the next export; IDs that vanished or changed are
  corrections and are reported. If the window's Starting Cash still disagrees with the
  store's Ending Cash the day before it, the change reaches further back than a year and
  the run rejects, saying so.
  *Accepted gap:* a backdated entry that moves no cash (e.g. a zero-amount corporate
  action) is not noticed. It carries no cash, and Xero discards zero-amount lines on
  import, so the CSVs are unaffected — but share quantities are, which matters once M5
  tracks stock as inventory. Revisit then.
- **D6 — Originals as the record, a database for speed.** Every fetched report is kept
  exactly as received, one immutable file per fetch, self-describing (a Flex report carries
  its own date range and generation time; D14); a re-fetched window (D5) supersedes older
  files for its range without deleting them, so a correction can always be traced. Parsed
  transactions live in an SQLite
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
  imported. *Gap:* a backdated transaction (D5) dated before that mark but never exported
  would be taken as exported; a restore should list the transactions near the mark for the
  user to check against Xero.
- **D8 — Encrypted at rest: the originals and the database.** Python's standard library
  has no symmetric encryption, so this adds a dependency (`cryptography`, AES-GCM); the
  converter itself stays standard-library only. The database cannot be encrypted by the
  standard `sqlite3` (SQLCipher would mean a native build to package and sign), so it is
  held in memory while the tool runs and stored as an encrypted image: decrypted and
  loaded with `Connection.deserialize` on start, written back with `serialize` and
  encrypted on save (both Python 3.11+, already the minimum). At a few MB per account
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
  nothing to flag it. To check against a real export: exactly which currencies the Flex
  Cash Report includes (with no balance and no activity, presumably none — harmless,
  having nothing to export).
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
  Originals are named by a hash of their content, so an identical re-fetch is recognised;
  their date ranges live inside them, so a database rebuild (D6) reads each file to learn
  it. The export record is a file of its own, out of a rebuild's reach. One key covers the
  whole store (D9). **Exported CSVs are not kept here:** the user has to find them to import
  into Xero, so they go to a visible folder of the user's choosing — by default one under
  Documents, a subfolder per export.
- **D15 — Engine and CLI first, then the GUI; released once both have it.** The CLI is the
  quickest way to build and test the engine against real data (the order the GUI companion
  followed); inexperienced users, who need the GUI's guided first run most, never meet a
  CLI-only version. CLI: `ibkr2xero fetch`, `ibkr2xero export [--to DATE]` (D10; a range for
  a re-export), confirming imports, adding and selecting accounts (D7); the existing
  one-statement mode unchanged. GUI, a first sketch (Sergei): a tab per account, and a tab
  for converting a hand-downloaded Activity Statement as today.

## To investigate — needs a real Flex export

These are not decisions but facts to establish, and they gate the engine work. They need
Sergei's own Flex token and an Activity Flex Query (XML) covering every section below. The
token is a credential: it is stored on his machine without passing through a transcript or
a file in the repository, and the fetched reports are real account data, kept in the
gitignored `examples/` like the statements are.

### I1 — How backdated entries look in Flex

Flex rows carry several dates (for cash transactions, at least a transaction date and a
report date); a backdated entry may show as an old transaction date with a recent report
date, which would also tell us whether a date-range request selects by one or the other.
Cross-check Flex fetches against Activity Statement CSVs downloaded for the same periods
(Sergei's suggestion), including an old period fetched before and after a known backdated
entry.

### I2 — Mapping Flex sections onto the converter

Every section the converter reads today needs its Flex counterpart: Cash Report (with its
components, which reconciliation checks against), Trades (stocks, options, futures, bonds,
forex), Transaction Fees, Commission Adjustments, Corporate Actions, and the cash
transactions (deposits and withdrawals, dividends, withholding tax, interest, fees) —
mapped field by field, and proven by converting the same period from both sources to
identical output. Whatever the user's query omits, the tool must detect and reject, naming
the missing section or field — a query configured by IBKR's AI is not guaranteed complete.
Also: which currencies the Flex Cash Report includes (D13).

## Out of scope

- Pushing to Xero (D4, M5).
- Hosting (M4) — although a store fed by Flex is what a hosted or Telegram front end
  would need.
- Feeding hand-downloaded Activity Statement CSVs into the store: different IDs (none),
  so they cannot be merged with Flex data reliably. They remain a separate, one-shot path.
