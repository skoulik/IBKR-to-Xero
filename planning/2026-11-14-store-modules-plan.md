# The store's modules — plan

*Layout agreed with Sergei 2026-11-14. Step 4 of the build order in
[2026-11-11-flex-store-plan.md](2026-11-11-flex-store-plan.md), whose decisions (D6–D22) this
implements; nothing here changes them. Names of functions are working names and may change in
the code. As each module is built, its place in the diagram, its arrows and its interfaces
become items of [ARCHITECTURE.md](../ARCHITECTURE.md), which then holds the current design;
this plan stays the record of what was agreed.*

## The layers

Four new modules under `ibkr_to_xero/`, each depending only on those below it. Only
`database.py` knows DuckDB, so a change of engine touches one file; only `store.py` knows the
folder; the converter modules do not change.

```mermaid
flowchart TD
    subgraph FRONT["Front ends"]
        cli["cli.py"]
        gui["gui.py"]
    end

    subgraph STEP5["Step 5"]
        ops["operations<br/>fetch, export, confirm"]
    end

    engine["engine.py<br/>one-shot conversion"]
    client["flex_client.py<br/>Flex Web Service"]

    subgraph STEP4["Step 4 — the store"]
        database["database.py<br/>DuckDB: schema, rebuild,<br/>export record"]
        store["store.py<br/>folder, ledgers, originals,<br/>password"]
        vault["vault.py<br/>keys, seal / open,<br/>remembered key"]
        locks["locks.py<br/>OS file locks"]
    end

    subgraph CONVERTER["The converter (unchanged)"]
        flex["flex.py"]
        parser["parser.py"]
        convert["convert.py"]
        reconcile["reconcile.py"]
        writer["writer.py"]
        model["model.py"]
    end

    cli --> engine
    gui --> engine
    cli -.-> ops
    gui -.-> ops
    ops -.-> store
    ops -.-> database
    ops -.-> client
    ops -.-> writer
    engine --> parser
    engine --> convert
    engine --> reconcile
    engine --> writer
    parser --> flex
    database --> flex
    database --> convert
    database --> reconcile
    store --> vault
    store --> locks
    flex --> model
    convert --> model
    reconcile --> model
```

Dashed: step 5, not built here. Third-party dependencies: `vault.py` — `cryptography`,
`keyring`; `database.py` — `duckdb` (our build, D22); the rest — the standard library.

## The modules

| Module | Its job (decisions) | Knows nothing of |
|---|---|---|
| `vault.py` | A random store key; a key from the password with `scrypt`; the store key locked and unlocked with it, as the contents of `store.key`; bytes sealed and opened — `zlib`, then AES-GCM; the key remembered in, read from and forgotten by the OS credential store (D8, D9) | Folders, DuckDB |
| `locks.py` | An exclusive lock on a file — `msvcrt` on Windows, `fcntl` on macOS — released when its process dies; a busy lock is refused at once, never waited on (D21) | Everything else |
| `store.py` | The folder (D7, D14): a store created, or opened with the remembered key or else the password; the password changed (only `store.key` rewritten); starting over, which renames the old folder aside and never deletes it (D9); the ledger index; each ledger's settings — Flex token and query ID, IB accounts, currencies and their dates, GST rate (D12, D20, D57); originals added and read, named by a hash of their content (D6); the store and ledger locks (D21) | DuckDB |
| `database.py` | DuckDB only (D22): `data.db` and `exports.db` opened, encrypted with the store key handed to it (D8); the schema, amounts `DECIMAL(38,11)` (D18); `data.db` rebuilt from the originals when the parser version it records is not the current one (D6) — each Flex report parsed to the model, `convert` and `reconcile` run per period and IB account, its rows and lines written; the export record (D10) | Folders, passwords |

**One direction only.** The model is written into tables and never read back into a
`Statement`: exports read the finished `line` rows, reports query the tables. No reverse
mapping, no tests for one.

