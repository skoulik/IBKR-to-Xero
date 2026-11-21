"""The CLI's commands over the store: ibkr2xero fetch | export | confirm | refetch |
status | account | password | start-over (plan 2026-11-21, D15).

`cli.main` hands over when the first word names one of them; anything else is a
statement file, converted as before. The questions an operation needs answered are
asked here, on the terminal: operations never ask.

Implements: AD-135, AD-129
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from . import vault
from .database import DatabaseError, ExportFile
from .flex_client import FlexClient, FlexError
from .model import ReconciliationError, StatementError, fmt_number
from .operations import Exported, Fetched, OperationError, Operations, Refetched
from .store import (
    CannotRemember,
    Ledger,
    NoStore,
    NoSuchLedger,
    PasswordNeeded,
    Store,
    StoreError,
    WrongPassword,
    default_folder,
)

# The scrypt cost of a new password; tests make it cheap.
STORE_COST = vault.DEFAULT_COST

_REMEMBER_WARNING = (
    "The password is still needed on another computer, or after a Windows password reset "
    "(on a Mac, a reset login keychain): keep it safe; a password manager is ideal."
)


class NeedsTerminal(Exception):
    """A question has to be asked, and there is no terminal to ask it on."""


class Terminal:
    """Where questions are asked and answers read; tests replace it.

    Implements: AD-137
    """

    @property
    def interactive(self) -> bool:
        return sys.stdin is not None and sys.stdin.isatty()

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt)

    def out(self, text: str = "") -> None:
        print(text)

    def err(self, text: str) -> None:
        print(text, file=sys.stderr)


class _Run:
    """One command's surroundings: the terminal and the seams handed to Operations."""

    def __init__(self, args, terminal: Terminal, client_factory, now, exports_root):
        self.args = args
        self.term = terminal
        self.client_factory = client_factory
        self.now = now
        self.exports_root = exports_root

    @property
    def folder(self) -> Path:
        return self.args.store or default_folder()

    def need_terminal(self, what: str) -> None:
        if not self.term.interactive:
            raise NeedsTerminal(f"{what}, and there is no terminal to ask on.")

    def open_store(self) -> Store:
        """The store, with the remembered key or else the password asked for (D9)."""
        try:
            return Store.open(self.folder)
        except NoStore:
            raise NoStore(
                f"There is no store in {self.folder} yet: `ibkr2xero account add NAME` makes one."
            ) from None
        except PasswordNeeded:
            self.need_terminal("The store's password is needed")
        for _ in range(3):
            password = self.term.secret("Password for the store: ")
            try:
                return Store.open(self.folder, password)
            except WrongPassword:
                self.term.err("The password is wrong.")
        raise WrongPassword("The password is wrong.")

    def ledger(self, store: Store) -> Ledger:
        if self.args.account:
            return store.find_ledger(self.args.account)
        entries = store.ledgers()
        if len(entries) == 1:
            return store.ledger(entries[0].id)
        if not entries:
            raise NoSuchLedger("The store has no accounts yet: `ibkr2xero account add NAME`.")
        names = ", ".join(entry.name for entry in entries)
        raise NoSuchLedger(f"The store has several accounts: name one with -a ({names}).")

    def operations(self, store: Store | None = None) -> Operations:
        """Implements: AD-131"""
        store = store or self.open_store()
        return Operations(
            store, self.ledger(store),
            client_factory=self.client_factory,
            now=self.now,
            progress=lambda text: self.term.err(f"  IBKR: {text}"),
            exports_root=self.exports_root,
        )

    def yes(self, prompt: str) -> bool:
        while True:
            answer = self.term.ask(f"{prompt} [y/n] ").strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def new_password(self) -> str:
        for _ in range(3):
            first = self.term.secret("New password: ")
            if not first:
                self.term.err("A password cannot be empty.")
                continue
            if self.term.secret("The same again: ") == first:
                return first
            self.term.err("The two differ.")
        raise OperationError("No password was set.")


