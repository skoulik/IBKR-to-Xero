"""What a front end does with a ledger: fetch from IBKR into the store, export what
Xero has not had yet, and the answers in between (plan 2026-11-21, step 5).

Operations never ask. Every answer is a parameter and every question a typed
result or error, so the CLI asks on the terminal and the GUI in a dialog, over
the same calls. Each takes its ledger's lock for its whole run (D21), opens the
ledger's databases, brings data.db up to the kept reports and closes both before
it returns.

Implements: AD-120
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .database import Database, ExportFile, Line, Report, read_report
from .flex_client import MAX_DAYS, FlexClient, FlexNotAvailable
from .model import OutputRow, ReconciliationError, StatementError, fmt_number
from .store import Ledger, LedgerSettings, Store
from .writer import write_files, write_report

STARTING, ENDING = "Starting Cash", "Ending Cash"
REPORT_NAME = "ibkr2xero_report.txt"
EXPORTS_FOLDER = "IBKR to Xero"

# Cash joins to a millionth, the reconciliation's tolerance: Flex writes a
# currency holding nothing as +-0.000000002.
_TOLERANCE = Decimal("0.000001")
# The last day IBKR may have closed is found by asking (plan I3): from New
# York's yesterday, a day back on each refusal, for at most a week.
_STEP_BACK = 7
_NEW_YORK = dt.timedelta(hours=5)  # UTC less five hours: never more than an hour out
_DAY = dt.timedelta(days=1)
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --- Errors ---------------------------------------------------------------------------


class OperationError(Exception):
    """An operation refused, and kept or wrote nothing."""


class NotSetUp(OperationError):
    """The account has no Flex token or query ID yet."""


class NothingStored(OperationError):
    """Nothing has been fetched for the account yet."""


class NotAvailable(OperationError):
    """IBKR has no statement for a range that should have one."""


class ReportRejected(OperationError):
    """A fetched report does not read, convert or reconcile, or is not of the range asked."""


class WrongAccount(OperationError):
    """The Flex query reports another IB account than the one stored."""


class DoesNotJoin(OperationError):
    """A report does not join the stored data by cash (D5, D69).

    Implements: AD-122
    """


class PendingFiles(OperationError):
    """Files of an earlier export still await an answer (D67)."""

    def __init__(self, files: list[ExportFile]):
        super().__init__(
            f"{len(files)} exported file(s) still await an answer: imported into Xero or not."
        )
        self.files = files


class CurrenciesUnanswered(OperationError):
    """A currency has no date already in Xero yet (D12)."""

    def __init__(self, currencies: list[str]):
        super().__init__(
            f"The last date already in Xero is needed for {', '.join(currencies)}."
        )
        self.currencies = currencies


class OutsideStoredRange(OperationError):
    """A re-fetch reaches outside the stored data (D66)."""


# --- Results --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    """What a fetch kept: periods in date order, where the stored data ends now, and the
    currencies still to be asked about (D12, D13)."""

    added: list[tuple[dt.date, dt.date]]
    through: dt.date | None
    to_ask: list[str]

    @property
    def up_to_date(self) -> bool:
        return not self.added


@dataclass(frozen=True)
class ExportedFile:
    currency: str
    path: Path
    rows: int
    summary: str


@dataclass(frozen=True)
class Exported:
    """An export's files and report; no folder when nothing was due."""

    folder: Path | None
    files: list[ExportedFile]
    report_path: Path | None
    report: str
    to_check: list[str] = field(default_factory=list)  # lines either side of a Xero date (D7)


@dataclass(frozen=True)
class Change:
    """A line a re-fetch adds, removes or changes; `exported` says whether it went to Xero."""

    kind: str  # "added", "removed", "changed"
    currency: str
    old: Line | None
    new: Line | None
    exported: str | None  # "confirmed", "pending" or None


@dataclass(frozen=True)
class PeriodChanges:
    start: dt.date
    end: dt.date
    changes: list[Change]
    cash: list[str]  # Starting or Ending Cash that moved


