"""The store's keys, and the sealing of its files.

One random key per store encrypts everything in it (plan D8, D9). `store.key`
holds that key locked by the user's password with scrypt, slow on purpose so
that guessing against a copy of the folder is slow too; the OS credential
store can remember the key, so the password is not asked for again on this
computer (D9, D59). Files are compressed, then sealed with AES-GCM, each bound
to its purpose so that one cannot stand in for another (D14). DuckDB gets a
key derived from the store key, never the key itself.

Bytes in, bytes out: which files these are, and where, is store.py's business.

Implements: AD-081, AD-087
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import sys
import unicodedata
import zlib
from dataclasses import dataclass

import keyring
import keyring.errors
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from keyring.backend import KeyringBackend

KEY_BYTES = 32
_NONCE_BYTES = 12  # AES-GCM's standard nonce
_TAG_BYTES = 16
_SALT_BYTES = 16
_CHECK_BYTES = 16
_STORE_ID = re.compile(r"[0-9a-f]{8}")

_KEY_FILE_FORMAT = 1
_KEY_FILE_AAD = b"ibkr2xero store.key 1"
_SEAL_FORMAT = 1

_CREDENTIAL = "IBKR to Xero \N{EM DASH} store {}"
_CREDENTIAL_USER = "store key"

# A cost read from store.key is used only if it is sane: a damaged or hostile
# file must not make the unlock allocate gigabytes.
_MAX_SCRYPT_MEMORY = 1 << 30


class VaultError(Exception):
    """A key or a sealed file could not be used."""


class WrongPassword(VaultError):
    """The password does not open the store's key."""


class KeyFileUnreadable(VaultError):
    """`store.key` is damaged, or in a format this version cannot read."""


class Damaged(VaultError):
    """A sealed file does not open with the store's key: damaged or replaced."""


@dataclass(frozen=True)
class Cost:
    """scrypt's cost: `n` (a power of two), `r` and `p`; memory is 128 * n * r bytes."""

    n: int
    r: int
    p: int


# About half a second and 128 MiB (0.48 s measured on a desktop, 2026-11-19).
DEFAULT_COST = Cost(n=2**17, r=8, p=1)


# --- Keys and IDs -------------------------------------------------------------


def new_key() -> bytes:
    """A new store key.

    Implements: AD-082
    """
    return secrets.token_bytes(KEY_BYTES)


def new_store_id() -> str:
    """A new store ID: eight hex digits, naming the store's remembered credential.

    Implements: AD-082
    """
    return secrets.token_hex(4)


def database_key(key: bytes) -> str:
    """The text DuckDB takes as `ENCRYPTION_KEY`, derived from the store key.

    Implements: AD-086
    """
    _check_key(key)
    return _derive(key, b"duckdb", KEY_BYTES).hex()


# --- store.key ----------------------------------------------------------------


@dataclass(frozen=True)
class _KeyFile:
    store_id: str
    cost: Cost
    salt: bytes
    nonce: bytes
    locked: bytes
    check: bytes


def lock_key(key: bytes, password: str, store_id: str, cost: Cost = DEFAULT_COST) -> bytes:
    """The contents of `store.key`: `key` locked by `password`.

    Every call draws a fresh salt and nonce, so changing the password rewrites
    this file alone (D9).

    Implements: AD-083
    """
    _check_key(key)
    _check_store_id(store_id)
    _check_cost(cost)
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    locked = AESGCM(_password_key(password, salt, cost)).encrypt(nonce, key, _KEY_FILE_AAD)
    document = {
        "format": _KEY_FILE_FORMAT,
        "store": store_id,
        "scrypt": {"n": cost.n, "r": cost.r, "p": cost.p, "salt": _b64(salt)},
        "nonce": _b64(nonce),
        "key": _b64(locked),
        "check": _b64(_check_value(key)),
    }
    return (json.dumps(document, indent=2) + "\n").encode("ascii")