def main(
    argv: list[str],
    *,
    terminal: Terminal | None = None,
    client_factory: Callable[[str], FlexClient] = FlexClient,
    now: Callable[[], dt.datetime] | None = None,
    exports_root: Path | None = None,
) -> int:
    """Implements: AD-135, AD-136"""
    args = _parser().parse_args(argv)
    run = _Run(args, terminal or Terminal(), client_factory, now, exports_root)
    try:
        return args.handler(run)
    except KeyboardInterrupt:
        run.term.err("Interrupted.")
        return 130
    except (NeedsTerminal, OperationError, StoreError, DatabaseError, FlexError, StatementError,
            ReconciliationError, FileExistsError, ValueError) as error:
        run.term.err(f"error: {error}")
        if args.nothing:
            run.term.err(args.nothing)
        return 2


# --- The commands ---------------------------------------------------------------------


def _fetch(run: _Run) -> int:
    """Implements: AD-121, AD-123"""
    ops = run.operations()
    result = ops.fetch(start=run.args.start)
    _print_fetched(run, result)
    if not result.added:
        run.term.out(f"Up to date: IBKR has closed no day after {result.through} yet.")
    _answer_currencies(run, ops, result.to_ask)
    _mention_pending(run, ops)
    return 0


def _export(run: _Run) -> int:
    """Implements: AD-124, AD-126"""
    ops = run.operations()
    args = run.args
    if args.again:
        if not (args.start and args.to):
            raise ValueError("a re-export needs --from and --to")
        _show_export(run, ops.reexport(args.start, args.to, args.output_dir, args.skip_zero))
        return 0
    if args.start:
        raise ValueError("--from is for a re-export, with --again")
    pending = ops.pending()
    if pending:
        _ask_pending(run, ops, pending, "Before exporting: ")
    _answer_currencies(run, ops, ops.status().to_ask)
    exported = ops.export(args.to, args.output_dir, args.skip_zero)
    _show_export(run, exported)
    if exported.files:
        run.term.out("Once imported into Xero, say so with `ibkr2xero confirm`; the next export "
                     "asks too.")
    return 0


def _confirm(run: _Run) -> int:
    """Implements: AD-125"""
    ops = run.operations()
    pending = ops.pending()
    if not pending:
        run.term.out("No exported file awaits an answer.")
        return 0
    _ask_pending(run, ops, pending, "")
    return 0


def _refetch(run: _Run) -> int:
    """Implements: AD-127"""
    ops = run.operations()
    refetched = ops.refetch(run.args.start, run.args.to)
    _show_refetch(run, refetched)
    if not refetched.changed:
        run.term.out("Nothing has changed at IBKR; nothing kept.")
        return 0
    if refetched.breaks:
        run.term.err("The re-fetch does not join the stored data, so it cannot be kept:")
        for item in refetched.breaks:
            run.term.err(f"  {item}")
        return 2
    run.need_terminal("Whether to keep the re-fetch has to be asked")
    if not run.yes("Keep the re-fetched reports in place of the stored ones?"):
        run.term.out("Not kept.")
        return 0
    ops.keep(refetched)
    run.term.out("Kept.")
    exported = [c for p in refetched.periods for c in p.changes if c.exported]
    if exported:
        run.term.out("Already exported, so correct these in Xero by hand:")
        for change in exported:
            run.term.out(f"  {_change_text(change)}")
    return 0


def _status(run: _Run) -> int:
    """Implements: AD-128"""
    status = run.operations().status()
    accounts = ", ".join(f"{a} (base {b})" for a, b in status.accounts.items()) or "none yet"
    run.term.out(f"{status.name}: IB account {accounts}")
    if not status.set_up:
        run.term.out("  no Flex token and query ID yet: `ibkr2xero account token` and `query`")
    if status.first:
        run.term.out(f"  fetched {status.first} to {status.last}, {status.periods} period(s), "
                     f"last generated by IBKR {status.last_generated:%Y-%m-%d %H:%M}")
    else:
        run.term.out("  nothing fetched yet: `ibkr2xero fetch`")
    for currency, date in sorted(status.xero_dates.items()):
        run.term.out(f"  {currency}: in Xero up to {date}" if date else
                     f"  {currency}: nothing in Xero before the stored data")
    for currency in status.to_ask:
        run.term.out(f"  {currency}: the last date in Xero not given yet")
    for file in status.pending:
        run.term.out(f"  awaiting an answer: {_file_path(file)}")
    return 0