**GST gets a home of its own.** Reconciliation takes the ledger's GST rate as a parameter
(D57), and with that change `fee_gst` moves out of `convert.py` into a module both
`convert.py` and `reconcile.py` use, so reconciliation no longer depends on conversion
(ARCHITECTURE AD-011, agreed 2026-11-14).

**The parser version** is a constant beside the Flex mapping, raised whenever what a report
maps to changes; `data.db` records the one it was built with, and a mismatch rebuilds it.

## Interfaces, in outline

- `vault`: `new_key()`; `lock_key(key, password, cost)` → the contents of `store.key`;
  `unlock_key(contents, password)` → the key, or `WrongPassword`; `seal(key, data, purpose)` /
  `unseal(key, sealed, purpose)` — `purpose` names the file (`index`, `ledger:<id>`,
  `original:<hash>`) and is authenticated with it, so one sealed file cannot stand in for
  another; `remember(store_id, key)`, `remembered(store_id)`, `forget(store_id)`.
- `locks`: `with locked(path):` — `Busy` when someone else holds it (D60).
- `store`: `Store.create(folder, password, remember)`; `Store.open(folder, password=None)` —
  the remembered key first, then the password: `PasswordNeeded`, `WrongPassword`,
  `StoreUnreadable`; `change_password`; `start_over`; `ledgers()`, `add_ledger`,
  `rename_ledger`, `remove_ledger`; per ledger: `settings` read and saved, `add_original(data)`,
  `originals()`, `read_original(name)`, the paths of its two databases; `with store.locked():`
  and `with ledger.locked():`.
- `database`: `open_data(path, key)`, `open_exports(path, key)`; `ensure_current(con,
  originals)` — a rebuild only if the parser version moved; the export record —
  `unexported_lines(to_date)`, `record_export(...)`, `confirm(file)`.

## Tests

One file per module, in temporary folders. An in-memory `keyring` backend stands in for the
OS store. The `scrypt` cost is part of `store.key`, so tests lock keys with a cheap one rather
than paying half a second per unlock. `database.py`'s tests rebuild from synthetic Flex reports,
built as `tests/test_flex.py` builds them, and from the real ones in `examples/flex/` when
present (skipped otherwise), checking the lines against the converter's own output for them.

## Not in step 4

The operations — catching up from the end of the stored data, the cash continuity check at
each join, choosing what to export, a manual re-fetch (D1, D5, D10, D12, D13) — are step 5: a
library module both front ends drive, so `cli.py` and `gui.py` stay thin.

## Order of work

**The DuckDB wheel workflow comes first** (Sergei, 2026-11-14): `database.py` needs our build
to write encrypted files, and developing on the published wheel means the tests run on the
DuckDB that ships, on Windows and the Mac alike. Then the modules, bottom up: `vault.py`,
`locks.py`, `store.py`, `database.py`.

## Decided while building

- <a id="d59"></a>**D59 — One remembered credential per store, kept on this computer** *(Sergei,
  2026-11-19)*. Each store has a random ID, readable in `store.key`, and its remembered key
  (D9) is the credential `IBKR to Xero — store <id>`, user name `store key`. So the current
  store and one set aside by starting over can both be remembered. Credential Manager and
  Keychain Access have no groups or folders; the shared prefix is what keeps a user's entries
  together, and distinct names avoid `keyring`'s renaming of a second entry under the same
  name to `<user>@<name>`. On Windows the credential is persisted on the local computer, not
  as `keyring`'s default "Enterprise", which roams to a domain user's other computers:
  *Remember on this computer* means this one. *Declined:* one fixed entry for every store —
  simpler, but only one store could be remembered at a time.
- <a id="d60"></a>**D60 — A busy lock says only that it is busy** *(Sergei, 2026-11-19; revises
  D21)*. Naming what is running would mean keeping that text in the lock file in plain text,
  where D14 allows nothing readable. So nothing is written into a lock file, and a refused
  operation says only that another IBKR to Xero window or command is working on that account
  (or on the store). *Declined:* the text written while the lock is held and emptied on
  release.
