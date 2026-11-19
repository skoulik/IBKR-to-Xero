"""store.py: the folder, the password, ledgers, settings, originals and locks.

Every test runs on a temporary folder with a cheap scrypt cost and an
in-memory credential store (conftest.py).

Implements: AD-095, AD-096, AD-097, AD-098, AD-099, AD-100, AD-101, AD-102, AD-103, AD-104
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_to_xero import store as store_module
from ibkr_to_xero import vault
from ibkr_to_xero.store import (
    LedgerEntry,
    LedgerSettings,
    NameTaken,
    NoStore,
    NoSuchLedger,
    PasswordNeeded,
    Store,
    StoreBusy,
    StoreExists,
    StoreUnreadable,
    WrongPassword,
)

CHEAP = vault.Cost(n=2**4, r=8, p=1)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_credential_store(memory_keyring):
    return memory_keyring


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "IBKR to Xero"


@pytest.fixture
def store(folder):
    return Store.create(folder, "correct horse", cost=CHEAP)


# --- Creating and opening ---------------------------------------------------------


def test_a_new_store_opens_with_its_password(store, folder):
    opened = Store.open(folder, "correct horse")
    assert opened.id == store.id
    assert opened.ledgers() == []


def test_without_a_remembered_key_the_password_is_needed(store, folder):
    with pytest.raises(PasswordNeeded):
        Store.open(folder)


def test_a_wrong_password(store, folder):
    with pytest.raises(WrongPassword):
        Store.open(folder, "correct hose")


def test_no_store_in_the_folder(folder):
    with pytest.raises(NoStore):
        Store.open(folder, "pw")


def test_a_store_is_never_created_over_another(store, folder):
    with pytest.raises(StoreExists):
        Store.create(folder, "another", cost=CHEAP)
    assert Store.open(folder, "correct horse").id == store.id


def test_a_remembered_key_opens_without_the_password(store, folder):
    assert not store.remembered
    store.set_remembered(True)
    assert store.remembered
    assert Store.open(folder).id == store.id
    store.set_remembered(False)
    assert not store.remembered
    with pytest.raises(PasswordNeeded):
        Store.open(folder)


def test_a_remembered_key_that_no_longer_opens_means_asking(store, folder, memory_keyring):
    vault.remember(store.id, vault.new_key())
    with pytest.raises(PasswordNeeded):
        Store.open(folder)
    assert Store.open(folder, "correct horse").id == store.id


def test_a_credential_store_that_fails(store, memory_keyring):
    memory_keyring.fail = True
    with pytest.raises(store_module.CannotRemember):
        store.set_remembered(True)
    assert not store.remembered


def test_damaged_store_key_or_index_is_unreadable(store, folder):
    (folder / "index.enc").write_bytes(b"\x01" + bytes(40))
    with pytest.raises(StoreUnreadable):
        Store.open(folder, "correct horse")
    (folder / "store.key").write_text("{}")
    with pytest.raises(StoreUnreadable):
        Store.open(folder, "correct horse")


def test_the_default_folder_is_per_user_application_data():
    folder = store_module.default_folder()
    assert folder.name == "IBKR to Xero"
    if sys.platform == "win32":
        assert folder.parent == Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        assert folder.parent == Path.home() / "Library" / "Application Support"


# --- The password -----------------------------------------------------------------


def test_changing_the_password_rewrites_store_key_alone(store, folder):
    ledger = store.add_ledger("Main")
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file() and p.name != "store.key"}
    store.change_password("new horse", cost=CHEAP)
    after = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file() and p.name != "store.key"}
    assert before == after
    with pytest.raises(WrongPassword):
        Store.open(folder, "correct horse")
    reopened = Store.open(folder, "new horse")
    assert reopened.ledger(ledger.id).settings() == LedgerSettings()


def test_a_remembered_key_survives_a_password_change(store, folder):
    store.set_remembered(True)
    store.change_password("new horse", cost=CHEAP)
    assert Store.open(folder).id == store.id


# --- Starting over ----------------------------------------------------------------


def test_starting_over_sets_the_old_store_aside(store, folder):
    store.add_ledger("Main")
    fresh = Store.start_over(folder, "fresh start", cost=CHEAP)
    today = dt.date.today().isoformat()
    aside = folder.with_name(f"IBKR to Xero (password forgotten {today})")
    assert fresh.ledgers() == [] and fresh.id != store.id
    assert Store.open(folder, "fresh start").id == fresh.id
    old = Store.open(aside, "correct horse")  # remembered later: still opens
    assert [e.name for e in old.ledgers()] == ["Main"]


def test_a_second_start_over_the_same_day_is_numbered(store, folder):
    Store.start_over(folder, "second", cost=CHEAP)
    Store.start_over(folder, "third", cost=CHEAP)
    today = dt.date.today().isoformat()
    base = f"IBKR to Xero (password forgotten {today})"
    assert Store.open(folder.with_name(base), "correct horse").id == store.id
    assert Store.open(folder.with_name(f"{base} 2"), "second")
    assert Store.open(folder, "third")


def test_starting_over_with_no_store_just_creates(folder):
    assert Store.start_over(folder, "pw", cost=CHEAP).ledgers() == []
    assert list(folder.parent.iterdir()) == [folder]


# --- Ledgers ----------------------------------------------------------------------


def test_ledgers_are_added_listed_and_found(store):
    main = store.add_ledger("  Main  ")
    other = store.add_ledger("Segregated")
    assert store.ledgers() == [LedgerEntry(main.id, "Main"), LedgerEntry(other.id, "Segregated")]
    assert store.find_ledger("main") is main
    assert store.ledger(other.id) is other
    assert re.fullmatch(r"[0-9a-f]{16}", main.id)


def test_names_are_unique_ignoring_case_and_not_empty(store):
    store.add_ledger("Main")
    with pytest.raises(NameTaken):
        store.add_ledger("MAIN")
    with pytest.raises(ValueError):
        store.add_ledger("   ")


def test_rename_and_accounts(store):
    main = store.add_ledger("Main")
    other = store.add_ledger("Other")
    store.rename_ledger(main.id, "Primary")
    store.rename_ledger(main.id, "primary")  # its own name, recased
    with pytest.raises(NameTaken):
        store.rename_ledger(other.id, "Primary")
    store.set_accounts(main.id, ["U1234567", "U7654321"])
    assert store.ledgers()[0] == LedgerEntry(main.id, "primary", ("U1234567", "U7654321"))


def test_unknown_ledgers(store):
    with pytest.raises(NoSuchLedger):
        store.ledger("0" * 16)
    with pytest.raises(NoSuchLedger):
        store.find_ledger("nothing")
    with pytest.raises(NoSuchLedger):
        store.rename_ledger("0" * 16, "x")


def test_removing_a_ledger_deletes_it(store, folder):
    main = store.add_ledger("Main")
    main.add_original(b"<report/>")
    store.remove_ledger(main.id)
    assert store.ledgers() == []
    assert not main.folder.exists()
    with pytest.raises(NoSuchLedger):
        store.ledger(main.id)


def test_the_folder_says_nothing_of_what_it_holds(store, folder):
    main = store.add_ledger("My Main Account")
    store.set_accounts(main.id, ["U1234567"])
    main.save_settings(LedgerSettings(flex_token="123456789012345678901234", flex_query_id="1705412"))
    main.add_original(b"<FlexQueryResponse accountId='U1234567'/>")
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*")}
    for name in names:
        assert re.fullmatch(
            r"store\.key|store\.lock|index\.enc|[0-9a-f]{16}(/(ledger\.enc|ledger\.lock|originals"
            r"(/[0-9a-f]{64}\.enc)?))?",
            name,
        ), name
    for path in folder.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for secret in (b"My Main", b"U1234567", b"1705412", b"123456789012"):
                assert secret not in content, (path.name, secret)


# --- Settings ---------------------------------------------------------------------


def test_a_new_ledger_has_default_settings(store):
    settings = store.add_ledger("Main").settings()
    assert settings == LedgerSettings()
    assert settings.gst_rate == Decimal("0.10")


def test_settings_round_trip(store, folder):
    main = store.add_ledger("Main")
    settings = LedgerSettings(
        flex_token="123456789012345678901234",
        flex_query_id="1705412",
        accounts={"U1234567": "AUD"},
        currencies={"AUD": dt.date(2026, 6, 30), "USD": None},
        gst_rate=Decimal("0.15"),
    )
    main.save_settings(settings)
    assert Store.open(folder, "correct horse").ledger(main.id).settings() == settings


def test_settings_from_an_older_file_take_defaults(store):
    main = store.add_ledger("Main")
    store_module._write_sealed(
        store._key, main.folder / "ledger.enc", f"ledger:{main.id}", {"format": 1}
    )
    assert main.settings() == LedgerSettings()


@pytest.mark.parametrize(
    "document",
    [
        {"format": 2},
        {"format": 1, "gst_rate": 0.1},
        {"format": 1, "gst_rate": "1.5"},
        {"format": 1, "accounts": {"U1": "aud"}},
        {"format": 1, "currencies": {"AUD": "30/06/2026"}},
        {"format": 1, "flex_token": 123},
    ],
)
def test_damaged_settings_are_unreadable(store, document):
    main = store.add_ledger("Main")
    store_module._write_sealed(store._key, main.folder / "ledger.enc", f"ledger:{main.id}", document)
    with pytest.raises(StoreUnreadable):
        main.settings()


def test_one_ledgers_settings_cannot_stand_in_for_anothers(store):
    main, other = store.add_ledger("Main"), store.add_ledger("Other")
    main.save_settings(LedgerSettings(flex_query_id="1"))
    (other.folder / "ledger.enc").write_bytes((main.folder / "ledger.enc").read_bytes())
    with pytest.raises(StoreUnreadable):
        other.settings()


# --- Originals --------------------------------------------------------------------


def test_originals_are_kept_once_and_read_back(store):
    main = store.add_ledger("Main")
    report = b"<FlexQueryResponse>" + b"x" * 5000 + b"</FlexQueryResponse>"
    name = main.add_original(report)
    assert main.add_original(report) == name
    assert re.fullmatch(r"[0-9a-f]{64}", name)
    assert main.originals() == [name]
    assert main.read_original(name) == report
    assert b"FlexQueryResponse" not in (main.folder / "originals" / f"{name}.enc").read_bytes()


def test_content_names_are_keyed_per_store(store, tmp_path):
    other = Store.create(tmp_path / "other", "pw", cost=CHEAP)
    report = b"<same report/>"
    assert store.add_ledger("A").add_original(report) != other.add_ledger("A").add_original(report)


def test_an_original_cannot_move_between_ledgers(store):
    main, other = store.add_ledger("Main"), store.add_ledger("Other")
    name = main.add_original(b"<report/>")
    source = main.folder / "originals" / f"{name}.enc"
    (other.folder / "originals" / source.name).write_bytes(source.read_bytes())
    with pytest.raises(StoreUnreadable, match="damaged"):
        other.read_original(name)


def test_a_missing_original(store):
    main = store.add_ledger("Main")
    with pytest.raises(StoreUnreadable, match="missing"):
        main.read_original("0" * 64)
    with pytest.raises(ValueError):
        main.read_original("../index")


# --- Databases --------------------------------------------------------------------


def test_database_paths_and_key(store):
    main = store.add_ledger("Main")
    assert main.data_db == main.folder / "data.db"
    assert main.exports_db == main.folder / "exports.db"
    assert main.database_key == vault.database_key(store._key)


# --- Locks ------------------------------------------------------------------------


def test_locks_are_reentrant_within_their_holder(store):
    with store.locked():
        main = store.add_ledger("Main")  # takes the store lock itself
        with main.locked():
            main.save_settings(LedgerSettings(flex_query_id="1"))  # and the ledger's


def test_another_object_on_the_same_store_is_refused(store, folder):
    other = Store.open(folder, "correct horse")
    main = store.add_ledger("Main")
    with store.locked():
        with pytest.raises(StoreBusy, match="working on the store"):
            other.add_ledger("Other")
    with main.locked():
        with pytest.raises(StoreBusy, match="working on this account"):
            other.ledger(main.id).save_settings(LedgerSettings())
        with pytest.raises(StoreBusy):
            other.remove_ledger(main.id)
    assert [e.name for e in store.ledgers()] == ["Main"]


def test_another_thread_is_refused(store):
    main = store.add_ledger("Main")
    refused = []

    def other_thread():
        try:
            main.save_settings(LedgerSettings())
        except StoreBusy:
            refused.append(True)

    with main.locked():
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
    assert refused == [True]


def test_another_process_is_refused(store, folder):
    main = store.add_ledger("Main")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from ibkr_to_xero.locks import locked\n"
            "with locked(sys.argv[1]):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(60)\n",
            str(main.folder / "ledger.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StoreBusy, match="this account"):
            main.add_original(b"<report/>")
        store.add_ledger("Other")  # other ledgers are unaffected
    finally:
        holder.kill()
        holder.wait()


# --- Writing ----------------------------------------------------------------------


def test_a_write_leaves_no_temporary_files(store, folder):
    main = store.add_ledger("Main")
    for n in range(3):
        main.save_settings(LedgerSettings(flex_query_id=str(n)))
    assert not [p for p in folder.rglob("*.tmp")]


def test_a_failed_write_keeps_the_old_file(store, folder, monkeypatch):
    main = store.add_ledger("Main")
    main.save_settings(LedgerSettings(flex_query_id="old"))

    def broken_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        main.save_settings(LedgerSettings(flex_query_id="new"))
    monkeypatch.undo()
    assert main.settings().flex_query_id == "old"
    assert not [p for p in folder.rglob("*.tmp")]
