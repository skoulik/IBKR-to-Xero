"""Check an installed DuckDB built with httpfs linked in, as the store needs it.

    python packaging/duckdb/check_wheel.py VERSION   run the checks; exit 0 only if all pass
    python packaging/duckdb/check_wheel.py --where   print the compiled module's path

The checks: the version is VERSION exactly (not a dev build); httpfs is statically
linked; with downloads and autoloading off and an empty extension directory, an
encrypted database is written with no LOAD, its contents are not readable in the
file, it reads back exactly — a DECIMAL(38,11) amount included — and a wrong key is
refused. They also run inside a frozen app, where `sys.frozen` is set.

Implements: AD-079
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from decimal import Decimal

import duckdb

KEY = "0123456789abcdef0123456789abcdef"
MARKER = "a-distinctive-marker"
AMOUNT = Decimal("-1234.56789012345")  # eleven places, the store's scale (D18)


def main(argv: list[str]) -> int:
    if argv == ["--where"]:
        print(importlib.util.find_spec("_duckdb").origin)
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    expected = argv[0].removeprefix("v")
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {label}{': ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    print(f"duckdb {duckdb.__version__}, frozen {getattr(sys, 'frozen', False)}")
    check("version", duckdb.__version__ == expected, f"{duckdb.__version__}, expected {expected}")

    work = tempfile.mkdtemp()
    config = {
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "extension_directory": os.path.join(work, "none"),
    }
    con = duckdb.connect(config=config)
    mode = con.execute(
        "select install_mode from duckdb_extensions() where extension_name = 'httpfs'"
    ).fetchone()
    check("httpfs statically linked", mode is not None and "STATIC" in str(mode[0]).upper(), str(mode))

    path = os.path.join(work, "enc.db")
    try:
        con.execute(f"ATTACH '{path}' AS enc (ENCRYPTION_KEY '{KEY}')")
        con.execute("CREATE TABLE enc.x(a VARCHAR, d DECIMAL(38,11))")
        con.execute("INSERT INTO enc.x VALUES (?, ?)", [MARKER, AMOUNT])
        con.execute("DETACH enc")
        check("encrypted write, offline, no LOAD", True)
    except duckdb.Error as exc:
        check("encrypted write, offline, no LOAD", False, str(exc).splitlines()[0])
        return 1
    with open(path, "rb") as fh:
        check("contents not readable in the file", MARKER.encode() not in fh.read())

    reader = duckdb.connect(config=config)
    reader.execute(f"ATTACH '{path}' AS enc (ENCRYPTION_KEY '{KEY}', READ_ONLY)")
    got = reader.execute("select a, d from enc.x").fetchall()
    check("read back exactly", got == [(MARKER, AMOUNT)], str(got))
    reader.execute("DETACH enc")
    reader.close()

    try:
        duckdb.connect(config=config).execute(
            f"ATTACH '{path}' AS enc (ENCRYPTION_KEY 'wrong-key-wrong-key-wrong-key-00')"
        )
        check("wrong key refused", False, "opened with the wrong key")
    except duckdb.Error as exc:
        message = str(exc).splitlines()[0]
        check("wrong key refused", "key" in message.lower(), message)

    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