@dataclass(frozen=True)
class Refetched:
    """A re-fetch, not kept until `keep` is called with it (D5, D66)."""

    periods: list[PeriodChanges]
    breaks: list[str]  # where it would not join the stored data (D69)
    reports: list[tuple[bytes, Report]] = field(repr=False)

    @property
    def changed(self) -> bool:
        return any(p.changes or p.cash for p in self.periods)


@dataclass(frozen=True)
class Status:
    name: str
    accounts: dict[str, str]  # IB account -> base currency
    set_up: bool  # a Flex token and query ID entered
    first: dt.date | None
    last: dt.date | None
    periods: int
    last_generated: dt.datetime | None
    xero_dates: dict[str, dt.date | None]
    to_ask: list[str]
    pending: list[ExportFile]


# --- The operations -------------------------------------------------------------------


class Operations:
    """The operations on one ledger of an open store.

    `client_factory(token)` makes the Flex client, `now()` returns the time as an aware
    datetime, `progress(text)` hears IBKR's progress while a report is generated, and
    `exports_root` is where export folders go: the testing seams.

    Implements: AD-120
    """

    def __init__(
        self,
        store: Store,
        ledger: Ledger,
        *,
        client_factory: Callable[[str], FlexClient] = FlexClient,
        now: Callable[[], dt.datetime] | None = None,
        progress: Callable[[str], None] | None = None,
        exports_root: Path | None = None,
    ):
        self.store = store
        self.ledger = ledger
        self._client_factory = client_factory
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._progress = progress
        self._exports_root = exports_root

    @property
    def name(self) -> str:
        """The account's display name (D20)."""
        for entry in self.store.ledgers():
            if entry.id == self.ledger.id:
                return entry.name
        return self.ledger.id

    @property
    def exports_root(self) -> Path:
        """Implements: AD-130"""
        return self._exports_root or documents_folder() / EXPORTS_FOLDER

    # --- Fetching -----------------------------------------------------------------

    def fetch(self, start: dt.date | None = None) -> Fetched:
        """Catch up from the end of the stored data to the last day IBKR has closed (D1, D2).

        With nothing stored, the first run: the last 365 days, or from `start` for an
        account younger than that (D13).

        Implements: AD-121, AD-123
        """
        added: list[tuple[dt.date, dt.date]] = []
        with self._open() as (db, settings):
            client = self._client(settings)
            last_closed = self.last_closed()
            periods = db.periods()
            if not periods:
                first = start or last_closed - (MAX_DAYS - 1) * _DAY
                if first > last_closed:
                    raise NotAvailable(f"IBKR has closed no day from {first} yet.")
                got = self._request(client, settings, first, last_closed, step_back=True)
                if got is None:
                    raise NotAvailable(f"IBKR has closed no day from {first} yet.")
                data, report = got
                settings = self._record_account(settings, report)
                self._keep(db, settings, [data])
                added.append(_span(report))
            else:
                if start is not None:
                    raise OperationError(
                        f"A start date is for the first fetch only; the stored data runs "
                        f"from {periods[0].start} to {periods[-1].end}."
                    )
                previous = periods[-1]
                while previous.end < last_closed:
                    begin = previous.end + _DAY
                    end = min(last_closed, begin + (MAX_DAYS - 1) * _DAY)
                    got = self._request(client, settings, begin, end, step_back=end == last_closed)
                    if got is None:
                        break  # nothing closed since the stored end
                    data, report = got
                    self._check_account(report, settings)
                    _check_joins(db.cash(previous.id, ENDING), report.cash(STARTING),
                                 f"{previous.end} and {begin}")
                    self._keep(db, settings, [data])
                    added.append(_span(report))
                    previous = db.periods()[-1]
                    if previous.end < end:
                        break  # stepped back: the days after it are not closed yet
            result = Fetched(added, db.periods()[-1].end, self._to_ask(db, settings))
        self._sync_index(settings)
        return result

    def set_xero_dates(self, dates: Mapping[str, dt.date | None]) -> None:
        """Per currency, the last date already in Xero; None: nothing is (D12).

        Implements: AD-123
        """
        for currency in dates:
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError(f"not a currency code: {currency!r}")
        with self.ledger.locked():
            settings = self.ledger.settings()
            merged = {**settings.currencies, **dates}
            self.ledger.save_settings(dataclasses.replace(settings, currencies=merged))

    def fetch_back(self) -> Fetched:
        """Fetch backwards, 365 days at a time, to the day after the earliest Xero date (D12).

        Implements: AD-123
        """
        added: list[tuple[dt.date, dt.date]] = []
        with self._open() as (db, settings):
            periods = db.periods()
            if not periods:
                raise NothingStored("Nothing has been fetched for this account yet.")
            dates = [d for d in settings.currencies.values() if d is not None]
            if dates:
                target = min(dates) + _DAY
                client = None
                first = periods[0]
                while target < first.start:
                    client = client or self._client(settings)
                    end = first.start - _DAY
                    begin = max(target, end - (MAX_DAYS - 1) * _DAY)
                    data, report = self._request(client, settings, begin, end, step_back=False)
                    self._check_account(report, settings)
                    _check_joins(report.cash(ENDING), db.cash(first.id, STARTING),
                                 f"{end} and {first.start}")
                    self._keep(db, settings, [data])
                    added.insert(0, _span(report))
                    first = db.periods()[0]
            periods = db.periods()
            result = Fetched(added, periods[-1].end, self._to_ask(db, settings))
        return result

    # --- Exporting ----------------------------------------------------------------

    def export(
        self, to: dt.date | None = None, folder: Path | None = None, skip_zero: bool = False
    ) -> Exported:
        """Everything not yet imported into Xero, up to `to` (D10, D67, D68).

        Implements: AD-124, AD-130
        """
        with self._open() as (db, settings):
            periods = db.periods()
            if not periods:
                raise NothingStored("Nothing has been fetched for this account yet.")
            pending = db.pending_files()
            if pending:
                raise PendingFiles(pending)
            to_ask = self._to_ask(db, settings)
            if to_ask:
                raise CurrenciesUnanswered(to_ask)
            stored_end = periods[-1].end
            to = to or stored_end
            if to > stored_end:
                raise OperationError(
                    f"The stored data ends on {stored_end}: fetch first, or export up to then."
                )
            through = {c: d for c, d in settings.currencies.items() if d is not None}
            lines = db.unexported_lines(to, through)
            if not lines:
                return Exported(None, [], None, "Nothing to export: Xero has everything up to "
                                f"{to}.\n")
            to_check = _straddling(db, lines, through) if not db.has_exports() else []
            when = self._local_now()
            target = folder or _new_folder(
                self.exports_root / _safe(self.name), f"{when:%Y-%m-%d} up to {to}"
            )
            heading = [
                f"IBKR to Xero export: {self.name} ({', '.join(settings.accounts) or '?'})",
                f"made {when:%Y-%m-%d %H:%M}: everything not yet imported into Xero, up to {to}",
            ]
            footer = ["Each file is pending until it is confirmed as imported into Xero."]
            exported = self._write(db, target, lines, skip_zero, heading, footer, to_check)
            files = {
                file.currency: (file.path.name, [line.key for line in lines[file.currency]])
                for file in exported.files
            }
            try:
                db.record_export(to, str(target), files, created_at=when.replace(tzinfo=None))
            except BaseException:
                _remove_written(exported)
                raise
            return exported

    def pending(self) -> list[ExportFile]:
        """Implements: AD-125"""
        with self._open() as (db, _):
            return db.pending_files()

    def resolve(self, answers: Mapping[int, bool]) -> None:
        """Per pending file: imported into Xero (confirmed) or not (discarded) (D67).

        Implements: AD-125
        """
        with self._open() as (db, _):
            for file_id, imported in answers.items():
                if imported:
                    db.confirm(file_id)
                else:
                    db.discard(file_id)

    def reexport(
        self, start: dt.date, end: dt.date, folder: Path | None = None, skip_zero: bool = False
    ) -> Exported:
        """Every line dated from `start` to `end`, whatever was exported; not recorded (D10).

        Implements: AD-126
        """
        if end < start:
            raise ValueError(f"the range ends before it starts: {start} to {end}")
        with self._open() as (db, settings):
            if not db.periods():
                raise NothingStored("Nothing has been fetched for this account yet.")
            lines = db.lines_dated(start, end)
            if not lines:
                return Exported(None, [], None, f"No transactions are dated {start} to {end}.\n")
            when = self._local_now()
            target = folder or _new_folder(
                self.exports_root / _safe(self.name), f"{when:%Y-%m-%d} re-export {start} to {end}"
            )
            heading = [
                f"IBKR to Xero re-export: {self.name} ({', '.join(settings.accounts) or '?'})",
                f"made {when:%Y-%m-%d %H:%M}: every transaction dated {start} to {end}",
            ]
            footer = [
                "A re-export is not recorded: these files change nothing in what is counted "
                "as imported."
            ]
            return self._write(db, target, lines, skip_zero, heading, footer, [])

    # --- Re-fetching --------------------------------------------------------------

    def refetch(self, start: dt.date, end: dt.date) -> Refetched:
        """Fetch the stored periods from `start` to `end` again, each with its own dates, and
        say what changed; nothing is kept (D5, D66).

        Implements: AD-127
        """
        if end < start:
            raise ValueError(f"the range ends before it starts: {start} to {end}")
        with self._open() as (db, settings):
            periods = db.periods()
            if not periods:
                raise NothingStored("Nothing has been fetched for this account yet.")
            if start < periods[0].start or end > periods[-1].end:
                raise OutsideStoredRange(
                    f"The stored data runs from {periods[0].start} to {periods[-1].end}; "
                    "a re-fetch stays inside it."
                )
            touched = [p for p in periods if p.start <= end and start <= p.end]
            client = self._client(settings)
            reports: list[tuple[bytes, Report]] = []
            changes: list[PeriodChanges] = []
            for period in touched:
                data, report = self._request(client, settings, period.start, period.end,
                                             step_back=False)
                self._check_account(report, settings)
                reports.append((data, report))
                changes.append(_compare(db, period.id, report))
            breaks = _breaks(db, periods, [r for _, r in reports])
            return Refetched(changes, breaks, reports)

    def keep(self, refetched: Refetched) -> None:
        """Keep a re-fetch: its reports replace the stored ones of their periods (AD-115).

        Implements: AD-127
        """
        with self._open() as (db, settings):
            spans = {(p.start, p.end) for p in db.periods()}
            if any(_span(report) not in spans for _, report in refetched.reports):
                raise OperationError("The stored data changed since the re-fetch: re-fetch again.")
            breaks = _breaks(db, db.periods(), [r for _, r in refetched.reports])
            if breaks:
                raise DoesNotJoin("The re-fetch does not join the stored data: " + "; ".join(breaks))
            self._keep(db, settings, [data for data, _ in refetched.reports])

    # --- Status -------------------------------------------------------------------

    def status(self) -> Status:
        """Implements: AD-128"""
        with self._open() as (db, settings):
            periods = db.periods()
            return Status(
                name=self.name,
                accounts=dict(settings.accounts),
                set_up=bool(settings.flex_token and settings.flex_query_id),
                first=periods[0].start if periods else None,
                last=periods[-1].end if periods else None,
                periods=len(periods),
                last_generated=max((p.generated_at for p in periods), default=None),
                xero_dates=dict(settings.currencies),
                to_ask=self._to_ask(db, settings),
                pending=db.pending_files(),
            )

    def last_closed(self) -> dt.date:
        """The latest day IBKR may have closed: yesterday in New York (I3).

        Implements: AD-121
        """
        return (self._now().astimezone(dt.timezone.utc) - _NEW_YORK).date() - _DAY

    # --- Within an operation ------------------------------------------------------

    @contextlib.contextmanager
    def _open(self) -> Iterator[tuple[Database, LedgerSettings]]:
        """The ledger locked, its databases open and current (D21, D6).

        Implements: AD-132, AD-133
        """
        with self.ledger.locked():
            settings = self.ledger.settings()
            with Database.open(self.ledger.data_db, self.ledger.exports_db,
                               self.ledger.database_key) as db:
                db.ensure_current(self.ledger.originals(), self.ledger.read_original,
                                  settings.gst_rate)
                yield db, settings

    def _client(self, settings: LedgerSettings) -> FlexClient:
        """Implements: AD-134"""
        if not (settings.flex_token and settings.flex_query_id):
            raise NotSetUp("The account has no Flex token and query ID yet.")
        return self._client_factory(settings.flex_token)

    def _request(
        self, client: FlexClient, settings: LedgerSettings, start: dt.date, end: dt.date,
        *, step_back: bool,
    ) -> tuple[bytes, Report] | None:
        """One report from `start`, ending on `end` or, stepping back, on the last day IBKR
        has closed; None when it has closed none since `start` (I3).

        Implements: AD-121, AD-122
        """
        lowest = max(start, end - (_STEP_BACK - 1) * _DAY) if step_back else end
        ask = end
        while True:
            try:
                data = client.fetch(settings.flex_query_id, start, ask, progress=self._progress)
            except FlexNotAvailable:
                if ask > lowest:
                    ask -= _DAY
                    continue
                if step_back and start >= lowest:
                    return None
                raise NotAvailable(
                    f"IBKR has no statement from {start} to {ask}. Before the account was "
                    "funded there is none: if it is younger, start later."
                ) from None
            return data, self._check_report(data, settings, start, ask)

    def _check_report(
        self, data: bytes, settings: LedgerSettings, start: dt.date, end: dt.date
    ) -> Report:
        """Implements: AD-122"""
        try:
            report = read_report(data, settings.gst_rate)
        except (StatementError, ReconciliationError) as error:
            raise ReportRejected(
                f"IBKR's report from {start} to {end} cannot be kept: {error}"
            ) from None
        if _span(report) != (start, end):
            raise ReportRejected(
                f"IBKR's report covers {report.statement.period_start} to "
                f"{report.statement.period_end}, not {start} to {end} as asked."
            )
        return report

    def _check_account(self, report: Report, settings: LedgerSettings) -> None:
        """Implements: AD-122"""
        account = report.statement.account
        if settings.accounts and account not in settings.accounts:
            raise WrongAccount(
                f"The Flex query reports IB account {account}, but this account holds "
                f"{', '.join(settings.accounts)}: check its query ID."
            )

    def _record_account(self, settings: LedgerSettings, report: Report) -> LedgerSettings:
        """The first report says which IB account the ledger is, and its base currency (D20)."""
        self._check_account(report, settings)
        statement = report.statement
        if statement.account in settings.accounts:
            return settings
        accounts = {**settings.accounts, statement.account: statement.base_currency}
        settings = dataclasses.replace(settings, accounts=accounts)
        self.ledger.save_settings(settings)
        return settings

    def _keep(self, db: Database, settings: LedgerSettings, reports: list[bytes]) -> None:
        """Keep reports and build them in; one the build refuses is taken out again."""
        before = set(self.ledger.originals())
        names = [self.ledger.add_original(data) for data in reports]
        try:
            db.ensure_current(self.ledger.originals(), self.ledger.read_original, settings.gst_rate)
        except BaseException:
            for name in names:
                if name not in before:
                    self.ledger.remove_original(name)
            raise

    def _to_ask(self, db: Database, settings: LedgerSettings) -> list[str]:
        return [c for c in db.currencies() if c not in settings.currencies]

    def _sync_index(self, settings: LedgerSettings) -> None:
        """The index lists the IB accounts the first fetch found (D20); after the ledger's
        lock is released, since the store's is always taken first (D21)."""
        for entry in self.store.ledgers():
            if entry.id == self.ledger.id and entry.accounts != tuple(settings.accounts):
                self.store.set_accounts(self.ledger.id, settings.accounts)

    def _local_now(self) -> dt.datetime:
        return self._now().astimezone().replace(microsecond=0)

    def _write(
        self, db: Database, target: Path, lines: dict[str, list[Line]], skip_zero: bool,
        heading: list[str], footer: list[str], to_check: list[str],
    ) -> Exported:
        """The CSVs, named for the account (D68), and the report.

        Implements: AD-130, AD-138
        """
        name = _safe(self.name)
        files: dict[str, list[OutputRow]] = {}
        currencies: dict[str, str] = {}
        summaries: dict[str, str] = {}
        for currency, currency_lines in lines.items():
            rows = [_row(line) for line in currency_lines if not (skip_zero and line.amount == 0)]
            if not rows:
                continue
            filename = f"{name} {currency}.csv"
            files[filename] = rows
            currencies[filename] = currency
            total = sum((row.amount for row in rows), Decimal(0))
            summaries[filename] = (
                f"{len(rows)} transactions, {rows[0].date} to {max(r.date for r in rows)}, "
                f"total {fmt_number(total)}"
                + (f" ({len(currency_lines) - len(rows)} zero-amount skipped)"
                   if len(rows) < len(currency_lines) else "")
            )
        report = list(heading)
        for filename in files:
            report.append(f"  {filename}: {summaries[filename]}")
        notes = db.notes(line.key for currency_lines in lines.values() for line in currency_lines)
        if notes:
            report.append("notes from the periods these come from:")
            report += [f"  {n.currency}, {n.period_start} to {n.period_end}: {n.text}" for n in notes]
        if to_check:
            report.append("check against Xero:")
            report += [f"  {item}" for item in to_check]
        report += footer
        text = "\n".join(report) + "\n"
        paths = write_files(files, target, report_name=REPORT_NAME)
        report_path = write_report(target, REPORT_NAME, text)
        exported = [
            ExportedFile(currencies[path.name], path, len(files[path.name]), summaries[path.name])
            for path in paths
        ]
        return Exported(target, exported, report_path, text, to_check)


