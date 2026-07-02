"""Test fixtures.

The examples/ folder holds a real IB statement and hand-made reference
outputs. It is gitignored (real account data), so tests that need it skip
when it is absent instead of failing. The statement file is discovered by
shape: reference outputs are named {CCY}.csv, the statement is the rest.
"""

import re
from pathlib import Path

import pytest

from ibkr_to_xero import parse_statement

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


@pytest.fixture(scope="session")
def fy_statement():
    """The full-financial-year statement (forex/futures/bonds/corp actions)."""
    if EXAMPLES.is_dir():
        for path in sorted(EXAMPLES.glob("*.csv")):
            if re.fullmatch(r"[A-Z]{3}", path.stem):
                continue
            parsed = parse_statement(path)
            if (parsed.period_end - parsed.period_start).days > 300:
                return parsed
    pytest.skip("no full-year example statement in examples/ (gitignored)")