def _account(run: _Run) -> int:
    """Implements: AD-136, AD-137"""
    args = run.args
    if args.action == "add":
        if not args.name:
            raise ValueError("`account add` needs the account's name")
        try:
            store = run.open_store()
        except NoStore:
            store = _create_store(run)
        run.need_terminal("The Flex token has to be asked")
        query = args.value or run.term.ask("Flex query ID: ").strip()
        token = run.term.secret("Flex token (not shown): ").strip()
        ledger = store.add_ledger(args.name)
        _save(ledger, flex_query_id=query, flex_token=token)
        run.term.out(f"Account {args.name!r} added. Fetch its reports with `ibkr2xero fetch`"
                     + (f" -a {args.name!r}." if len(store.ledgers()) > 1 else "."))
        return 0
    store = run.open_store()
    if args.action == "list":
        entries = store.ledgers()
        if not entries:
            run.term.out("No accounts yet.")
        for entry in entries:
            run.term.out(f"{entry.name}: {', '.join(entry.accounts) or 'not fetched yet'}")
        return 0
    if not args.name:
        raise ValueError(f"`account {args.action}` needs the account's name")
    ledger = store.find_ledger(args.name)
    if args.action == "rename":
        if not args.value:
            raise ValueError("`account rename` needs the new name")
        store.rename_ledger(ledger.id, args.value)
        run.term.out(f"Renamed to {args.value!r}.")
    elif args.action == "remove":
        run.need_terminal("Removing an account has to be confirmed")
        run.term.out("This deletes the account's fetched reports and its record of what was "
                     "exported to Xero. Adding it back starts again from the first fetch.")
        typed = run.term.ask("Type the account's name to confirm: ").strip()
        if typed.casefold() != args.name.strip().casefold():
            run.term.out("Not removed.")
            return 0
        store.remove_ledger(ledger.id)
        run.term.out("Removed.")
    elif args.action == "token":
        run.need_terminal("The Flex token has to be asked")
        token = run.term.secret("New Flex token (not shown): ").strip()
        _save(ledger, flex_token=token)
        run.term.out("Token saved.")
    elif args.action == "query":
        if not args.value:
            raise ValueError("`account query` needs the query ID")
        _save(ledger, flex_query_id=args.value.strip())
        run.term.out("Query ID saved.")
    return 0


def _password(run: _Run) -> int:
    """Implements: AD-136"""
    store = run.open_store()
    if run.args.remember:
        store.set_remembered(True)
        run.term.out("This computer remembers the store's key now. " + _REMEMBER_WARNING)
    elif run.args.forget:
        store.set_remembered(False)
        run.term.out("This computer no longer remembers the store's key.")
    else:
        run.need_terminal("The new password has to be asked")
        store.change_password(run.new_password(), cost=STORE_COST)
        run.term.out("Password changed.")
    return 0


def _start_over(run: _Run) -> int:
    """Implements: AD-136"""
    run.need_terminal("Starting over has to be confirmed")
    run.term.out(f"This sets the store in {run.folder} aside, renamed with "
                 f"'(password forgotten {dt.date.today()})', and makes a new, empty one. "
                 "Nothing is deleted: the old password still opens the old store.")
    if not run.yes("Start over?"):
        run.term.out("Nothing changed.")
        return 0
    store = Store.start_over(run.folder, run.new_password(), cost=STORE_COST)
    _offer_remember(run, store)
    run.term.out("A new store is ready: `ibkr2xero account add NAME`.")
    return 0


# --- Questions --------------------------------------------------------------------------