def unlock_key(contents: bytes, password: str) -> bytes:
    """The store's key from `store.key`'s contents.

    Raises `WrongPassword` or `KeyFileUnreadable`.

    Implements: AD-083
    """
    key_file = _parse_key_file(contents)
    try:
        key = AESGCM(_password_key(password, key_file.salt, key_file.cost)).decrypt(
            key_file.nonce, key_file.locked, _KEY_FILE_AAD
        )
    except InvalidTag:
        raise WrongPassword("The password is wrong.") from None
    if not hmac.compare_digest(_check_value(key), key_file.check):
        raise KeyFileUnreadable("store.key is damaged: its key does not match its check value.")
    return key


def store_id_of(contents: bytes) -> str:
    """The store ID recorded in `store.key`, which needs no password.

    Implements: AD-083
    """
    return _parse_key_file(contents).store_id


def key_opens(contents: bytes, key: bytes) -> bool:
    """Whether `key` is the one `store.key` locks — for checking a remembered key.

    Implements: AD-083
    """
    key_file = _parse_key_file(contents)
    if len(key) != KEY_BYTES:
        return False
    return hmac.compare_digest(_check_value(key), key_file.check)


def _parse_key_file(contents: bytes) -> _KeyFile:
    try:
        document = json.loads(contents.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("not a JSON object")
        format_ = document["format"]
        if format_ != _KEY_FILE_FORMAT:
            raise KeyFileUnreadable(
                f"store.key is in format {format_!r}, which this version cannot read."
            )
        scrypt = document["scrypt"]
        cost = Cost(n=_integer(scrypt["n"]), r=_integer(scrypt["r"]), p=_integer(scrypt["p"]))
        _check_cost(cost)
        store_id = document["store"]
        _check_store_id(store_id)
        key_file = _KeyFile(
            store_id=store_id,
            cost=cost,
            salt=_unb64(scrypt["salt"], _SALT_BYTES),
            nonce=_unb64(document["nonce"], _NONCE_BYTES),
            locked=_unb64(document["key"], KEY_BYTES + _TAG_BYTES),
            check=_unb64(document["check"], _CHECK_BYTES),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise KeyFileUnreadable(f"store.key is damaged ({error}).") from error
    return key_file


def _password_key(password: str, salt: bytes, cost: Cost) -> bytes:
    # NFC: the same password typed on another system must give the same bytes.
    secret = unicodedata.normalize("NFC", password).encode("utf-8")
    return Scrypt(salt=salt, length=KEY_BYTES, n=cost.n, r=cost.r, p=cost.p).derive(secret)


def _check_value(key: bytes) -> bytes:
    return _derive(key, b"check", _CHECK_BYTES)


def _check_cost(cost: Cost) -> None:
    n, r, p = cost.n, cost.r, cost.p
    if not (n > 1 and n & (n - 1) == 0 and r >= 1 and p >= 1):
        raise ValueError(f"not a valid scrypt cost: {cost}")
    if 128 * n * r > _MAX_SCRYPT_MEMORY or p > 16:
        raise ValueError(f"scrypt cost too high: {cost}")


def _check_store_id(store_id: object) -> None:
    if not isinstance(store_id, str) or not _STORE_ID.fullmatch(store_id):
        raise ValueError(f"not a store ID: {store_id!r}")


def _check_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError(f"a store key is {KEY_BYTES} bytes")


def _integer(value: object) -> int:
    if type(value) is not int:  # JSON true would pass isinstance(value, int)
        raise ValueError(f"not an integer: {value!r}")
    return value


# --- Sealed files ---------------------------------------------------------------


def seal(key: bytes, data: bytes, purpose: str) -> bytes:
    """`data` compressed, then encrypted and bound to `purpose`.

    Implements: AD-084
    """
    _check_key(key)
    nonce = os.urandom(_NONCE_BYTES)
    header = bytes([_SEAL_FORMAT])
    ciphertext = AESGCM(key).encrypt(nonce, zlib.compress(data), _seal_aad(header, purpose))
    return header + nonce + ciphertext


def unseal(key: bytes, sealed: bytes, purpose: str) -> bytes:
    """The data `seal` sealed for `purpose`; anything else is `Damaged`.

    Implements: AD-084
    """
    _check_key(key)
    if len(sealed) < 1 + _NONCE_BYTES + _TAG_BYTES:
        raise Damaged(f"{purpose} is damaged: too short to be a sealed file.")
    header, nonce, ciphertext = sealed[:1], sealed[1 : 1 + _NONCE_BYTES], sealed[1 + _NONCE_BYTES :]
    if header[0] != _SEAL_FORMAT:
        raise Damaged(f"{purpose} is in format {header[0]}, which this version cannot read.")
    try:
        compressed = AESGCM(key).decrypt(nonce, ciphertext, _seal_aad(header, purpose))
        return zlib.decompress(compressed)
    except (InvalidTag, zlib.error):
        raise Damaged(f"{purpose} is damaged, or is not this store's.") from None


def _seal_aad(header: bytes, purpose: str) -> bytes:
    return header + purpose.encode("utf-8")


# --- The remembered key ---------------------------------------------------------


def remember(store_id: str, key: bytes) -> None:
    """Keep the store's key in the OS credential store, replacing any earlier copy.

    Implements: AD-085, AD-080
    """
    _check_store_id(store_id)
    _check_key(key)
    try:
        backend = _backend()
        # Delete first: on Windows, keyring moves a credential it overwrites to
        # "<user>@<name>" instead of replacing it, which would leave a stale copy.
        _delete(backend, store_id)
        backend.set_password(_credential(store_id), _CREDENTIAL_USER, _b64(key))
    except Exception as error:  # the OS store's own errors, whatever the backend
        raise VaultError(f"The key could not be remembered on this computer: {error}") from error


def remembered(store_id: str) -> bytes | None:
    """The store's remembered key, or None — in which case the password is asked (D9).

    Implements: AD-085, AD-080
    """
    _check_store_id(store_id)
    try:
        text = _backend().get_password(_credential(store_id), _CREDENTIAL_USER)
        return None if text is None else _unb64(text, KEY_BYTES)
    except Exception:  # missing, malformed or unavailable all mean the same here
        return None


def forget(store_id: str) -> None:
    """Remove the store's remembered key; one never remembered is no error.

    Implements: AD-085, AD-080
    """
    _check_store_id(store_id)
    try:
        _delete(_backend(), store_id)
    except Exception as error:
        raise VaultError(f"The remembered key could not be removed: {error}") from error


def _backend() -> KeyringBackend:
    """The OS credential store itself — the one place tests replace.

    Not keyring's own pick: once a second backend is installed, keyring chains
    them, and a chain would bypass the persistence set here.
    """
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        backend = WinVaultKeyring()
        # keyring's default is Enterprise persistence, which roams with a domain
        # profile; "Remember on this computer" means this one (D59).
        backend.persist = "local machine"
        return backend
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()  # the login keychain; keyring never marks an item to sync
    return keyring.get_keyring()  # not a released platform: running from source


def _delete(backend: KeyringBackend, store_id: str) -> None:
    try:
        backend.delete_password(_credential(store_id), _CREDENTIAL_USER)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing to delete


def _credential(store_id: str) -> str:
    return _CREDENTIAL.format(store_id)


# --- Helpers --------------------------------------------------------------------


def _derive(key: bytes, label: bytes, length: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=None, info=b"ibkr2xero " + label
    ).derive(key)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: object, length: int) -> bytes:
    if not isinstance(text, str):
        raise ValueError(f"not base64 text: {text!r}")
    data = base64.b64decode(text, validate=True)
    if len(data) != length:
        raise ValueError(f"expected {length} bytes, found {len(data)}")
    return data
