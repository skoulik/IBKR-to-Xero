"""vault.py: keys, store.key, sealed files and the remembered key.

Keys are locked with a cheap scrypt cost, except in one test of the default;
an in-memory keyring stands in for the OS credential store, and one test per
platform checks the real Credential Manager or Keychain.

Implements: AD-087
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys

import pytest

from ibkr_to_xero import vault
from ibkr_to_xero.vault import (
    Cost,
    Damaged,
    KeyFileUnreadable,
    VaultError,
    WrongPassword,
)

CHEAP = Cost(n=2**4, r=8, p=1)
STORE_ID = "7f3c9a2e"
KEY = bytes(range(32))


def _lock(key=KEY, password="correct horse", store_id=STORE_ID):
    return vault.lock_key(key, password, store_id, CHEAP)


def _edit(contents: bytes, change) -> bytes:
    document = json.loads(contents)
    change(document)
    return json.dumps(document).encode()


# --- Keys and IDs -------------------------------------------------------------


def test_new_keys_are_random_32_bytes():
    keys = {vault.new_key() for _ in range(10)}
    assert len(keys) == 10
    assert all(len(key) == 32 for key in keys)


def test_new_store_ids_are_eight_hex_digits():
    ids = {vault.new_store_id() for _ in range(10)}
    assert len(ids) == 10
    assert all(len(i) == 8 and int(i, 16) >= 0 and i == i.lower() for i in ids)


def test_content_names_are_keyed_so_they_reveal_nothing():
    import hashlib

    name = vault.content_name(KEY, b"<report/>")
    assert name == vault.content_name(KEY, b"<report/>")
    assert len(name) == 64 and int(name, 16) >= 0
    assert name != hashlib.sha256(b"<report/>").hexdigest()
    assert name != vault.content_name(vault.new_key(), b"<report/>")
    assert name != vault.content_name(KEY, b"<report />")


def test_the_database_key_is_derived_text_never_the_key_itself():
    derived = vault.database_key(KEY)
    assert derived == vault.database_key(KEY)
    assert len(derived) == 64 and int(derived, 16) >= 0
    assert KEY.hex() not in derived
    assert vault.database_key(vault.new_key()) != derived


# --- store.key ----------------------------------------------------------------


def test_a_locked_key_unlocks_with_its_password():
    contents = _lock()
    assert vault.unlock_key(contents, "correct horse") == KEY
    assert vault.store_id_of(contents) == STORE_ID


def test_a_wrong_password_is_reported_as_such():
    with pytest.raises(WrongPassword):
        vault.unlock_key(_lock(), "correct hose")


def test_store_key_is_json_with_nothing_of_the_key_in_clear():
    contents = _lock()
    document = json.loads(contents)
    assert document["format"] == 1 and document["store"] == STORE_ID
    assert document["scrypt"]["n"] == CHEAP.n
    assert KEY not in contents and base64.b64encode(KEY) not in contents
    assert KEY.hex().encode() not in contents


def test_every_lock_differs_so_a_new_password_shares_nothing_with_the_old():
    first, second = _lock(), _lock()
    assert first != second
    assert json.loads(first)["scrypt"]["salt"] != json.loads(second)["scrypt"]["salt"]


def test_the_password_is_compared_in_unicode_nfc():
    composed, decomposed = "café", "café"
    assert vault.unlock_key(_lock(password=composed), decomposed) == KEY


def test_key_opens_tells_this_store_key_from_another():
    contents = _lock()
    assert vault.key_opens(contents, KEY)
    assert not vault.key_opens(contents, vault.new_key())
    assert not vault.key_opens(contents, b"short")


def test_the_default_cost_works_and_is_recorded():
    assert vault.DEFAULT_COST == Cost(n=2**17, r=8, p=1)
    contents = vault.lock_key(KEY, "pw", STORE_ID)
    scrypt = json.loads(contents)["scrypt"]
    assert (scrypt["n"], scrypt["r"], scrypt["p"]) == (2**17, 8, 1)
    assert vault.unlock_key(contents, "pw") == KEY


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"not json",
        b"[1, 2]",
        b'{"format": 1}',
        b"\xff\xfe",
    ],
)
def test_unparseable_store_key_is_unreadable_not_a_wrong_password(contents):
    with pytest.raises(KeyFileUnreadable):
        vault.unlock_key(contents, "correct horse")
    with pytest.raises(KeyFileUnreadable):
        vault.store_id_of(contents)


def test_another_format_is_named():
    contents = _edit(_lock(), lambda d: d.update(format=2))
    with pytest.raises(KeyFileUnreadable, match="format 2"):
        vault.unlock_key(contents, "correct horse")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(store="not-an-id"),
        lambda d: d.update(nonce="!!!"),
        lambda d: d.update(key=base64.b64encode(b"x" * 10).decode()),
        lambda d: d["scrypt"].update(n=1000),  # not a power of two
        lambda d: d["scrypt"].update(n=2**30),  # 1 TiB: refused, not attempted
        lambda d: d["scrypt"].update(r=True),
    ],
)
def test_damaged_fields_are_unreadable(change):
    with pytest.raises(KeyFileUnreadable):
        vault.unlock_key(_edit(_lock(), change), "correct horse")


def test_a_check_value_that_does_not_match_is_damage():
    other = base64.b64encode(bytes(16)).decode()
    contents = _edit(_lock(), lambda d: d.update(check=other))
    with pytest.raises(KeyFileUnreadable, match="check value"):
        vault.unlock_key(contents, "correct horse")
    assert not vault.key_opens(contents, KEY)


def test_bad_arguments_are_programming_errors():
    with pytest.raises(ValueError):
        vault.lock_key(b"short", "pw", STORE_ID, CHEAP)
    with pytest.raises(ValueError):
        vault.lock_key(KEY, "pw", "STORE", CHEAP)
    with pytest.raises(ValueError):
        vault.lock_key(KEY, "pw", STORE_ID, Cost(n=3, r=8, p=1))


# --- Sealed files ---------------------------------------------------------------


def test_sealed_data_opens_for_its_purpose():
    data = b"<FlexQueryResponse/>" * 1000
    sealed = vault.seal(KEY, data, "original:abc")
    assert vault.unseal(KEY, sealed, "original:abc") == data
    assert len(sealed) < len(data) // 10  # compressed before it was encrypted
    assert b"FlexQueryResponse" not in sealed


def test_empty_data_seals():
    assert vault.unseal(KEY, vault.seal(KEY, b"", "index"), "index") == b""


def test_every_seal_differs():
    assert vault.seal(KEY, b"same", "index") != vault.seal(KEY, b"same", "index")


def test_a_file_cannot_stand_in_for_another():
    sealed = vault.seal(KEY, b"ledger settings", "ledger:aaaa")
    with pytest.raises(Damaged):
        vault.unseal(KEY, sealed, "ledger:bbbb")


def test_another_key_cannot_open_it():
    with pytest.raises(Damaged):
        vault.unseal(vault.new_key(), vault.seal(KEY, b"x", "index"), "index")


@pytest.mark.parametrize("position", [0, 1, 13, -1])
def test_any_changed_byte_is_damage(position):
    sealed = bytearray(vault.seal(KEY, b"some data", "index"))
    sealed[position] ^= 0x01
    with pytest.raises(Damaged):
        vault.unseal(KEY, bytes(sealed), "index")


def test_a_truncated_file_is_damage():
    sealed = vault.seal(KEY, b"some data", "index")
    for length in (0, 1, 28, len(sealed) - 1):
        with pytest.raises(Damaged):
            vault.unseal(KEY, sealed[:length], "index")


def test_another_format_byte_is_named():
    sealed = b"\x02" + vault.seal(KEY, b"x", "index")[1:]
    with pytest.raises(Damaged, match="format 2"):
        vault.unseal(KEY, sealed, "index")


# --- The remembered key (memory_keyring: conftest.py) ------------------------------


def test_a_remembered_key_comes_back(memory_keyring):
    vault.remember(STORE_ID, KEY)
    assert vault.remembered(STORE_ID) == KEY
    assert memory_keyring.entries == {
        ("IBKR to Xero — store 7f3c9a2e", "store key"): base64.b64encode(KEY).decode()
    }


def test_each_store_has_its_own_credential(memory_keyring):
    other_key = vault.new_key()
    vault.remember(STORE_ID, KEY)
    vault.remember("0badcafe", other_key)
    assert vault.remembered(STORE_ID) == KEY
    assert vault.remembered("0badcafe") == other_key
    assert len(memory_keyring.entries) == 2


def test_remembering_again_replaces(memory_keyring):
    new = vault.new_key()
    vault.remember(STORE_ID, KEY)
    vault.remember(STORE_ID, new)
    assert vault.remembered(STORE_ID) == new
    assert len(memory_keyring.entries) == 1


def test_nothing_remembered_is_none(memory_keyring):
    assert vault.remembered(STORE_ID) is None


def test_a_malformed_credential_is_none(memory_keyring):
    service = ("IBKR to Xero — store 7f3c9a2e", "store key")
    for value in ("not base64!", base64.b64encode(b"short").decode()):
        memory_keyring.entries[service] = value
        assert vault.remembered(STORE_ID) is None


def test_forget_removes_and_tolerates_nothing_to_remove(memory_keyring):
    vault.remember(STORE_ID, KEY)
    vault.forget(STORE_ID)
    assert vault.remembered(STORE_ID) is None
    vault.forget(STORE_ID)


def test_a_failing_credential_store(memory_keyring):
    memory_keyring.fail = True
    assert vault.remembered(STORE_ID) is None  # the password is asked instead
    with pytest.raises(VaultError, match="could not be remembered"):
        vault.remember(STORE_ID, KEY)
    with pytest.raises(VaultError, match="could not be removed"):
        vault.forget(STORE_ID)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_credential_manager_keeps_one_local_credential():
    from win32ctypes.pywin32 import pywintypes, win32cred

    store_id = vault.new_store_id()
    name = f"IBKR to Xero — store {store_id}"

    def ours():
        try:
            everything = win32cred.CredEnumerate(None, 0)
        except pywintypes.error as error:
            if error.winerror != 1168:  # ERROR_NOT_FOUND: the user has no credentials at all
                raise
            everything = []  # as on a fresh CI runner, once ours is forgotten
        return [c for c in everything if store_id in c["TargetName"]]

    try:
        vault.remember(store_id, vault.new_key())
        key = vault.new_key()
        vault.remember(store_id, key)  # keyring alone would leave a "store key@…" copy
        [credential] = ours()
        assert credential["TargetName"] == name
        assert credential["UserName"] == "store key"
        assert credential["Persist"] == win32cred.CRED_PERSIST_LOCAL_MACHINE
        assert vault.remembered(store_id) == key
        vault.forget(store_id)
        assert ours() == [] and vault.remembered(store_id) is None
    finally:
        for credential in ours():
            try:
                win32cred.CredDelete(credential["TargetName"], win32cred.CRED_TYPE_GENERIC)
            except pywintypes.error:
                pass


def _security(*args):
    return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
def test_macos_keychain_keeps_one_item_in_the_default_keychain():
    found = _security("default-keychain")
    default = found.stdout.strip().strip('"')
    info = _security("show-keychain-info", default)
    if info.returncode != 0:
        pytest.skip(
            f"the default keychain is not usable here: default-keychain gave {found.stdout.strip()!r}"
            f" {found.stderr.strip()!r}; show-keychain-info exited {info.returncode}:"
            f" {info.stderr.strip()!r}"
        )
    store_id = vault.new_store_id()
    name = f"IBKR to Xero — store {store_id}"

    def find():
        return _security("find-generic-password", "-s", name, "-a", "store key", default)

    try:
        vault.remember(store_id, vault.new_key())
        key = vault.new_key()
        vault.remember(store_id, key)
        assert find().returncode == 0, "not in the default keychain"
        assert vault.remembered(store_id) == key
        vault.forget(store_id)
        assert find().returncode == 44, "an item is left: remembering twice kept two"
        assert vault.remembered(store_id) is None
    finally:
        for _ in range(5):
            if find().returncode != 0:
                break
            _security("delete-generic-password", "-s", name, "-a", "store key", default)