def _create_store(run: _Run) -> Store:
    """The first `account add` makes the store (D9).

    Implements: AD-137
    """
    run.need_terminal("A new store needs a password")
    run.term.out(f"Making a store for the fetched reports in {run.folder}.")
    store = Store.create(run.folder, run.new_password(), cost=STORE_COST)
    _offer_remember(run, store)
    return store


def _offer_remember(run: _Run, store: Store) -> None:
    run.term.out(_REMEMBER_WARNING)
    if run.yes("Remember the password on this computer, so it is not asked again?"):
        try:
            store.set_remembered(True)
        except CannotRemember as error:
            run.term.err(f"Not remembered: {error}")


def _answer_currencies(run: _Run, ops: Operations, to_ask: list[str]) -> None:
    """Per new currency, the last date already in Xero; then fetch back to it (D12, D13).

    Implements: AD-137, AD-123
    """
    while to_ask:
        if not run.term.interactive:
            run.term.out(f"Not asked, with no terminal: the last date already in Xero for "
                         f"{', '.join(to_ask)}. Run `ibkr2xero fetch` from a terminal to answer.")
            return
        run.term.out(f"For {', '.join(to_ask)}: the last date already in Xero (YYYY-MM-DD), or "
                     "'none' if Xero has nothing of it yet.")
        same = _ask_date(run, "One date for all? Type it, or press Enter to answer one by one: ",
                         empty_ok=True)
        dates = {}
        for currency in to_ask:
            dates[currency] = same if same else _ask_xero_date(run, currency)
        ops.set_xero_dates(dates)
        back = ops.fetch_back()
        _print_fetched(run, back)
        to_ask = back.to_ask


def _ask_xero_date(run: _Run, currency: str) -> dt.date | None:
    while True:
        text = run.term.ask(f"{currency}: last date already in Xero: ").strip()
        if text.lower() == "none":
            start = _ask_date(run, f"{currency}: export it from which date? (Enter: everything "
                              "fetched) ", empty_ok=True)
            return start - dt.timedelta(days=1) if start else None
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            run.term.err("A date is YYYY-MM-DD, or 'none'.")


def _ask_date(run: _Run, prompt: str, *, empty_ok: bool) -> dt.date | None:
    while True:
        text = run.term.ask(prompt).strip()
        if not text and empty_ok:
            return None
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            run.term.err("A date is YYYY-MM-DD.")


def _ask_pending(run: _Run, ops: Operations, pending: list[ExportFile], lead: str) -> None:
    """Implements: AD-125, AD-137"""
    if not run.term.interactive:
        raise NeedsTerminal(
            f"{len(pending)} exported file(s) await an answer: run `ibkr2xero confirm` from a "
            "terminal"
        )
    run.term.out(f"{lead}{len(pending)} exported file(s) await an answer. Yes counts the file "
                 "as imported, never to be exported again; no lets the next export include its "
                 "transactions instead, and the file can no longer be confirmed.")
    answers = {}
    for file in pending:
        answers[file.id] = run.yes(f"{_file_path(file)}, exported {file.created_at:%Y-%m-%d}: "
                                   "imported into Xero?")
    ops.resolve(answers)


def _mention_pending(run: _Run, ops: Operations) -> None:
    pending = ops.pending()
    if pending:
        run.term.out(f"{len(pending)} exported file(s) await an answer: `ibkr2xero confirm`.")


def _save(ledger: Ledger, **changes) -> None:
    import dataclasses

    with ledger.locked():
        ledger.save_settings(dataclasses.replace(ledger.settings(), **changes))


# --- Output ---------------------------------------------------------------------------


def _print_fetched(run: _Run, result: Fetched) -> None:
    for start, end in result.added:
        run.term.out(f"  fetched {start} to {end}")


def _show_export(run: _Run, exported: Exported) -> None:
    if not exported.files:
        run.term.out(exported.report.strip())
        return
    for file in exported.files:
        run.term.out(f"  {file.path}: {file.summary}")
    for item in exported.to_check:
        run.term.out(f"  check against Xero: {item}")
    if exported.report_path:
        run.term.out(f"  report saved to {exported.report_path}")


