"""Test fixtures.

The examples/ folder holds a real IB statement and hand-made reference
outputs. It is gitignored (real account data), so tests that need it skip
when it is absent instead of failing. The statement file is discovered by
shape: reference outputs are named {CCY}.csv, the statement is the rest.
"""

import re
from pathlib import Path

import pytest

from ib_connector import parse_statement

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _find_statement_csv() -> Path | None:
    if not EXAMPLES.is_dir():
        return None
    for path in sorted(EXAMPLES.glob("*.csv")):
        if not re.fullmatch(r"[A-Z]{3}", path.stem):
            return path
    return None


STATEMENT_CSV = _find_statement_csv()


@pytest.fixture(scope="session")
def statement_csv() -> Path:
    if STATEMENT_CSV is None:
        pytest.skip("no example statement in examples/ (gitignored, not distributed)")
    return STATEMENT_CSV


@pytest.fixture(scope="session")
def statement(statement_csv):
    return parse_statement(statement_csv)
