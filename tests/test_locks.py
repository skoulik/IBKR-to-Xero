"""locks.py: exclusive, refused at once, released when the block ends or the process dies.

Implements: AD-089, AD-090
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ibkr_to_xero.locks import Busy, locked

ROOT = Path(__file__).resolve().parents[1]

_HOLDER = """
import sys, time
from ibkr_to_xero.locks import locked
with locked(sys.argv[1]):
    print("held", flush=True)
    time.sleep(60)
"""


def _hold_in_another_process(path):
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert child.stdout.readline().strip() == "held"
    return child


def test_a_lock_is_exclusive_within_one_process(tmp_path):
    path = tmp_path / "ledger.lock"
    with locked(path):
        with pytest.raises(Busy) as refused:
            with locked(path):
                pass  # pragma: no cover
        assert refused.value.path == path
    with locked(path):
        pass  # free again once the block ended


def test_the_file_is_created_kept_and_left_empty(tmp_path):
    path = tmp_path / "store.lock"
    with locked(path):
        assert path.exists()
    assert path.exists() and path.stat().st_size == 0


def test_a_failing_block_still_releases(tmp_path):
    path = tmp_path / "ledger.lock"
    with pytest.raises(RuntimeError):
        with locked(path):
            raise RuntimeError("the operation failed")
    with locked(path):
        pass


def test_locks_on_different_files_are_independent(tmp_path):
    with locked(tmp_path / "store.lock"), locked(tmp_path / "ledger.lock"):
        pass


def test_another_process_holding_it_refuses_at_once(tmp_path):
    path = tmp_path / "ledger.lock"
    child = _hold_in_another_process(path)
    try:
        started = time.monotonic()
        with pytest.raises(Busy, match="another IBKR to Xero window or command"):
            with locked(path):
                pass  # pragma: no cover
        assert time.monotonic() - started < 1, "waited instead of refusing"
    finally:
        child.kill()
        child.wait()


def test_a_killed_holder_leaves_no_stale_lock(tmp_path):
    path = tmp_path / "ledger.lock"
    child = _hold_in_another_process(path)
    child.kill()
    child.wait()
    # The OS releases the lock with the process; allow it a moment.
    deadline = time.monotonic() + 5
    while True:
        try:
            with locked(path):
                return
        except Busy:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_a_missing_folder_is_an_error_not_busy(tmp_path):
    with pytest.raises(FileNotFoundError):
        with locked(tmp_path / "missing" / "ledger.lock"):
            pass  # pragma: no cover