def _show_refetch(run: _Run, refetched: Refetched) -> None:
    for period in refetched.periods:
        if not (period.changes or period.cash):
            run.term.out(f"  {period.start} to {period.end}: unchanged")
            continue
        run.term.out(f"  {period.start} to {period.end}:")
        for change in period.changes:
            run.term.out(f"    {_change_text(change)}")
        for item in period.cash:
            run.term.out(f"    {item}")


def _change_text(change) -> str:
    def text(line):
        return f"{line.date} {fmt_number(line.amount)} {line.description}"

    if change.kind == "added":
        body = f"added   {change.currency} {text(change.new)}"
    elif change.kind == "removed":
        body = f"removed {change.currency} {text(change.old)}"
    else:
        body = f"changed {change.currency} {text(change.old)} -> {text(change.new)}"
    if change.exported == "confirmed":
        body += " (imported into Xero)"
    elif change.exported == "pending":
        body += " (in a file awaiting an answer)"
    return body


def _file_path(file: ExportFile) -> str:
    return str(Path(file.folder) / file.filename)


# --- Arguments ------------------------------------------------------------------------


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from None


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", type=Path, default=None, metavar="DIR",
                        help="the store's folder (default: the one for this user)")
    picks = argparse.ArgumentParser(add_help=False)
    picks.add_argument("-a", "--account", default=None, metavar="NAME",
                       help="the account to work on; may be left out when there is only one")

    parser = argparse.ArgumentParser(
        prog="ibkr2xero",
        description="Work on the store of reports fetched from IBKR's Flex Web Service. "
        "`ibkr2xero FILE` converts one statement file, as before.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, handler, help_text, nothing, parents=(common, picks)):
        p = sub.add_parser(name, parents=list(parents), help=help_text, description=help_text)
        p.set_defaults(handler=handler, nothing=nothing)
        return p

    p = command("fetch", _fetch, "fetch what IBKR has closed since the stored data ends",
                "Nothing more was kept.")
    p.add_argument("--from", dest="start", type=_date, metavar="DATE",
                   help="first fetch only: start here, for an account younger than a year")

    p = command("export", _export, "export everything not yet imported into Xero",
                "No output files were written.")
    p.add_argument("--to", type=_date, metavar="DATE",
                   help="up to this date (default: the end of the stored data)")
    p.add_argument("-o", "--output-dir", type=Path, default=None, metavar="DIR",
                   help="write the files here (default: a new folder under Documents)")
    p.add_argument("-s", "--skip-zero-transactions", dest="skip_zero", action="store_true",
                   help="leave zero-amount transactions out of the files")
    p.add_argument("--again", action="store_true",
                   help="re-export every transaction dated --from to --to, whatever was "
                   "exported; not recorded")
    p.add_argument("--from", dest="start", type=_date, metavar="DATE",
                   help="with --again: the first date")

    command("confirm", _confirm, "say which exported files were imported into Xero", "")

    p = command("refetch", _refetch, "fetch stored days again and show what changed at IBKR",
                "Nothing was kept.")
    p.add_argument("--from", dest="start", type=_date, metavar="DATE", required=True)
    p.add_argument("--to", type=_date, metavar="DATE", required=True)

    command("status", _status, "what is stored for an account, and what awaits an answer", "")

    p = command("account", _account, "add, list, rename or remove accounts; replace a token "
                "or query ID", "", parents=(common,))
    p.add_argument("action", choices=("add", "list", "rename", "remove", "token", "query"))
    p.add_argument("name", nargs="?", help="the account's name")
    p.add_argument("value", nargs="?",
                   help="rename: the new name; query and add: the Flex query ID")
    p.set_defaults(account=None)

    p = command("password", _password, "change the store's password, or what this computer "
                "remembers", "", parents=(common,))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--remember", action="store_true",
                       help="remember the store's key on this computer")
    group.add_argument("--forget", action="store_true",
                       help="forget the store's key on this computer")

    command("start-over", _start_over, "set the store aside and make a new one, for a "
            "forgotten password", "", parents=(common,))
    return parser
