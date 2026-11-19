"""The store: a per-user folder of ledgers, every file in it encrypted.

One folder per user (plan D7) holds `store.key`, the index of ledgers and a
folder per ledger with its settings, the Flex reports fetched for it and its
two databases (D14). The key comes from the computer's memory of it or from
the password (D9); everything is sealed by vault.py, and runs exclude each
other through locks.py (D21). DuckDB is database.py's: this module only says
where its files are and which key opens them.

On screen a ledger is an "account" (D20), and so it is in this module's
messages; in the code it stays a ledger.

Implements: AD-093, AD-091
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import gst, locks, vault

APP_FOLDER = "IBKR to Xero"
STORE_KEY = "store.key"
STORE_LOCK = "store.lock"
INDEX = "index.enc"
LEDGER_LOCK = "ledger.lock"
LEDGER_SETTINGS = "ledger.enc"
ORIGINALS = "originals"
DATA_DB = "data.db"
EXPORTS_DB = "exports.db"

DEFAULT_GST_RATE = gst.DEFAULT_RATE  # Australia's; a ledger setting (D57)

_INDEX_FORMAT = 1
_SETTINGS_FORMAT = 1
_LEDGER_ID = re.compile(r"[0-9a-f]{16}")
_CONTENT_NAME = re.compile(r"[0-9a-f]{64}")
_CURRENCY = re.compile(r"[A-Z]{3}")


class StoreError(Exception):
    """The store could not be used as asked."""


class NoStore(StoreError):
    """There is no store in the folder."""


class StoreExists(StoreError):
    """A store is already there."""


class PasswordNeeded(StoreError):
    """The key is not remembered on this computer: the password is needed."""


class WrongPassword(StoreError):
    """The password does not open the store."""


class StoreUnreadable(StoreError):
    """A file of the store is damaged, or of a format this version cannot read."""


class StoreBusy(StoreError):
    """Another IBKR to Xero window or command is working on it."""


class CannotRemember(StoreError):
    """The OS credential store would not keep, or let go of, the key."""


class NoSuchLedger(StoreError):
    """No ledger has that ID or name."""


class NameTaken(StoreError):
    """Another ledger already has that name."""


def default_folder() -> Path:
    """The per-user folder the store lives in (D7).

    Implements: AD-095
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:  # not a released platform: running from source
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_FOLDER


@dataclass(frozen=True)
class LedgerEntry:
    """A ledger as the index lists it."""

    id: str
    name: str
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerSettings:
    """What `ledger.enc` holds (D9, D12, D20, D57).

    Implements: AD-100
    """

    flex_token: str = ""
    flex_query_id: str = ""
    accounts: dict[str, str] = field(default_factory=dict)  # IB account -> base currency
    currencies: dict[str, dt.date | None] = field(default_factory=dict)  # -> last date in Xero
    gst_rate: Decimal = DEFAULT_GST_RATE


class _Lock:
    """An OS lock held by one thread, reentrant within it (AD-103)."""

    def __init__(self, path: Path, busy: str):
        self._path = path
        self._busy = busy
        self._owner: int | None = None
        self._depth = 0

    @property
    def held(self) -> bool:
        return self._owner == threading.get_ident()

    @contextlib.contextmanager
    def __call__(self) -> Iterator[None]:
        if self.held:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(locks.locked(self._path))
            except locks.Busy:
                raise StoreBusy(self._busy) from None
            self._owner, self._depth = threading.get_ident(), 1
            try:
                yield
            finally:
                self._owner, self._depth = None, 0