# --- Helpers --------------------------------------------------------------------------


def documents_folder() -> Path:
    """The user's Documents folder; on Windows wherever it has been moved (D68).

    Implements: AD-130
    """
    if sys.platform == "win32":
        try:
            return _known_folder("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
        except OSError:
            pass
    return Path.home() / "Documents"


def _known_folder(guid_text: str) -> Path:
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    guid = uuid.UUID(guid_text)
    value = GUID(guid.time_low, guid.time_mid, guid.time_hi_version,
                 (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    path = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(path)) != 0:
        raise OSError("no Documents folder")
    try:
        return Path(path.value)
    finally:
        ole32.CoTaskMemFree(path)


def _safe(name: str) -> str:
    """A name a file name can hold: each character it cannot becomes "-" (D68)."""
    return _UNSAFE.sub("-", name).strip(" .") or "account"


def _new_folder(parent: Path, name: str) -> Path:
    """A folder not there yet: ` 2`, ` 3` added for another the same day (D68)."""
    candidate, number = parent / name, 2
    while candidate.exists():
        candidate, number = parent / f"{name} {number}", number + 1
    return candidate


def _remove_written(exported: Exported) -> None:
    for path in [file.path for file in exported.files] + [exported.report_path]:
        if path is not None:
            path.unlink(missing_ok=True)
    if exported.folder is not None:
        with contextlib.suppress(OSError):
            exported.folder.rmdir()  # only if it is empty: one the export made


def _row(line: Line) -> OutputRow:
    return OutputRow(date=line.date, amount=line.amount, description=line.description,
                     reference=line.reference)


def _span(report: Report) -> tuple[dt.date, dt.date]:
    return report.statement.period_start, report.statement.period_end


def _check_joins(before: Mapping[str, Decimal], after: Mapping[str, Decimal], where: str) -> None:
    """Implements: AD-122"""
    problems = _join_problems(before, after)
    if problems:
        raise DoesNotJoin(
            f"The cash does not join between {where}: {'; '.join(problems)}. Something "
            "already stored has changed at IBKR: re-fetch the stored days before it."
        )


def _join_problems(before: Mapping[str, Decimal], after: Mapping[str, Decimal]) -> list[str]:
    problems = []
    for currency in sorted(set(before) | set(after)):
        ended, starts = before.get(currency, Decimal(0)), after.get(currency, Decimal(0))
        if abs(ended - starts) > _TOLERANCE:
            problems.append(f"{currency} ends at {fmt_number(ended)}, then starts at "
                            f"{fmt_number(starts)}")
    return problems


def _breaks(db: Database, periods: list, reports: list[Report]) -> list[str]:
    """Where re-fetched reports would not join their stored neighbours, or each other (D69).

    Implements: AD-127
    """
    if not reports:
        return []
    breaks: list[str] = []
    first, last = _span(reports[0])[0], _span(reports[-1])[1]
    before = [p for p in periods if p.end == first - _DAY]
    after = [p for p in periods if p.start == last + _DAY]
    if before:
        problems = _join_problems(db.cash(before[0].id, ENDING), reports[0].cash(STARTING))
        if problems:
            breaks.append(f"at {first}: {'; '.join(problems)} — widen the re-fetch to start "
                          f"before {first}")
    for earlier, later in zip(reports, reports[1:]):
        problems = _join_problems(earlier.cash(ENDING), later.cash(STARTING))
        if problems:
            breaks.append(f"IBKR's own reports do not join at {_span(later)[0]}: "
                          f"{'; '.join(problems)}")
    if after:
        problems = _join_problems(reports[-1].cash(ENDING), db.cash(after[0].id, STARTING))
        if problems:
            breaks.append(f"at {last}: {'; '.join(problems)} — widen the re-fetch to end "
                          f"after {last}")
    return breaks


def _compare(db: Database, period_id: int, report: Report) -> PeriodChanges:
    old = {line.key: line for line in db.period_lines(period_id)}
    new = {line.key: line for line, _, _ in report.lines}
    status = db.export_status(old)

    def same(a: Line, b: Line) -> bool:
        return (a.date, a.amount, a.description, a.reference) == (b.date, b.amount, b.description,
                                                                     b.reference)

    changes = [Change("added", line.currency, None, line, None)
               for key, line in new.items() if key not in old]
    for key, line in old.items():
        if key not in new:
            changes.append(Change("removed", line.currency, line, None, status.get(key)))
        elif not same(line, new[key]):
            changes.append(Change("changed", line.currency, line, new[key], status.get(key)))
    cash = []
    for which in (STARTING, ENDING):
        problems = _join_problems(db.cash(period_id, which), report.cash(which))
        cash += [f"{which}: {p.replace('ends at', 'was').replace('then starts at', 'now')}"
                 for p in problems]
    start, end = _span(report)
    return PeriodChanges(start, end, changes, cash)


def _straddling(db: Database, lines: dict[str, list[Line]], through: Mapping[str, dt.date]) -> list[str]:
    """The first export's lines dated one side of a currency's Xero date and reported on the
    other: included though maybe in Xero already, or left out though maybe not (D7).

    Implements: AD-124
    """
    found = []
    for currency, currency_lines in lines.items():
        mark = through.get(currency)
        if mark is None:
            continue
        for line in currency_lines:
            if line.date <= mark < line.report_date:
                found.append(f"{currency} {line.date} {fmt_number(line.amount)} {line.description}: "
                             f"dated by {mark} but reported {line.report_date}, so included "
                             "here; check Xero does not have it already")
    far = dt.date(9999, 12, 31)
    for currency, mark in through.items():
        for line in db.lines_dated(mark + _DAY, far).get(currency, []):
            if line.report_date <= mark:
                found.append(f"{currency} {line.date} {fmt_number(line.amount)} {line.description}: "
                             f"reported {line.report_date}, by {mark}, so left out as already "
                             "in Xero; check Xero has it")
    return found
