# IBKR to Xero — Architecture

| | |
|---|---|
| **Document** | How the converter is built: its modules, the data between them, the rules they enforce, and how it ships |
| **Status** | Current design, maintained with the code: a change to either changes the other in the same commit |
| **Items** | [AD-001](#ad-001) to [AD-118](#ad-118). The next takes **AD-119** |

## Contents

- [Conventions](#conventions)
- [1. Context](#1-context)
- [2. Modules](#2-modules)
- [3. The model](#3-the-model)
- [4. Reading an Activity Statement](#4-reading-an-activity-statement)
- [5. Reading a Flex report](#5-reading-a-flex-report)
- [6. Conversion](#6-conversion)
- [7. Reconciliation](#7-reconciliation)
- [8. Output](#8-output)
- [9. Front ends](#9-front-ends)
- [10. The Flex Web Service client](#10-the-flex-web-service-client)
- [11. The store](#11-the-store)
- [12. Distribution](#12-distribution)

## Conventions

- **Items** are `AD-nnn`, each anchored where it is defined. An item is one element of the
  design that code or configuration realises: a module, an interface, a data structure or
  format, a rule, a setting. It states the design in the present tense, and what it states is
  binding on the code.
- **Decisions.** An item that carries out a decision of a plan in `planning/` names it in its
  Decisions column, and every mention of a decision is a link to its anchor in the plan
  (`[D16](planning/2026-11-11-flex-store-plan.md#d16)`). The plan holds why; this document
  holds what is built.
- **Diagrams** are Mermaid. Every arrow is an item, labelled "text · AD-nnn" on the arrow and
  listed in the table beneath the diagram. An arrow drawn in more than one diagram is one item,
  defined under the first. A dashed node belongs to a later stage: it shows where that stage
  plugs in, and has no arrows or items until then. Every row of a table with an Item column is
  an item.
- **Traceability.** Code, scripts and configuration cite the items they realise in an
  `Implements:` comment — `# Implements: AD-012, AD-031` — at the module, function or setting
  that realises them; they cite a plan's `Dnn` as well where the reason matters to whoever
  edits that code. `tests/test_architecture.py` checks it with the rest of the suite: an
  undefined, duplicated or unlabelled identifier fails the run; an item nothing cites yet is
  reported as a warning, so the design can run ahead of the code.

## 1. Context

```mermaid
flowchart LR
    user([User])
    app["IBKR to Xero<br/>CLI and GUI"]
    statement[/"Statement file<br/>Activity Statement CSV<br/>or Flex report XML"/]
    out[/"Output folder<br/>one CSV per currency<br/>and a report"/]
    ibkr[["IBKR Flex Web Service"]]
    xero[["Xero"]]
    creds[["Credential Manager<br/>or Keychain"]]

    user -->|Runs a conversion · AD-001| app
    app -->|Reads one statement · AD-002| statement
    app -->|Writes, or writes nothing · AD-003| out
    app -->|Fetches a Flex report · AD-004| ibkr
    app -->|Remembers a store's key · AD-080| creds

    style xero stroke-dasharray: 5 5
```

| Item | From | To | What crosses | Decisions |
|---|---|---|---|---|
| <a id="ad-001"></a>**AD-001** | The user | The app | One conversion of one statement, started from the command line ([AD-060](#ad-060)) or the desktop window ([AD-061](#ad-061)), with the options of [AD-059](#ad-059) | [D39](planning/2026-11-14-earlier-decisions.md#d39) |
| <a id="ad-002"></a>**AD-002** | The app | A statement file | The file the user names or drops: an Activity Statement CSV exported from Client Portal, or an Activity Flex Query report in XML; read, never modified | [D27](planning/2026-11-14-earlier-decisions.md#d27) |
| <a id="ad-003"></a>**AD-003** | The app | The output folder | Per currency with cash activity, a Xero bank-statement CSV ([AD-053](#ad-053)), and the run report ([AD-056](#ad-056)) — all of it, or nothing at all ([AD-052](#ad-052), [AD-054](#ad-054)) | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D25](planning/2026-11-14-earlier-decisions.md#d25), [D40](planning/2026-11-14-earlier-decisions.md#d40), [D41](planning/2026-11-14-earlier-decisions.md#d41) |
| <a id="ad-004"></a>**AD-004** | The app | IBKR's Flex Web Service | HTTPS requests carrying the user's Flex token and query ID, and the Flex report returned ([AD-063](#ad-063)) | [D11](planning/2026-11-11-flex-store-plan.md#d11) |
| <a id="ad-080"></a>**AD-080** | The app | The OS credential store — Windows Credential Manager, the macOS Keychain | A store's key, kept when the user ticks *Remember on this computer* and read back in place of asking for the password; one credential per store ([AD-085](#ad-085)) | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D59](planning/2026-11-14-store-modules-plan.md#d59) |

Xero is drawn dashed: the CSVs are imported into Xero by the user, one per currency's bank
account; pushing to Xero directly is a later stage (`TODO.md` M5, [D4](planning/2026-11-11-flex-store-plan.md#d4)).

## 2. Modules

```mermaid
flowchart TD
    cli["cli.py"]
    gui["gui.py"]
    engine["engine.py"]
    parser["parser.py"]
    flex["flex.py"]
    convert["convert.py"]
    reconcile["reconcile.py"]
    writer["writer.py"]
    gst["gst.py"]
    client["flex_client.py"]
    database["database.py"]
    store["store.py"]
    vault["vault.py"]
    locks["locks.py"]
    ibkr[["IBKR Flex Web Service"]]
    creds[["Credential Manager<br/>or Keychain"]]

    cli -->|run · AD-005| engine
    gui -->|run, off the UI thread · AD-006| engine
    engine -->|parse_statement · AD-007| parser
    parser -->|a Flex report · AD-008| flex
    engine -->|convert · AD-009| convert
    engine -->|reconcile · AD-010| reconcile
    reconcile -->|fee_gst · AD-011| gst
    convert -->|fee_gst · AD-105| gst
    engine -->|write_results, write_report · AD-012| writer
    client -->|SendRequest, GetStatement · AD-004| ibkr
    vault -->|a remembered key · AD-080| creds
    store -->|keys, seals, the remembered key · AD-091| vault
    store -->|locked · AD-092| locks
    database -->|read_flex · AD-108| flex
    database -->|convert · AD-109| convert
    database -->|reconcile, at the ledger's rate · AD-110| reconcile
```

Every module imports `model.py` ([AD-013](#ad-013)); those imports are not drawn.

| Item | From | To | What crosses | Decisions |
|---|---|---|---|---|
| <a id="ad-005"></a>**AD-005** | `cli.py` | `engine.py` | `run(statement_path, RunOptions)` → `RunResult`, or a typed error ([AD-059](#ad-059)) | [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-006"></a>**AD-006** | `gui.py` | `engine.py` | The same call, made on a worker thread so the window stays responsive ([AD-061](#ad-061)) | [D42](planning/2026-11-14-earlier-decisions.md#d42), [D43](planning/2026-11-14-earlier-decisions.md#d43) |
| <a id="ad-007"></a>**AD-007** | `engine.py` | `parser.py` | `parse_statement(path)` → `Statement` ([AD-021](#ad-021)) | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-008"></a>**AD-008** | `parser.py` | `flex.py` | `is_flex_report(path)` ([AD-028](#ad-028)), then `parse_flex(path)` → `Statement` for a Flex report | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-009"></a>**AD-009** | `engine.py` | `convert.py` | `convert(Statement)` → currency → `OutputRow`s, unrounded ([section 6](#6-conversion)) | [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-010"></a>**AD-010** | `engine.py` | `reconcile.py` | `reconcile(Statement, rows, gst_rate=, accept_unattributed_gst=)` → `CurrencyResult`s, or `ReconciliationError` ([AD-052](#ad-052)); the engine leaves the rate at its default | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D42](planning/2026-11-14-earlier-decisions.md#d42), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-011"></a>**AD-011** | `reconcile.py` | `gst.py` | `fee_gst(Statement)` → fee ID → GST folded into it, so reconciliation checks that GST with the fees ([AD-051](#ad-051)) | [D19](planning/2026-11-11-flex-store-plan.md#d19) |
| <a id="ad-105"></a>**AD-105** | `convert.py` | `gst.py` | The same `fee_gst(Statement)`, for the GST to fold into each fee's row ([AD-046](#ad-046)) | [D19](planning/2026-11-11-flex-store-plan.md#d19) |
| <a id="ad-012"></a>**AD-012** | `engine.py` | `writer.py` | `write_results(results, folder, overwrite=, report_name=)` and `write_report(folder, name, text)` | [D41](planning/2026-11-14-earlier-decisions.md#d41), [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-091"></a>**AD-091** | `store.py` | `vault.py` | `store.key`'s contents locked, unlocked and checked; files sealed and opened; content names; the remembered key kept, read and forgotten ([section 11](#the-stores-keys)) | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D9](planning/2026-11-11-flex-store-plan.md#d9) |
| <a id="ad-108"></a>**AD-108** | `database.py` | `flex.py` | `read_flex(data)` → the report's `Statement` and generation time ([AD-117](#ad-117)), and `PARSER_VERSION` ([AD-118](#ad-118)) | [D6](planning/2026-11-11-flex-store-plan.md#d6), [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-109"></a>**AD-109** | `database.py` | `convert.py` | `convert(Statement)`, one report at a time | [D3](planning/2026-11-11-flex-store-plan.md#d3), [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-110"></a>**AD-110** | `database.py` | `reconcile.py` | `reconcile(Statement, rows, gst_rate=)` with the ledger's rate; unattributed GST is never accepted | [D18](planning/2026-11-11-flex-store-plan.md#d18), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-092"></a>**AD-092** | `store.py` | `locks.py` | `locked(path)` on `store.lock` and each `ledger.lock` ([AD-089](#ad-089)) | [D21](planning/2026-11-11-flex-store-plan.md#d21) |

| Item | Module | Its job | Decisions |
|---|---|---|---|
| <a id="ad-013"></a>**AD-013** | `model.py` | The typed model every other module shares ([section 3](#3-the-model)); no I/O | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-014"></a>**AD-014** | `parser.py` | The Activity Statement front end ([section 4](#4-reading-an-activity-statement)), and `parse_statement`, which picks the front end by the file's content ([AD-028](#ad-028)) | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-015"></a>**AD-015** | `flex.py` | The Flex report front end ([section 5](#5-reading-a-flex-report)); never imports `parser.py` | [D16](planning/2026-11-11-flex-store-plan.md#d16), [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-016"></a>**AD-016** | `convert.py` | Records to per-currency output rows ([section 6](#6-conversion)); unrounded amounts, no synthetic rows | [D28](planning/2026-11-14-earlier-decisions.md#d28), [D38](planning/2026-11-14-earlier-decisions.md#d38) |
| <a id="ad-017"></a>**AD-017** | `reconcile.py` | The Cash Report checks, synthetic rows and rounding ([section 7](#7-reconciliation)) | [D23](planning/2026-11-14-earlier-decisions.md#d23) |
| <a id="ad-106"></a>**AD-106** | `gst.py` | The GST rate's default, Australia's 10%, and the GST a source links to its fees; shared by conversion and reconciliation, so neither depends on the other | [D19](planning/2026-11-11-flex-store-plan.md#d19), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-018"></a>**AD-018** | `writer.py` | The CSV files and the report file ([section 8](#8-output)); the only module besides the front ends that writes files | [D25](planning/2026-11-14-earlier-decisions.md#d25), [D41](planning/2026-11-14-earlier-decisions.md#d41) |
| <a id="ad-019"></a>**AD-019** | `engine.py` | The one entry point every front end drives ([AD-059](#ad-059)); never prints | [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-020"></a>**AD-020** | `flex_client.py` | Runs a Flex query over HTTPS ([section 10](#10-the-flex-web-service-client)); no other I/O | [D11](planning/2026-11-11-flex-store-plan.md#d11), [D15](planning/2026-11-11-flex-store-plan.md#d15) |
| <a id="ad-081"></a>**AD-081** | `vault.py` | The store's keys and the sealing of its files ([section 11](#the-stores-keys)): bytes in and out, no files or folders; the only module using `cryptography` and `keyring`. Its errors are `VaultError`s | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D9](planning/2026-11-11-flex-store-plan.md#d9) |
| <a id="ad-088"></a>**AD-088** | `locks.py` | Exclusive locks on files ([section 11](#locks)); the standard library only, and nothing else of the store | [D21](planning/2026-11-11-flex-store-plan.md#d21) |
| <a id="ad-107"></a>**AD-107** | `database.py` | A ledger's two DuckDB databases ([section 11](#the-databases)): `data.db` built from its reports, `exports.db` the export record. The only module that knows DuckDB; it knows nothing of folders or passwords, taking paths and a key | [D6](planning/2026-11-11-flex-store-plan.md#d6), [D22](planning/2026-11-11-flex-store-plan.md#d22) |
| <a id="ad-093"></a>**AD-093** | `store.py` | The store's folder ([section 11](#the-folder)): created and opened, its password, its ledgers and their settings and originals, its locks. Knows nothing of DuckDB; its errors are `StoreError`s | [D7](planning/2026-11-11-flex-store-plan.md#d7), [D14](planning/2026-11-11-flex-store-plan.md#d14) |

## 3. The model

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-021"></a>**AD-021** | `Statement` | One statement period as every front end delivers it: `period_start`, `period_end`, `account`, `base_currency`; the records in four lists, each in source order — `trades`, `corporate_actions`, `cash_txns`, `tax_items`; and `cash_report`, currency → component → amount, components named as in the Activity Statement ("Starting Cash", "Trades (Sales)", …), the base-currency summary left out | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-022"></a>**AD-022** | Records | `Trade`, `CorporateAction`, `CashTxn` and `TaxItem` share `id` (IBKR's transaction ID where the source has one, else the line number), `where` (how an error points at it: "line 57", "Flex CashTransaction 123") and `report_date` (where the source gives one). A trade's `commission` is the whole charge, taxes on it included; `proceeds` is `None` for futures | [D5](planning/2026-11-11-flex-store-plan.md#d5), [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-023"></a>**AD-023** | `CashKind` | The buckets cash transactions are checked in, in output order: Fees, Deposits & Withdrawals, Dividends, Withholding Tax, Interest, Commission Adjustments; each value is the bucket's name in notes and errors | [D28](planning/2026-11-14-earlier-decisions.md#d28) |
| <a id="ad-024"></a>**AD-024** | `AssetClass` | Stocks, options, futures, bonds and forex; the value is the output's Reference tag — `STOCK`, `OPTION`, `FUTURE`, `BOND`, `FX` | [D38](planning/2026-11-14-earlier-decisions.md#d38) |
| <a id="ad-025"></a>**AD-025** | `TaxKind` | Stamp duty — inside its trade's commission, for display and checks only — and GST, cash of its own, on the item its `taxed_id` names | [D19](planning/2026-11-11-flex-store-plan.md#d19) |
| <a id="ad-026"></a>**AD-026** | `OutputRow`, `CurrencyResult` | A row is a date, an amount (unrounded until [AD-048](#ad-048)), a description, the payee "Interactive Brokers", a Reference tag, and — not written — the section and the record it came from, and for a forex trade its leg (`fx-base`, `fx-quote`, `fx-fee`); synthetic rows name no record. A result is one currency's rows, its Starting and Ending Cash, and notes on what was verified or synthesised | [D25](planning/2026-11-14-earlier-decisions.md#d25), [D29](planning/2026-11-14-earlier-decisions.md#d29), [D30](planning/2026-11-14-earlier-decisions.md#d30) |
| <a id="ad-027"></a>**AD-027** | Money and errors | Every amount is a `Decimal`, parsed from the source's text, never a float; written plain, without trailing zeros. `StatementError`: the input is malformed or unsupported. `ReconciliationError`: the cash does not add up; its text is a report of every problem | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D24](planning/2026-11-14-earlier-decisions.md#d24) |

## 4. Reading an Activity Statement

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-028"></a>**AD-028** | Picking the front end | A file whose first byte past a byte-order mark and whitespace is `<` is a Flex report ([section 5](#5-reading-a-flex-report)); anything else is read as an Activity Statement | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-029"></a>**AD-029** | Sections | Every row is `Section,Kind,cells…`. A `Header` row names the columns of the `Data` rows after it, per section, and may be re-emitted (Trades does so per asset category). Rows of kind `Total`, `SubTotal`, `Total in USD`, `Notes` or empty are skipped; any other kind rejects. Trailing empty cells are tolerated, extra filled cells reject. The first cell carries a UTF-8 byte-order mark | [D27](planning/2026-11-14-earlier-decisions.md#d27) |
| <a id="ad-030"></a>**AD-030** | Period and account | The Statement section's Period field — "June 1, 2026 - June 30, 2026", or one date — gives the period; Account Information gives the account and base currency. No Period rejects | [D27](planning/2026-11-14-earlier-decisions.md#d27) |
| <a id="ad-031"></a>**AD-031** | Trades | `DataDiscriminator` `Order` rows are transactions; `ClosedLot`, `SubTotal` and `Total` are not; execution-level `Trade` rows reject, asking for order-level detail; any other rejects. Asset categories Stocks, Bonds, Equity and Index Options, Futures and Forex map to [AD-024](#ad-024); any other rejects. Futures need no Proceeds. A forex row's commission is in the one `Comm in XXX` column, which names its currency | [D27](planning/2026-11-14-earlier-decisions.md#d27), [D31](planning/2026-11-14-earlier-decisions.md#d31), [D32](planning/2026-11-14-earlier-decisions.md#d32), [D33](planning/2026-11-14-earlier-decisions.md#d33), [D58](planning/2026-11-14-earlier-decisions.md#d58) |
| <a id="ad-032"></a>**AD-032** | Cash sections | Fees, Deposits & Withdrawals (dated by Settle Date), Dividends, Withholding Tax, Interest and Commission Adjustments share Currency, Date, Description and Amount; Corporate Actions adds Proceeds; the Transaction Fees section becomes stamp-duty tax items. Rows whose Currency is not a three-letter code are aggregates and skipped. A record's ID is its line number, and it has no report date | [D27](planning/2026-11-14-earlier-decisions.md#d27), [D35](planning/2026-11-14-earlier-decisions.md#d35) |
| <a id="ad-033"></a>**AD-033** | Cash Report | Read in blocks, each opened by a `Starting Cash` row: rows belong to their block's currency, not their own Currency cell. The first, base-currency block is skipped. A duplicate block or component, or a row before any block, rejects; so does a statement without a Cash Report | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D26](planning/2026-11-14-earlier-decisions.md#d26) |

## 5. Reading a Flex report

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-034"></a>**AD-034** | The section table | `SECTIONS` is the one table of what is read: each section's element, its name in Client Portal's query editor, the rows read and the level of detail they must have, and the attributes every such row must carry. It drives both the validation and the mapping. A missing section, level or field rejects, naming what to tick in the query | [D16](planning/2026-11-11-flex-store-plan.md#d16) |
| <a id="ad-035"></a>**AD-035** | The statement | The root is `FlexQueryResponse` with exactly one `FlexStatement`, which carries `accountId`, `fromDate` and `toDate` — more than one rejects, until multi-account reports are supported. Dates are `yyyyMMdd`, times `HHmmss`, joined by `;`; anything else rejects with the settings to choose. Rows carry `reportDate`, kept in the record | [D5](planning/2026-11-11-flex-store-plan.md#d5), [D16](planning/2026-11-11-flex-store-plan.md#d16), [D20](planning/2026-11-11-flex-store-plan.md#d20) |
| <a id="ad-036"></a>**AD-036** | Cash Report | One `CashReportCurrency` row per currency, at the currency level of detail; its attributes map to the model's component names. Identification attributes, the deposits-plus-withdrawals sum and the per-segment and running-total splits (`…Com`, `…Sec`, `…MTD`, `…YTD`) are not components; an unknown attribute passes through under its Flex name. Zero components are left out, except Starting and Ending Cash | [D16](planning/2026-11-11-flex-store-plan.md#d16) |
| <a id="ad-037"></a>**AD-037** | Trades | `Order` rows. Categories `STK`, `OPT`, `FUT`, `BOND` and `CASH` map to [AD-024](#ad-024); any other rejects. An option's symbol is its `description`, the readable contract. The commission is `ibCommission` plus `taxes`; only a forex trade's commission may be in another currency than the trade. The record ID is `ibOrderID` | [D16](planning/2026-11-11-flex-store-plan.md#d16), [D17](planning/2026-11-11-flex-store-plan.md#d17) |
| <a id="ad-038"></a>**AD-038** | Cash transactions and transfers | `CashTransaction` rows map by `type` to [AD-023](#ad-023); any other type rejects; deposits and withdrawals are dated by `settleDate`. Cash `Transfer` rows join the deposits and withdrawals, their description rebuilt in the Activity Statement's wording ("Internal Transfer In From Account …"); a position transfer moving cash rejects. Descriptions are otherwise Flex's own wording | [D16](planning/2026-11-11-flex-store-plan.md#d16), [D17](planning/2026-11-11-flex-store-plan.md#d17) |
| <a id="ad-117"></a>**AD-117** | Reading a kept report | `read_flex(data)` takes a report's bytes, as fetched and kept, and returns `FlexReport`: its `Statement` and when IBKR generated it (`whenGenerated`), which decides between two reports of the same period | [D5](planning/2026-11-11-flex-store-plan.md#d5), [D6](planning/2026-11-11-flex-store-plan.md#d6) |
| <a id="ad-118"></a>**AD-118** | Parser version | `PARSER_VERSION`, beside the mapping, is raised whenever what a report maps to changes — here, in conversion or in reconciliation. A database built with another version is rebuilt ([AD-114](#ad-114)) | [D6](planning/2026-11-11-flex-store-plan.md#d6) |
| <a id="ad-039"></a>**AD-039** | Tax items | `TransactionTax` summary rows become stamp duty; `SalesTax` rows become GST naming the taxed item by `taxableTransactionID`; a tax type other than GST rejects | [D19](planning/2026-11-11-flex-store-plan.md#d19) |

## 6. Conversion

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-040"></a>**AD-040** | Output order | Per currency: trades, corporate actions, then cash transactions by [AD-023](#ad-023)'s order, each in source order | [D28](planning/2026-11-14-earlier-decisions.md#d28) |
| <a id="ad-041"></a>**AD-041** | Trade descriptions | One grammar: `{+\|-}{qty} {symbol} [({event})] price: {price} comm: {comm} [({qualifiers})]`. Zero price or commission is left out. Codes `A` and `Ep` add the events "assigned" and "expired"; other codes stay silent. The commission shows to the cent | [D38](planning/2026-11-14-earlier-decisions.md#d38) |
| <a id="ad-042"></a>**AD-042** | Cash trades | Stocks, options and bonds: one row in the trade's currency, proceeds plus commission | [D38](planning/2026-11-14-earlier-decisions.md#d38) |
| <a id="ad-043"></a>**AD-043** | Futures | One row of the commission alone, worded "… commission: {amt}": the notional never moves cash, and the P/L arrives as [AD-047](#ad-047) | [D31](planning/2026-11-14-earlier-decisions.md#d31) |
| <a id="ad-044"></a>**AD-044** | Forex | A trade on `BASE.QUOTE` is a transfer between two currency accounts: the base currency moves by the quantity, the quote currency — the trade's — by the proceeds, both tagged `FX` under one description. The commission is a row of its own in its own currency, tagged `FX-FEE`. A pair that does not read so rejects | [D32](planning/2026-11-14-earlier-decisions.md#d32) |
| <a id="ad-045"></a>**AD-045** | Stamp duty | A nonzero stamp-duty item joins the one trade with its currency, symbol and date-time, whose description gains "(incl. stamp duty {amt})"; matching no trade, or more than one, rejects. Zero items are skipped | [D35](planning/2026-11-14-earlier-decisions.md#d35) |
| <a id="ad-046"></a>**AD-046** | Corporate actions and cash transactions | A corporate action keeps its description and proceeds, tagged `CORP`. A cash transaction keeps its description and amount; GST the source links to a fee is added to the fee's amount and the description gains "(incl. GST {amt})"; GST linked to anything but a fee in its own currency rejects | [D19](planning/2026-11-11-flex-store-plan.md#d19) |

## 7. Reconciliation

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-047"></a>**AD-047** | Synthetic rows | Rows with no source record, dated the period's last day and tagged in the Reference column: `MTM` for the Cash Report's Cash Settling MTM, `GST` for GST on fees no row carries ([AD-051](#ad-051)), `ROUNDING` for the rounding residual ([AD-048](#ad-048)) | [D3](planning/2026-11-11-flex-store-plan.md#d3), [D29](planning/2026-11-14-earlier-decisions.md#d29), [D30](planning/2026-11-14-earlier-decisions.md#d30), [D37](planning/2026-11-14-earlier-decisions.md#d37) |
| <a id="ad-048"></a>**AD-048** | Rounding | After every check, amounts round half away from zero to the cent; the rounded rows must add up to rounded Ending less rounded Starting Cash within half a cent per row plus one cent, and the residual becomes the `ROUNDING` row; more rejects | [D30](planning/2026-11-14-earlier-decisions.md#d30) |
| <a id="ad-049"></a>**AD-049** | The Cash Report identity | Per currency, Starting Cash plus the components equals Ending Cash, to a millionth. Components map to the sections whose rows must add up to them: Trades (Sales), Trades (Purchase), Commissions, GST and Transaction Fees to the trades, with corporate actions and commission adjustments; dividends and payments in lieu; withholding tax; broker and bond interest; other fees; deposits, withdrawals and account transfers. Ending Settled Cash and the securities-lending collateral lines are information, outside the sums. A component that maps nowhere and is not zero rejects; so do rows in a currency the Cash Report lacks | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D26](planning/2026-11-14-earlier-decisions.md#d26), [D33](planning/2026-11-14-earlier-decisions.md#d33), [D34](planning/2026-11-14-earlier-decisions.md#d34), [D36](planning/2026-11-14-earlier-decisions.md#d36) |
| <a id="ad-050"></a>**AD-050** | Stamp-duty check | The stamp-duty items add up to the Transaction Fees component; they are a breakdown of trade commissions, never extra cash | [D35](planning/2026-11-14-earlier-decisions.md#d35) |
| <a id="ad-051"></a>**AD-051** | GST | The checks use the rate passed in — a ledger's setting, or Australia's 10% by default and in a one-shot conversion — and messages show it. GST folded into fees ([AD-046](#ad-046)) is checked with the fees. Of the rest of the GST component, the part inside trade commissions must be zero or the rate times the Commissions component — when it is the rate, every trade row showing a commission gains "(incl. GST)". The remainder, GST on fees with no dated row, must have the component's sign and no larger amount, and equal the rate times a subset of the Fees rows: a unique subset puts "(excl. GST {amt})" on each of its rows and lists them in the notes; several matching subsets are noted, not itemised; none rejects. It becomes the `GST` row. `accept_unattributed_gst` accepts GST that cannot be verified, the sign-and-size check still applying | [D37](planning/2026-11-14-earlier-decisions.md#d37), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-052"></a>**AD-052** | All or nothing | Every failed check in every currency is gathered into one `ReconciliationError`, and nothing is written. A currency without rows yields no result, and no file | [D23](planning/2026-11-14-earlier-decisions.md#d23), [D25](planning/2026-11-14-earlier-decisions.md#d25) |

## 8. Output

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-053"></a>**AD-053** | The CSV | `{CCY}.csv` per currency, UTF-8, Xero's bank-statement columns: `*Date` (ISO), `*Amount` (plain), `Payee`, `Description`, `Reference`, `Cheque Number` (empty) | [D25](planning/2026-11-14-earlier-decisions.md#d25) |
| <a id="ad-054"></a>**AD-054** | The overwrite check | Before writing anything, every target — each CSV and the report — is checked; if any exists, nothing is written, unless overwriting is asked for | [D40](planning/2026-11-14-earlier-decisions.md#d40) |
| <a id="ad-055"></a>**AD-055** | The output folder | The folder given, or else a subfolder beside the statement named after it without its extension (`_out` added when it has none) | [D40](planning/2026-11-14-earlier-decisions.md#d40) |
| <a id="ad-056"></a>**AD-056** | The run report | The statement, the time, the account and period, then per file its rows and cash and its notes — or that no currency had cash activity. It names files bare, as saved; a front end may show full paths. Saved as `ibkr2xero_report.txt` unless named otherwise or turned off | [D41](planning/2026-11-14-earlier-decisions.md#d41) |
| <a id="ad-057"></a>**AD-057** | Zero-amount rows | Dropped on request, and only after reconciliation, where they cannot hide a mismatch; a currency left without rows writes no file | [D46](planning/2026-11-14-earlier-decisions.md#d46) |

## 9. Front ends

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-058"></a>**AD-058** | Library API | `ibkr_to_xero` exports the model's `Statement`, `OutputRow`, `CurrencyResult` and errors, the stages `parse_statement`, `convert`, `reconcile` and `write_results`, and the engine's `run`, `RunOptions`, `RunResult`, `FileOutput` and `resolve_output_dir` | [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-059"></a>**AD-059** | `engine.run` | Parses, converts, reconciles, drops zero rows if asked, resolves the folder, writes and composes the report; returns a `RunResult` — the account line, the folder, each file with its summary and notes, the report's text and path. `RunOptions`: `output_dir`, `force_overwrite`, `skip_zero_transactions`, `report_name` (`None`: not saved), `accept_unattributed_gst`. It never prints; `StatementError`, `ReconciliationError` and `FileExistsError` propagate | [D39](planning/2026-11-14-earlier-decisions.md#d39), [D42](planning/2026-11-14-earlier-decisions.md#d42) |
| <a id="ad-060"></a>**AD-060** | The CLI | `ibkr2xero FILE [-o DIR] [-f] [-s] [-r NAME] [--no-save-report] [--accept-unattributed-gst] [--version]`. Prints the account line, each file with its full path, its summary and notes, and where the report went; exit 0. A rejection or an overwrite conflict goes to standard error with "no output files were written", exit 2 | [D15](planning/2026-11-11-flex-store-plan.md#d15), [D39](planning/2026-11-14-earlier-decisions.md#d39), [D40](planning/2026-11-14-earlier-decisions.md#d40), [D41](planning/2026-11-14-earlier-decisions.md#d41), [D46](planning/2026-11-14-earlier-decisions.md#d46) |
| <a id="ad-061"></a>**AD-061** | The GUI window | One window in four states — Idle, Busy, Done, Error. Idle takes one dropped file, or one opened from the context menu; offers the three options as checkboxes and the output folder, prefilled by [AD-055](#ad-055); converts on a worker thread. An overwrite conflict asks, and converts again with overwriting if allowed. Done shows the report, a link per written file, and Open folder (on Windows, Explorer with the report selected); Error shows the message in red and that nothing was written | [D15](planning/2026-11-11-flex-store-plan.md#d15), [D39](planning/2026-11-14-earlier-decisions.md#d39), [D45](planning/2026-11-14-earlier-decisions.md#d45) |
| <a id="ad-062"></a>**AD-062** | The GUI's dependency | PySide6 is an optional extra (`ibkr-to-xero[gui]`); without it the GUI says how to install it. On Windows the window sets its own taskbar identity, so the taskbar shows its icon | [D43](planning/2026-11-14-earlier-decisions.md#d43), [D52](planning/2026-11-14-earlier-decisions.md#d52) |

## 10. The Flex Web Service client

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-063"></a>**AD-063** | `FlexClient.fetch` | `FlexClient(token).fetch(query_id, from_date, to_date)` returns the report's bytes. At most 365 days per request, ending no earlier than it starts; anything else is a `ValueError` | [D11](planning/2026-11-11-flex-store-plan.md#d11) |
| <a id="ad-064"></a>**AD-064** | Polling | IBKR's version-3 SendRequest returns a reference; GetStatement is polled for the report, first after 5 seconds, then at doubling intervals up to 30, until a deadline (`max_wait`, an hour by default); a progress callback hears each wait | [D11](planning/2026-11-11-flex-store-plan.md#d11) |
| <a id="ad-065"></a>**AD-065** | Errors | IBKR's transient codes (generation in progress, rate limit, server load, …) are retried until the deadline. Token codes raise `FlexTokenError` with how to make a new token; query codes `FlexQueryError`; network, HTTP and deadline failures `FlexUnavailableError`; anything else `FlexError` | [D11](planning/2026-11-11-flex-store-plan.md#d11) |
| <a id="ad-066"></a>**AD-066** | The token | Passed in, never stored by the client, and never in a message: no error carries the request URL. Messages are ASCII, which any console prints | [D9](planning/2026-11-11-flex-store-plan.md#d9) |
| <a id="ad-067"></a>**AD-067** | Testing seams | The HTTP opener, `sleep` and the monotonic clock are parameters, so tests run without the network or waiting. Requests identify themselves as `ibkr2xero/<version>` | |

## 11. The store

### The store's keys

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-082"></a>**AD-082** | Keys and IDs | `new_key()`: 32 random bytes, one key per store, encrypting everything in it. `new_store_id()`: 8 random hex digits, which name the store's credential ([AD-085](#ad-085)) and reveal nothing | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D59](planning/2026-11-14-store-modules-plan.md#d59) |
| <a id="ad-083"></a>**AD-083** | `store.key` | `lock_key(key, password, store_id, cost)` gives the file's contents: JSON text holding the format (1), the store ID, the scrypt cost and a random salt, the store key sealed with AES-GCM under the key scrypt derives from the password, and a check value derived from the store key. The password is taken in Unicode NFC, so the same password typed on another system opens it. `unlock_key(contents, password)` returns the key: one the password does not open is `WrongPassword`; contents that do not parse, of another format, or whose key fails the check are `KeyFileUnreadable`. `store_id_of(contents)` reads the ID without the password; `key_opens(contents, key)` checks a key against the check value. The default cost, N=2¹⁷, r=8, p=1, takes about half a second and 128 MiB; being in the file, it can be raised without breaking an existing store. A cost needing over 1 GiB is unreadable rather than attempted | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D14](planning/2026-11-11-flex-store-plan.md#d14) |
| <a id="ad-084"></a>**AD-084** | Sealed files | `seal(key, data, purpose)` compresses with `zlib`, then encrypts with AES-GCM under a fresh random 96-bit nonce: a format byte (1), the nonce, then the ciphertext and its tag. The purpose — `index`, `ledger:<id>`, `original:<hash>` — is authenticated with the format byte, so one sealed file cannot stand in for another. `unseal(key, sealed, purpose)` returns the data; a file that does not open is `Damaged`, never a wrong password, since the key has been proven by then | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D14](planning/2026-11-11-flex-store-plan.md#d14) |
| <a id="ad-085"></a>**AD-085** | The remembered key | `remember(store_id, key)` keeps the key, in base64, as the credential `IBKR to Xero — store <id>`, user name `store key`, replacing any earlier one. On Windows it is a generic credential persisted on this computer only; on macOS a login-keychain item, never marked to sync. The store is the OS's own, not `keyring`'s pick of backend, which chains backends once a second is installed and would bypass that persistence. `remembered(store_id)` returns the key or `None`: missing, malformed, or a credential store that fails — each means asking for the password; the caller checks the key with `key_opens`. `forget(store_id)` removes it, a missing one being no error. A credential store that fails to keep or remove a key is a `VaultError` | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D59](planning/2026-11-14-store-modules-plan.md#d59) |
| <a id="ad-094"></a>**AD-094** | Content names | `content_name(key, data)`: HMAC-SHA256 of the data under a key derived from the store key, as 64 hex digits. It names an original uniquely and reveals nothing — not even to someone holding the same report, as a plain hash would | [D6](planning/2026-11-11-flex-store-plan.md#d6), [D14](planning/2026-11-11-flex-store-plan.md#d14) |
| <a id="ad-086"></a>**AD-086** | DuckDB's key | `database_key(key)` derives, by HKDF-SHA256 labelled for DuckDB, the text handed to DuckDB as its `ENCRYPTION_KEY`. The store key itself never reaches DuckDB, so neither use of it can weaken the other | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D22](planning/2026-11-11-flex-store-plan.md#d22) |
| <a id="ad-087"></a>**AD-087** | Testing seams | The scrypt cost is a parameter — of `lock_key`, and of the store's `create`, `change_password` and `start_over` — kept in the file, so tests lock with a cheap one. The credential store is chosen in one function, which tests replace with one in memory; one test per platform checks the real Credential Manager or Keychain, skipped where the keychain is locked (an SSH session) | |

### Locks

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-089"></a>**AD-089** | `locked(path)` | `with locked(path):` holds an exclusive lock on the file, creating it if it is missing; lock files are never deleted. A lock held elsewhere — by another process, or by another open of the file in this one — is refused at once with `Busy`, never waited for. It is released when the block ends, and by the OS when the process dies, so a crash leaves no stale lock. Nothing is written into the file, so a refusal can say only that another IBKR to Xero window or command is working there | [D21](planning/2026-11-11-flex-store-plan.md#d21), [D60](planning/2026-11-14-store-modules-plan.md#d60) |
| <a id="ad-090"></a>**AD-090** | The mechanism | Windows: `msvcrt.locking` on the file's first byte, held per handle. macOS: `flock`, held per open file — not `lockf`, whose locks belong to the whole process: two opens in one process would not exclude each other, and closing any handle would drop them | [D21](planning/2026-11-11-flex-store-plan.md#d21) |

### The folder

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-095"></a>**AD-095** | Where and what | `default_folder()`: `%LOCALAPPDATA%\IBKR to Xero` on Windows, `~/Library/Application Support/IBKR to Xero` on macOS; every call takes the folder, so a store set aside and a test's are the same code. In it `store.key`, `store.lock`, `index.enc`, and per ledger a folder of 16 random hex digits holding `ledger.lock`, `ledger.enc`, `originals/<content name>.enc`, `data.db` and `exports.db`. No name says anything of the accounts or periods inside | [D7](planning/2026-11-11-flex-store-plan.md#d7), [D14](planning/2026-11-11-flex-store-plan.md#d14) |
| <a id="ad-096"></a>**AD-096** | Creating and opening | `Store.create(folder, password)` makes a key and a store ID, an empty index, then `store.key` — a store exists once `store.key` does — and refuses a folder that has one (`StoreExists`). `Store.open(folder, password=None)` uses the remembered key if it opens `store.key`, else the password: none is `PasswordNeeded`, a wrong one `WrongPassword`; no `store.key` is `NoStore`; a damaged `store.key` or index `StoreUnreadable`. Remembering is separate: `store.remembered` and `store.set_remembered(on)`, whose failure is `CannotRemember` and leaves the store open | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D59](planning/2026-11-14-store-modules-plan.md#d59) |
| <a id="ad-097"></a>**AD-097** | The password | `change_password(new)` asks for no current one and rewrites `store.key` alone, with the same key and store ID, so a remembered key still opens it | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D63](planning/2026-11-14-store-modules-plan.md#d63) |
| <a id="ad-098"></a>**AD-098** | Starting over | `Store.start_over(folder, password)` renames the folder to `<its name> (password forgotten <date>)` beside it — ` 2`, ` 3` appended for another the same day — and creates a new store; nothing is deleted, and the old store's remembered key, if any, is left. A folder in use (Windows refuses the rename) is `StoreBusy` | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D62](planning/2026-11-14-store-modules-plan.md#d62) |
| <a id="ad-099"></a>**AD-099** | Ledgers | `index.enc`, sealed as `index`, lists each ledger's ID, display name and IB account IDs. `ledgers()`; `ledger(id)` and `find_ledger(name)`, one `Ledger` object per ledger and store; `add_ledger(name)`, `rename_ledger(id, name)`, `set_accounts(id, accounts)` and `remove_ledger(id)`, each under the store lock. Names are trimmed, non-empty and unique ignoring case (`NameTaken`). A new ledger's folder and settings are written before the index names it; removing one takes its lock too, takes it out of the index, then deletes its folder | [D14](planning/2026-11-11-flex-store-plan.md#d14), [D20](planning/2026-11-11-flex-store-plan.md#d20), [D61](planning/2026-11-14-store-modules-plan.md#d61) |
| <a id="ad-100"></a>**AD-100** | Ledger settings | `ledger.settings()` → `LedgerSettings`: the Flex token and query ID (empty until entered), each IB account's base currency, each currency's last date already in Xero (or none), and the GST rate, 10% by default. `save_settings(settings)`, under the ledger lock. Sealed as `ledger:<id>`, JSON with a format number; a field a later version adds takes its default in an older file | [D9](planning/2026-11-11-flex-store-plan.md#d9), [D12](planning/2026-11-11-flex-store-plan.md#d12), [D20](planning/2026-11-11-flex-store-plan.md#d20), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-101"></a>**AD-101** | Originals | `add_original(data)` → its content name, sealed as `original:<ledger id>/<name>` under the ledger lock; the same report twice is stored once. `originals()` lists the names, `read_original(name)` returns the bytes; one that does not open is `StoreUnreadable`, naming it | [D6](planning/2026-11-11-flex-store-plan.md#d6), [D14](planning/2026-11-11-flex-store-plan.md#d14) |
| <a id="ad-102"></a>**AD-102** | Databases | `ledger.data_db` and `ledger.exports_db` are the paths; `ledger.database_key` is the store's key for DuckDB ([AD-086](#ad-086)) | [D6](planning/2026-11-11-flex-store-plan.md#d6), [D22](planning/2026-11-11-flex-store-plan.md#d22) |
| <a id="ad-103"></a>**AD-103** | Locks | `with store.locked():` holds `store.lock`, `with ledger.locked():` its `ledger.lock`. Reentrant within the thread holding them, so a method taking a lock itself works inside a caller's block; another thread, object or process is refused. A refusal is `StoreBusy`: "Another IBKR to Xero window or command is working on this account" — or "on the store" | [D21](planning/2026-11-11-flex-store-plan.md#d21), [D60](planning/2026-11-14-store-modules-plan.md#d60) |
| <a id="ad-104"></a>**AD-104** | Writing a file | To a temporary file beside it, flushed to disk, then renamed over it: a crash leaves the old file or the new, never part of either | [D6](planning/2026-11-11-flex-store-plan.md#d6) |

### The databases

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-111"></a>**AD-111** | Opening | `Database.open(data_path, exports_path, key)`: one in-memory DuckDB connection with both files attached, encrypted with the key ([AD-086](#ad-086)). Extension downloads and autoloading are off and the extension directory empty, so only the `httpfs` linked into our build is used. Opened for an operation and closed after it, under the ledger's lock | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D21](planning/2026-11-11-flex-store-plan.md#d21), [D22](planning/2026-11-11-flex-store-plan.md#d22) |
| <a id="ad-112"></a>**AD-112** | `data.db` | The tables of [D18](planning/2026-11-11-flex-store-plan.md#d18): `meta`, `ib_account`, `period`, `cash_report`, `trade`, `cash_txn`, `corporate_action`, `tax_item` and `line`, each record keeping its period and IB account. A `line` also keeps its report date — its record's, or the period's end for a synthetic one — and its place in the output. Money and quantities are `DECIMAL(38,11)`; a value with more places, or 28 digits before the point, rejects the report, since DuckDB would round it silently. Written in multi-row inserts | [D18](planning/2026-11-11-flex-store-plan.md#d18), [D20](planning/2026-11-11-flex-store-plan.md#d20) |
| <a id="ad-113"></a>**AD-113** | Line keys | Stable across rebuilds, since `exports.db` records them: `<account>:<T\|A\|C>:<record ID>`, with `:<leg>` for forex — T a trade, A a corporate action, C a cash transaction, so IBKR's separate ID series cannot collide; a synthetic row `<MTM\|GST\|ROUNDING>:<account>:<from>-<to>:<currency>`. Two lines with one key reject the build | [D18](planning/2026-11-11-flex-store-plan.md#d18) |
| <a id="ad-114"></a>**AD-114** | Building | `ensure_current(names, read, gst_rate)`, given the ledger's report names and a reader. A database recording another parser version, schema or GST rate is rebuilt from every report: into a new file, which replaces `data.db` only when complete. Otherwise only reports it lacks are added, in one transaction. Each report is read, converted and reconciled ([AD-108](#ad-108) to [AD-110](#ad-110)); one that fails leaves the database as it was and is `BuildFailed`, naming the report's period — the old database is kept for whoever fixes it | [D3](planning/2026-11-11-flex-store-plan.md#d3), [D6](planning/2026-11-11-flex-store-plan.md#d6), [D57](planning/2026-11-11-flex-store-plan.md#d57) |
| <a id="ad-115"></a>**AD-115** | Periods | Each report is a period of one IB account. Two reports of exactly one period: the later generated is used, the other marked superseded. Periods overlapping otherwise are refused, until the manual re-fetch ([D5](planning/2026-11-11-flex-store-plan.md#d5)) defines which wins | [D5](planning/2026-11-11-flex-store-plan.md#d5), [D6](planning/2026-11-11-flex-store-plan.md#d6) |
| <a id="ad-116"></a>**AD-116** | `exports.db` | `export` (when, up to which date, the folder), `export_file` (its currency, file name, `pending` or `confirmed` and when) and `exported_line`; never rebuilt. `unexported_lines(to_date, imported_through)`: per currency, in output order, the lines dated up to `to_date` in no confirmed file and not reported on or before that currency's date already in Xero, if given. `record_export(to_date, folder, files)` records an export's files as pending; `pending_files()` lists them; `confirm(file_id)` marks one imported | [D7](planning/2026-11-11-flex-store-plan.md#d7), [D10](planning/2026-11-11-flex-store-plan.md#d10), [D12](planning/2026-11-11-flex-store-plan.md#d12) |

## 12. Distribution

| Item | Element | Design | Decisions |
|---|---|---|---|
| <a id="ad-068"></a>**AD-068** | The Python package | `ibkr-to-xero`, Python 3.12 or later; the commands `ibkr2xero` and `ibkr2xero-gui`; required dependencies `cryptography`, `keyring` and `duckdb` (for the store) — DuckDB as our own wheel from its release ([AD-078](#ad-078)) on Windows x64 and Apple silicon, PyPI's elsewhere; extras `gui` (PySide6) and `dev` (pytest); the app icon as package data | [D8](planning/2026-11-11-flex-store-plan.md#d8), [D22](planning/2026-11-11-flex-store-plan.md#d22), [D43](planning/2026-11-14-earlier-decisions.md#d43), [D44](planning/2026-11-14-earlier-decisions.md#d44), [D51](planning/2026-11-14-earlier-decisions.md#d51), [D64](planning/2026-11-14-store-modules-plan.md#d64) |
| <a id="ad-069"></a>**AD-069** | Frozen build | One PyInstaller spec builds both programs from `packaging/entrypoints/` into one folder: the GUI (windowed) and the CLI (console) executables over one shared `_internal` holding Python, the packages and the native libraries, unpacked once at install and never per run. On macOS the folder is the `IBKR to Xero.app` bundle, both executables in `Contents/MacOS`, its `Info.plist` setting `LSBackgroundOnly` false — a console program in a bundle otherwise makes PyInstaller mark the whole app background-only | [D48](planning/2026-11-14-earlier-decisions.md#d48), [D56](planning/2026-11-14-earlier-decisions.md#d56) |
| <a id="ad-070"></a>**AD-070** | macOS package | One `.pkg` for Apple silicon only, refused elsewhere by Installer; the programs signed with the hardened runtime by the Developer ID Application identity, the package by the Developer ID Installer identity. It needs macOS 14.4, Qt 6.12's own minimum, and Installer refuses older systems | [D48](planning/2026-11-14-earlier-decisions.md#d48), [D49](planning/2026-11-14-earlier-decisions.md#d49), [D50](planning/2026-11-14-earlier-decisions.md#d50), [D52](planning/2026-11-14-earlier-decisions.md#d52), [D55](planning/2026-11-14-earlier-decisions.md#d55) |
| <a id="ad-071"></a>**AD-071** | Windows installer | Inno Setup, installing for the current user unless elevated; not code-signed | [D48](planning/2026-11-14-earlier-decisions.md#d48), [D50](planning/2026-11-14-earlier-decisions.md#d50) |
| <a id="ad-076"></a>**AD-076** | What the macOS package installs | The app into `/Applications`, always; the CLI a choice, ticked by default: the package's install script links `/usr/local/bin/ibkr2xero` to the CLI inside the app | [D48](planning/2026-11-14-earlier-decisions.md#d48), [D56](planning/2026-11-14-earlier-decisions.md#d56) |
| <a id="ad-077"></a>**AD-077** | What the Windows installer installs | The program folder, always; two choices — the GUI's Start-menu shortcut, with an optional desktop one, and the CLI, the folder on `PATH` in the scope installed | [D48](planning/2026-11-14-earlier-decisions.md#d48), [D56](planning/2026-11-14-earlier-decisions.md#d56) |
| <a id="ad-072"></a>**AD-072** | The release workflow | A `v*` tag runs the tests — on Linux first, where the database's skip, then in the macOS and Windows build jobs on the DuckDB that ships — checks the tag against the package version, creates a draft release and attaches both installers; on macOS it notarises and staples the package, then runs the packaged CLI's `--help` and keeps the packaged app running for 20 seconds. A manual run is a dry run that attaches the installers to the run instead. Nothing is published automatically | [D47](planning/2026-11-14-earlier-decisions.md#d47), [D50](planning/2026-11-14-earlier-decisions.md#d50), [D53](planning/2026-11-14-earlier-decisions.md#d53), [D65](planning/2026-11-14-store-modules-plan.md#d65) |
| <a id="ad-073"></a>**AD-073** | macOS signing material | The signing identities are chosen by SHA-1 in a temporary keychain built from the repository's secrets, deleted when the job ends; notarisation uses an App Store Connect API key from the secrets | [D50](planning/2026-11-14-earlier-decisions.md#d50) |
| <a id="ad-074"></a>**AD-074** | Version | `pyproject.toml` is the one source of the version: the CLI's `--version`, the window's title, the installers' names and the Windows file properties all read it | [D53](planning/2026-11-14-earlier-decisions.md#d53) |
| <a id="ad-075"></a>**AD-075** | Test data | Real statements live only in the gitignored `examples/`; tests that need them skip when it is absent, so CI runs on synthetic data alone | [D54](planning/2026-11-14-earlier-decisions.md#d54) |
| <a id="ad-078"></a>**AD-078** | The DuckDB wheel workflow | Run by hand with one duckdb-python tag. For macOS on Apple silicon and Windows x64: vcpkg pinned to a commit builds static OpenSSL and curl — on macOS through a triplet targeting macOS 14.4 — kept in vcpkg's binary cache; duckdb-python compiles through `ccache` with `httpfs` added to `BUILD_EXTENSIONS`, its version forced to the tag (`OVERRIDE_GIT_DESCRIBE`) and, on macOS, its minimum macOS 14.4. Each wheel passes [AD-079](#ad-079) in plain Python and frozen, the macOS module must record macOS 14.4 as its minimum, and it gets a notices file: duckdb-python, DuckDB, duckdb-httpfs and every vcpkg port inside. Published on the pre-release `duckdb-<version>-httpfs` with a `SHA256SUMS` | [D22](planning/2026-11-11-flex-store-plan.md#d22), [D55](planning/2026-11-14-earlier-decisions.md#d55) |
| <a id="ad-079"></a>**AD-079** | The wheel check | `packaging/duckdb/check_wheel.py`: the version is the tag's exactly; `httpfs` is statically linked; with extension downloads and autoloading off and an empty extension directory, an encrypted database is written with no `LOAD`, its contents unreadable in the file, read back exactly — a `DECIMAL(38,11)` amount included — and refused with a wrong key | [D18](planning/2026-11-11-flex-store-plan.md#d18), [D22](planning/2026-11-11-flex-store-plan.md#d22) |