class Store:
    """An open store. Made by `create`, `open` or `start_over`, never directly.

    Implements: AD-093
    """

    def __init__(self, folder: Path, key: bytes, store_id: str):
        self.folder = folder
        self.id = store_id
        self._key = key
        self._lock = _Lock(folder / STORE_LOCK, "Another IBKR to Xero window or command is working on the store.")
        self._ledgers: dict[str, Ledger] = {}

    # --- Creating and opening ---------------------------------------------------

    @classmethod
    def create(
        cls, folder: str | os.PathLike[str], password: str, *, cost: vault.Cost = vault.DEFAULT_COST
    ) -> Store:
        """A new, empty store in `folder`.

        Implements: AD-096
        """
        folder = Path(folder)
        if (folder / STORE_KEY).exists():
            raise StoreExists(f"There is already a store in {folder}.")
        folder.mkdir(parents=True, exist_ok=True)
        store = cls(folder, vault.new_key(), vault.new_store_id())
        with store.locked():
            if (folder / STORE_KEY).exists():  # made meanwhile by another window
                raise StoreExists(f"There is already a store in {folder}.")
            store._write_index([])
            # Last: a store exists once store.key does.
            _write_file(folder / STORE_KEY, vault.lock_key(store._key, password, store.id, cost))
        return store

    @classmethod
    def open(cls, folder: str | os.PathLike[str], password: str | None = None) -> Store:
        """The store in `folder`, opened with the remembered key, else the password.

        Implements: AD-096
        """
        folder = Path(folder)
        try:
            contents = (folder / STORE_KEY).read_bytes()
        except FileNotFoundError:
            raise NoStore(f"There is no store in {folder}.") from None
        try:
            store_id = vault.store_id_of(contents)
            key = vault.remembered(store_id)
            if key is None or not vault.key_opens(contents, key):
                if password is None:
                    raise PasswordNeeded("The password is needed to open the store.")
                key = vault.unlock_key(contents, password)
        except vault.KeyFileUnreadable as error:
            raise StoreUnreadable(str(error)) from None
        except vault.WrongPassword:
            raise WrongPassword("The password is wrong.") from None
        store = cls(folder, key, store_id)
        store._read_index()  # a damaged index is found now, not halfway through a run
        return store

    @classmethod
    def start_over(
        cls, folder: str | os.PathLike[str], password: str, *, cost: vault.Cost = vault.DEFAULT_COST
    ) -> Store:
        """Set the store in `folder` aside, never deleting it, and create a new one (D9, D62).

        Implements: AD-098
        """
        folder = Path(folder)
        if folder.exists():
            aside = _aside_name(folder, dt.date.today())
            try:
                folder.rename(aside)
            except PermissionError:
                raise StoreBusy(
                    "The store is in use: close other IBKR to Xero windows and commands first."
                ) from None
        return cls.create(folder, password, cost=cost)

    # --- The password and the remembered key ------------------------------------

    def change_password(self, new_password: str, *, cost: vault.Cost = vault.DEFAULT_COST) -> None:
        """Lock the store's key with a new password; no current one asked (D63).

        Implements: AD-097
        """
        with self.locked():
            _write_file(self.folder / STORE_KEY, vault.lock_key(self._key, new_password, self.id, cost))

    @property
    def remembered(self) -> bool:
        """Whether this computer remembers the key.

        Implements: AD-096
        """
        return vault.remembered(self.id) == self._key

    def set_remembered(self, on: bool) -> None:
        """Remember the key on this computer, or forget it (D9).

        Implements: AD-096
        """
        try:
            if on:
                vault.remember(self.id, self._key)
            else:
                vault.forget(self.id)
        except vault.VaultError as error:
            raise CannotRemember(str(error)) from None

    # --- Ledgers --------------------------------------------------------------------

    def ledgers(self) -> list[LedgerEntry]:
        """Every ledger, in the order added.

        Implements: AD-099
        """
        return self._read_index()

    def ledger(self, ledger_id: str) -> Ledger:
        """The ledger with this ID; one object per ledger, so its lock is reentrant.

        Implements: AD-099
        """
        if not any(entry.id == ledger_id for entry in self._read_index()):
            raise NoSuchLedger(f"There is no account with ID {ledger_id!r}.")
        if ledger_id not in self._ledgers:
            self._ledgers[ledger_id] = Ledger(self, ledger_id)
        return self._ledgers[ledger_id]

    def find_ledger(self, name: str) -> Ledger:
        """The ledger with this name, ignoring case.

        Implements: AD-099
        """
        wanted = name.strip().casefold()
        for entry in self._read_index():
            if entry.name.casefold() == wanted:
                return self.ledger(entry.id)
        raise NoSuchLedger(f"There is no account named {name!r}.")

    def add_ledger(self, name: str) -> Ledger:
        """A new, empty ledger.

        Implements: AD-099
        """
        name = _clean_name(name)
        with self.locked():
            entries = self._read_index()
            _check_name_free(entries, name)
            taken = {entry.id for entry in entries}
            ledger_id = secrets.token_hex(8)
            while ledger_id in taken or (self.folder / ledger_id).exists():
                ledger_id = secrets.token_hex(8)
            (self.folder / ledger_id / ORIGINALS).mkdir(parents=True)
            ledger = Ledger(self, ledger_id)
            ledger.save_settings(LedgerSettings())
            # Last: the index names only a ledger that is complete.
            self._write_index([*entries, LedgerEntry(ledger_id, name)])
            self._ledgers[ledger_id] = ledger
        return ledger

    def rename_ledger(self, ledger_id: str, name: str) -> None:
        """Implements: AD-099"""
        name = _clean_name(name)
        with self.locked():
            entries = self._read_index()
            entry = _entry(entries, ledger_id)
            _check_name_free([e for e in entries if e.id != ledger_id], name)
            self._write_index([_replace_entry(e, entry, name=name) for e in entries])

    def set_accounts(self, ledger_id: str, accounts: Iterable[str]) -> None:
        """The IB accounts a ledger covers, as its reports show them (D20).

        Implements: AD-099
        """
        accounts = tuple(accounts)
        if not all(isinstance(a, str) and a.strip() == a and a for a in accounts):
            raise ValueError(f"not IB account IDs: {accounts!r}")
        with self.locked():
            entries = self._read_index()
            entry = _entry(entries, ledger_id)
            self._write_index([_replace_entry(e, entry, accounts=accounts) for e in entries])

    def remove_ledger(self, ledger_id: str) -> None:
        """Delete a ledger and everything of it (D61).

        Implements: AD-099
        """
        ledger = self.ledger(ledger_id)
        if ledger._lock.held:
            raise RuntimeError("a ledger is removed outside its own lock")
        with self.locked():
            with ledger.locked():  # refused while a run works on it
                entries = self._read_index()
                self._write_index([e for e in entries if e.id != ledger_id])
            # The lock file is closed now; Windows deletes no open file.
            shutil.rmtree(ledger.folder)
            del self._ledgers[ledger_id]

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store-wide lock (D21): for the index and the password.

        Implements: AD-103, AD-092
        """
        with self._lock():
            yield

    # --- The index ------------------------------------------------------------------

    def _read_index(self) -> list[LedgerEntry]:
        document = _read_sealed(self._key, self.folder / INDEX, "index", "The list of accounts")
        try:
            if document["format"] != _INDEX_FORMAT:
                raise ValueError(f"format {document['format']!r}, which this version cannot read")
            entries = []
            for item in document["ledgers"]:
                ledger_id, name, accounts = item["id"], item["name"], item["accounts"]
                if not (isinstance(ledger_id, str) and _LEDGER_ID.fullmatch(ledger_id)):
                    raise ValueError(f"not a ledger ID: {ledger_id!r}")
                if not isinstance(name, str) or not all(isinstance(a, str) for a in accounts):
                    raise ValueError("a name or account is not text")
                entries.append(LedgerEntry(ledger_id, name, tuple(accounts)))
            return entries
        except (KeyError, TypeError, ValueError) as error:
            raise StoreUnreadable(f"The list of accounts is damaged ({error}).") from None

    def _write_index(self, entries: list[LedgerEntry]) -> None:
        document = {
            "format": _INDEX_FORMAT,
            "ledgers": [{"id": e.id, "name": e.name, "accounts": list(e.accounts)} for e in entries],
        }
        _write_sealed(self._key, self.folder / INDEX, "index", document)


class Ledger:
    """One ledger of an open store: its settings, originals and databases.

    Implements: AD-099
    """

    def __init__(self, store: Store, ledger_id: str):
        self.id = ledger_id
        self.folder = store.folder / ledger_id
        self._store = store
        self._lock = _Lock(
            self.folder / LEDGER_LOCK,
            "Another IBKR to Xero window or command is working on this account.",
        )

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this ledger's lock (D21): for the length of an operation.

        Implements: AD-103, AD-092
        """
        with self._lock():
            yield

    # --- Settings -------------------------------------------------------------------

    def settings(self) -> LedgerSettings:
        """Implements: AD-100"""
        document = _read_sealed(
            self._store._key, self.folder / LEDGER_SETTINGS, f"ledger:{self.id}", "An account's settings"
        )
        try:
            return _settings_from_json(document)
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise StoreUnreadable(f"An account's settings are damaged ({error}).") from None

    def save_settings(self, settings: LedgerSettings) -> None:
        """Implements: AD-100"""
        document = _settings_to_json(settings)
        with self.locked():
            _write_sealed(self._store._key, self.folder / LEDGER_SETTINGS, f"ledger:{self.id}", document)

    # --- Originals ------------------------------------------------------------------

    def add_original(self, data: bytes) -> str:
        """Keep a fetched report as received (D6); its name, the same for the same report.

        Implements: AD-101
        """
        name = vault.content_name(self._store._key, data)
        path = self.folder / ORIGINALS / f"{name}.enc"
        with self.locked():
            if not path.exists():
                _write_file(path, vault.seal(self._store._key, data, self._original_purpose(name)))
        return name

    def originals(self) -> list[str]:
        """The names of every report kept, sorted.

        Implements: AD-101
        """
        return sorted(
            path.stem
            for path in (self.folder / ORIGINALS).glob("*.enc")
            if _CONTENT_NAME.fullmatch(path.stem)
        )

    def read_original(self, name: str) -> bytes:
        """Implements: AD-101"""
        if not _CONTENT_NAME.fullmatch(name):
            raise ValueError(f"not an original's name: {name!r}")
        path = self.folder / ORIGINALS / f"{name}.enc"
        try:
            return vault.unseal(self._store._key, path.read_bytes(), self._original_purpose(name))
        except FileNotFoundError:
            raise StoreUnreadable(f"A fetched report is missing: {name}.") from None
        except vault.Damaged:
            raise StoreUnreadable(f"A fetched report is damaged: {name}.") from None

    def _original_purpose(self, name: str) -> str:
        return f"original:{self.id}/{name}"

    # --- Databases ------------------------------------------------------------------

    @property
    def data_db(self) -> Path:
        """Implements: AD-102"""
        return self.folder / DATA_DB

    @property
    def exports_db(self) -> Path:
        """Implements: AD-102"""
        return self.folder / EXPORTS_DB

    @property
    def database_key(self) -> str:
        """Implements: AD-102"""
        return vault.database_key(self._store._key)


