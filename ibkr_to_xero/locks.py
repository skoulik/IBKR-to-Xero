"""Exclusive locks on files, refused at once rather than waited for.

A run works on a ledger under its lock, and changes to the store's index or
password under the store-wide one (plan D21); which files those are is
store.py's business. The OS releases a lock when its process dies, so a crash
leaves nothing stale. Nothing is written into a lock file (D60), so a refusal
says only that the lock is busy.

Implements: AD-088
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path

# What the OS says when someone else holds the lock: Windows EACCES (or
# EDEADLOCK), macOS EWOULDBLOCK. Anything else is a real failure, not "busy".
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EACCES)}


class Busy(Exception):
    """The lock is held by another IBKR to Xero window or command."""

    def __init__(self, path: Path):
        super().__init__(f"{path} is locked by another IBKR to Xero window or command.")
        self.path = path


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # one attempt; LK_LOCK would retry for 10 s

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    # flock, not lockf: lockf's locks belong to the whole process, so a second
    # open in this process would not be refused, and closing any one handle to
    # the file would drop them.
    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: str | os.PathLike[str]) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the block; `Busy` if it is held elsewhere.

    Implements: AD-089, AD-090
    """
    path = Path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # not inherited by children (PEP 446)
    try:
        try:
            _lock(fd)
        except OSError as error:
            if error.errno in _BUSY:
                raise Busy(path) from None
            raise
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