# --- Files --------------------------------------------------------------------------


def _write_file(path: Path, data: bytes) -> None:
    """Replace `path` with `data`: the old file or the new, never part of either.

    Implements: AD-104
    """
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # Windows refuses while another process has the file open to read
                # it, which lasts a moment.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _write_sealed(key: bytes, path: Path, purpose: str, document: dict) -> None:
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _write_file(path, vault.seal(key, data, purpose))


def _read_sealed(key: bytes, path: Path, purpose: str, what: str) -> dict:
    try:
        data = vault.unseal(key, path.read_bytes(), purpose)
    except FileNotFoundError:
        raise StoreUnreadable(f"{what} is missing from the store.") from None
    except vault.Damaged:
        raise StoreUnreadable(f"{what} is damaged.") from None
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise StoreUnreadable(f"{what} is damaged ({error}).") from None
    if not isinstance(document, dict):
        raise StoreUnreadable(f"{what} is damaged.")
    return document


def _aside_name(folder: Path, today: dt.date) -> Path:
    base = f"{folder.name} (password forgotten {today.isoformat()})"
    aside, number = folder.with_name(base), 1
    while aside.exists():
        number += 1
        aside = folder.with_name(f"{base} {number}")
    return aside


# --- Ledger names and settings ----------------------------------------------------


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("An account needs a name.")
    return name


def _check_name_free(entries: list[LedgerEntry], name: str) -> None:
    if any(entry.name.casefold() == name.casefold() for entry in entries):
        raise NameTaken(f"There is already an account named {name!r}.")


def _entry(entries: list[LedgerEntry], ledger_id: str) -> LedgerEntry:
    for entry in entries:
        if entry.id == ledger_id:
            return entry
    raise NoSuchLedger(f"There is no account with ID {ledger_id!r}.")


def _replace_entry(entry: LedgerEntry, target: LedgerEntry, **changes) -> LedgerEntry:
    if entry.id != target.id:
        return entry
    return LedgerEntry(
        entry.id, changes.get("name", entry.name), changes.get("accounts", entry.accounts)
    )


def _settings_to_json(settings: LedgerSettings) -> dict:
    return {
        "format": _SETTINGS_FORMAT,
        "flex_token": settings.flex_token,
        "flex_query_id": settings.flex_query_id,
        "accounts": dict(settings.accounts),
        "currencies": {
            code: None if date is None else date.isoformat()
            for code, date in settings.currencies.items()
        },
        "gst_rate": str(settings.gst_rate),
    }


def _settings_from_json(document: dict) -> LedgerSettings:
    if document["format"] != _SETTINGS_FORMAT:
        raise ValueError(f"format {document['format']!r}, which this version cannot read")
    defaults = LedgerSettings()
    token = document.get("flex_token", defaults.flex_token)
    query_id = document.get("flex_query_id", defaults.flex_query_id)
    accounts = document.get("accounts", {})
    currencies = document.get("currencies", {})
    if not (isinstance(token, str) and isinstance(query_id, str)):
        raise ValueError("the Flex token or query ID is not text")
    if not isinstance(accounts, dict) or not all(
        isinstance(a, str) and isinstance(c, str) and _CURRENCY.fullmatch(c)
        for a, c in accounts.items()
    ):
        raise ValueError("an account's base currency is not a currency code")
    if not isinstance(currencies, dict) or not all(
        _CURRENCY.fullmatch(c) and (d is None or isinstance(d, str)) for c, d in currencies.items()
    ):
        raise ValueError("a currency's date is not a date")
    rate = document.get("gst_rate")
    gst_rate = defaults.gst_rate if rate is None else Decimal(rate)
    if not (isinstance(rate, (str, type(None))) and 0 <= gst_rate < 1):
        raise ValueError(f"not a GST rate: {rate!r}")
    return LedgerSettings(
        flex_token=token,
        flex_query_id=query_id,
        accounts=dict(accounts),
        currencies={c: None if d is None else dt.date.fromisoformat(d) for c, d in currencies.items()},
        gst_rate=gst_rate,
    )
