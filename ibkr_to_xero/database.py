"""A ledger's databases: data.db, built from its kept reports, and exports.db, the export record.

data.db is derived and disposable (plan D6): it records the parser version,
schema and GST rate it was built with, and when any of them moves it is built
again from the reports the store keeps. exports.db records what has been
exported (D10) and is never rebuilt, so it refers to lines by keys that stay
the same across rebuilds (D18). Both are DuckDB files that DuckDB encrypts
(D8, D22). Amounts are DECIMAL(38,11) (D18); DuckDB would round a longer value
silently, so such a value rejects its report instead.

This is the only module that knows DuckDB. It knows nothing of folders or
passwords: the store hands it two paths and a key, and the caller holds the
ledger's lock while it is open (D21).

Implements: AD-107
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Context, Decimal
from pathlib import Path

import duckdb

from .convert import convert
from .flex import PARSER_VERSION, read_flex
from .model import OutputRow, ReconciliationError, Statement, StatementError
from .reconcile import reconcile

SCHEMA_VERSION = 2  # 2: the reconciliation's notes
EXPORTS_SCHEMA_VERSION = 1

_SCALE = 11  # DECIMAL(38,11): Flex's at most 9 places, and two guard places (D18)
_TOO_LARGE = Decimal(10) ** 27  # 38 digits less the 11 after the point
_STEP = Decimal(1).scaleb(-_SCALE)
_WIDE = Context(prec=80)  # room to quantize any value below _TOO_LARGE exactly
_CHUNK = 500  # rows per multi-row INSERT; executemany is far slower in DuckDB
_SYNTHETIC = ("MTM", "GST", "ROUNDING")
_LINE_COLUMNS = "l.key, l.currency, l.date, l.report_date, l.amount, l.description, l.reference"

# Only the httpfs linked into our build, never one downloaded or found on disk (D22).
_CONFIG = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "extension_directory": str(Path(tempfile.gettempdir()) / "ibkr2xero-no-extensions"),
}

_DATA_SCHEMA = """
CREATE TABLE {db}.meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
CREATE TABLE {db}.ib_account (id VARCHAR PRIMARY KEY, base_currency VARCHAR NOT NULL);
CREATE TABLE {db}.period (
    id INTEGER PRIMARY KEY, account VARCHAR NOT NULL, from_date DATE NOT NULL,
    to_date DATE NOT NULL, original VARCHAR NOT NULL UNIQUE, generated_at TIMESTAMP NOT NULL,
    superseded_by INTEGER);
CREATE TABLE {db}.cash_report (
    period INTEGER NOT NULL, account VARCHAR NOT NULL, currency VARCHAR NOT NULL,
    component VARCHAR NOT NULL, amount DECIMAL(38,11) NOT NULL);
CREATE TABLE {db}.trade (
    id VARCHAR NOT NULL, period INTEGER NOT NULL, account VARCHAR NOT NULL, report_date DATE,
    datetime TIMESTAMP NOT NULL, currency VARCHAR NOT NULL, asset_class VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL, quantity DECIMAL(38,11) NOT NULL, price DECIMAL(38,11),
    proceeds DECIMAL(38,11), commission DECIMAL(38,11) NOT NULL,
    commission_currency VARCHAR NOT NULL, codes VARCHAR NOT NULL);
CREATE TABLE {db}.cash_txn (
    id VARCHAR NOT NULL, period INTEGER NOT NULL, account VARCHAR NOT NULL, report_date DATE,
    date DATE NOT NULL, currency VARCHAR NOT NULL, kind VARCHAR NOT NULL,
    description VARCHAR NOT NULL, amount DECIMAL(38,11) NOT NULL);
CREATE TABLE {db}.corporate_action (
    id VARCHAR NOT NULL, period INTEGER NOT NULL, account VARCHAR NOT NULL, report_date DATE,
    datetime TIMESTAMP NOT NULL, currency VARCHAR NOT NULL, description VARCHAR NOT NULL,
    proceeds DECIMAL(38,11) NOT NULL);
CREATE TABLE {db}.tax_item (
    id VARCHAR NOT NULL, period INTEGER NOT NULL, account VARCHAR NOT NULL, report_date DATE,
    datetime TIMESTAMP NOT NULL, currency VARCHAR NOT NULL, kind VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL, amount DECIMAL(38,11) NOT NULL, taxed_id VARCHAR);
CREATE TABLE {db}.line (
    key VARCHAR PRIMARY KEY, period INTEGER NOT NULL, account VARCHAR NOT NULL,
    currency VARCHAR NOT NULL, position INTEGER NOT NULL, date DATE NOT NULL,
    report_date DATE NOT NULL, amount DECIMAL(38,11) NOT NULL, description VARCHAR NOT NULL,
    reference VARCHAR NOT NULL, source_id VARCHAR);
CREATE TABLE {db}.note (
    period INTEGER NOT NULL, account VARCHAR NOT NULL, currency VARCHAR NOT NULL,
    position INTEGER NOT NULL, text VARCHAR NOT NULL);
"""

_EXPORTS_SCHEMA = """
CREATE TABLE exports.meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
CREATE TABLE exports.export (
    id INTEGER PRIMARY KEY, created_at TIMESTAMP NOT NULL, to_date DATE NOT NULL,
    folder VARCHAR NOT NULL);
CREATE TABLE exports.export_file (
    id INTEGER PRIMARY KEY, export INTEGER NOT NULL, currency VARCHAR NOT NULL,
    filename VARCHAR NOT NULL, status VARCHAR NOT NULL, confirmed_at TIMESTAMP);
CREATE TABLE exports.exported_line (line_key VARCHAR NOT NULL, export_file INTEGER NOT NULL);
"""


class DatabaseError(Exception):
    """A ledger's database could not be opened or used."""


class BuildFailed(DatabaseError):
    """A kept report could not be built into data.db; the database is as it was."""


@dataclass(frozen=True)
class Line:
    """One Xero row as data.db keeps it.

    Implements: AD-112
    """

    key: str
    currency: str
    date: dt.date
    report_date: dt.date
    amount: Decimal
    description: str
    reference: str


@dataclass(frozen=True)
class Report:
    """A kept report read, converted and reconciled: what data.db stores of it."""

    statement: Statement
    generated_at: dt.datetime
    lines: list[tuple[Line, int, str | None]]  # the line, its place, its record's ID
    notes: list[tuple[str, str]] = field(default_factory=list)  # currency, the reconciliation's note

    def cash(self, which: str) -> dict[str, Decimal]:
        """Per currency, "Starting Cash" or "Ending Cash"."""
        return {
            currency: components.get(which, Decimal(0))
            for currency, components in self.statement.cash_report.items()
        }


@dataclass(frozen=True)
class Period:
    """A stored period of one IB account, the report it came from in use.

    Implements: AD-115
    """

    id: int
    account: str
    start: dt.date
    end: dt.date
    generated_at: dt.datetime


@dataclass(frozen=True)
class Note:
    period_start: dt.date
    period_end: dt.date
    currency: str
    text: str


@dataclass(frozen=True)
class ExportFile:
    """A file of an export still waiting to be confirmed as imported (D10)."""

    id: int
    export_id: int
    created_at: dt.datetime
    to_date: dt.date
    folder: str
    currency: str
    filename: str


@dataclass(frozen=True)
class BuildResult:
    rebuilt: bool
    added: int


def read_report(data: bytes, gst_rate: Decimal) -> Report:
    """Read, convert and reconcile one report, as data.db would store it.

    Raises StatementError or ReconciliationError, as the converter does; step 5
    can check a fetched report this way before the store keeps it.

    Implements: AD-114, AD-108, AD-109, AD-110
    """
    flex = read_flex(data)
    statement = flex.statement
    results = reconcile(statement, convert(statement), gst_rate=gst_rate)
    report_dates = _report_dates(statement)
    lines: list[tuple[Line, int, str | None]] = []
    notes = [(result.currency, note) for result in results for note in result.notes]
    for result in results:
        for position, row in enumerate(result.rows):
            key, kind = _line_key(statement, result.currency, row)
            report_date = report_dates.get((kind, row.source_id)) if kind else None
            line = Line(
                key=key,
                currency=result.currency,
                date=row.date,
                report_date=report_date or statement.period_end,
                amount=row.amount,
                description=row.description,
                reference=row.reference,
            )
            lines.append((line, position, row.source_id or None))
    keys = [line.key for line, _, _ in lines]
    if len(set(keys)) != len(keys):
        twice = sorted({k for k in keys if keys.count(k) > 1})
        raise StatementError(f"two rows of the report have one key: {', '.join(twice)}")
    _check_numbers(statement, lines)
    return Report(statement, flex.generated_at, lines, notes)


class Database:
    """A ledger's data.db and exports.db, open on one connection.

    Implements: AD-111
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection, data_path: Path, key: str):
        self._con = connection
        self._data_path = data_path
        self._key = key

    @classmethod
    def open(cls, data_path: str | os.PathLike[str], exports_path: str | os.PathLike[str], key: str) -> Database:
        """Implements: AD-111"""
        con = duckdb.connect(config=_CONFIG)
        try:
            _attach(con, Path(data_path), "data", key)
            _attach(con, Path(exports_path), "exports", key)
            database = cls(con, Path(data_path), key)
            database._prepare_exports()
        except duckdb.Error as error:
            con.close()
            raise DatabaseError(f"The account's database could not be opened: {error}") from None
        return database

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- Building data.db -------------------------------------------------------

    def ensure_current(
        self, names: Iterable[str], read: Callable[[str], bytes], gst_rate: Decimal
    ) -> BuildResult:
        """Bring data.db up to the ledger's kept reports (D6).

        Implements: AD-114
        """
        names = list(names)
        if self._built_with() != _build_meta(gst_rate):
            self._rebuild(names, read, gst_rate)
            return BuildResult(rebuilt=True, added=len(names))
        known = {row[0] for row in self._con.execute("SELECT original FROM data.period").fetchall()}
        new = [name for name in names if name not in known]
        if not new:
            return BuildResult(rebuilt=False, added=0)
        reports = [(name, _read(name, read, gst_rate)) for name in new]
        existing = self._con.execute(
            "SELECT account, from_date, to_date FROM data.period"
        ).fetchall()
        if any(_overlaps(report.statement, *period) for _, report in reports for period in existing):
            # A report of a period already stored: work out which wins from scratch.
            self._rebuild(names, read, gst_rate)
            return BuildResult(rebuilt=True, added=len(names))
        placed = _place_periods(reports, first_id=self._next_id("data.period"))
        _check_no_gaps(
            set(existing)
            | {(e.report.statement.account, e.report.statement.period_start,
                e.report.statement.period_end) for e in placed}
        )
        self._con.execute("BEGIN TRANSACTION")
        try:
            for entry in placed:
                _write_report(self._con, "data", entry)
            self._con.execute("COMMIT")
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        return BuildResult(rebuilt=False, added=len(new))

    def _built_with(self) -> dict[str, str] | None:
        if not _has_table(self._con, "data", "meta"):
            return None
        return dict(self._con.execute("SELECT key, value FROM data.meta").fetchall())

    def _rebuild(self, names: list[str], read: Callable[[str], bytes], gst_rate: Decimal) -> None:
        """Build into a new file and put it in data.db's place only once complete.

        Implements: AD-114
        """
        reports = [(name, _read(name, read, gst_rate)) for name in names]
        placed = _place_periods(reports, first_id=1)
        fresh = self._data_path.with_name(self._data_path.name + ".new")
        _remove(fresh)
        _attach(self._con, fresh, "fresh", self._key)
        try:
            self._con.execute(_DATA_SCHEMA.format(db="fresh"))
            _insert(self._con, "fresh.meta", sorted(_build_meta(gst_rate).items()))
            for entry in placed:
                _write_report(self._con, "fresh", entry)
            self._con.execute("DETACH fresh")
        except BaseException:
            try:
                self._con.execute("DETACH fresh")
            except duckdb.Error:
                pass
            _remove(fresh)
            raise
        self._con.execute("DETACH data")
        try:
            os.replace(fresh, self._data_path)
        finally:
            _attach(self._con, self._data_path, "data", self._key)

    def _next_id(self, table: str) -> int:
        (highest,) = self._con.execute(f"SELECT max(id) FROM {table}").fetchone()
        return (highest or 0) + 1

    # --- Reading data.db ------------------------------------------------------------

    def periods(self) -> list[Period]:
        """The periods in use — none superseded — by account and date.

        Implements: AD-115
        """
        rows = self._con.execute(
            """
            SELECT id, account, from_date, to_date, generated_at FROM data.period
            WHERE superseded_by IS NULL ORDER BY account, from_date
            """
        ).fetchall()
        return [Period(*row) for row in rows]

    def cash(self, period_id: int, which: str) -> dict[str, Decimal]:
        """A period's "Starting Cash" or "Ending Cash", per currency of its Cash Report."""
        rows = self._con.execute(
            "SELECT currency, amount FROM data.cash_report WHERE period = ? AND component = ?",
            [period_id, which],
        ).fetchall()
        return {currency: amount for currency, amount in rows}

    def currencies(self) -> list[str]:
        """Every currency in the Cash Reports in use (D13)."""
        rows = self._con.execute(
            """
            SELECT DISTINCT c.currency FROM data.cash_report c JOIN data.period p ON c.period = p.id
            WHERE p.superseded_by IS NULL ORDER BY 1
            """
        ).fetchall()
        return [row[0] for row in rows]

    def period_lines(self, period_id: int) -> list[Line]:
        """A period's lines, in output order per currency."""
        rows = self._con.execute(
            f"SELECT {_LINE_COLUMNS} FROM data.line l WHERE l.period = ? "
            "ORDER BY l.currency, l.account, l.position",
            [period_id],
        ).fetchall()
        return [Line(*row) for row in rows]

    def lines_dated(self, start: dt.date, end: dt.date) -> dict[str, list[Line]]:
        """Per currency, in output order, every line dated from `start` to `end` (D10's re-export).

        Implements: AD-116
        """
        rows = self._con.execute(
            f"""
            SELECT {_LINE_COLUMNS} FROM data.line l JOIN data.period p ON l.period = p.id
            WHERE l.date BETWEEN ? AND ?
            ORDER BY l.currency, p.from_date, l.account, l.position
            """,
            [start, end],
        ).fetchall()
        out: dict[str, list[Line]] = {}
        for row in rows:
            line = Line(*row)
            out.setdefault(line.currency, []).append(line)
        return out

    def notes(self, keys: Iterable[str]) -> list[Note]:
        """The reconciliation's notes on the periods and currencies these lines come from."""
        keys = list(keys)
        if not keys:
            return []
        rows = self._con.execute(
            """
            SELECT p.from_date, p.to_date, n.currency, n.text FROM data.note n
            JOIN data.period p ON n.period = p.id
            JOIN (SELECT DISTINCT period, currency FROM data.line WHERE key IN (SELECT unnest(?))) s
                ON s.period = n.period AND s.currency = n.currency
            ORDER BY n.currency, p.from_date, n.account, n.position
            """,
            [keys],
        ).fetchall()
        return [Note(*row) for row in rows]

    # --- The export record ------------------------------------------------------

    def unexported_lines(
        self, to_date: dt.date, imported_through: Mapping[str, dt.date] | None = None
    ) -> dict[str, list[Line]]:
        """Per currency, in output order, the lines an export up to `to_date` holds (D10, D12).

        Implements: AD-116
        """
        imported_through = imported_through or {}
        rows = self._con.execute(
            f"""
            SELECT {_LINE_COLUMNS}
            FROM data.line l JOIN data.period p ON l.period = p.id
            WHERE l.date <= ? AND l.key NOT IN (
                SELECT e.line_key FROM exports.exported_line e
                JOIN exports.export_file f ON e.export_file = f.id
                WHERE f.status = 'confirmed')
            ORDER BY l.currency, p.from_date, l.account, l.position
            """,
            [to_date],
        ).fetchall()
        out: dict[str, list[Line]] = {}
        for row in rows:
            line = Line(*row)
            through = imported_through.get(line.currency)
            if through is not None and line.report_date <= through:
                continue  # already in Xero before the store began (D7, D12)
            out.setdefault(line.currency, []).append(line)
        return out

    def record_export(
        self,
        to_date: dt.date,
        folder: str,
        files: Mapping[str, tuple[str, Sequence[str]]],
        *,
        created_at: dt.datetime | None = None,
    ) -> dict[str, int]:
        """Record an export: per currency its file name and line keys, all pending.

        Implements: AD-116
        """
        keys = [key for _, file_keys in files.values() for key in file_keys]
        if keys:
            known = {
                row[0]
                for row in self._con.execute(
                    "SELECT key FROM data.line WHERE key IN (SELECT unnest(?))", [keys]
                ).fetchall()
            }
            unknown = sorted(set(keys) - known)
            if unknown:
                raise ValueError(f"not lines of this account: {', '.join(unknown[:5])}")
        created_at = created_at or dt.datetime.now().replace(microsecond=0)
        self._con.execute("BEGIN TRANSACTION")
        try:
            export_id = self._next_id("exports.export")
            _insert(self._con, "exports.export", [(export_id, created_at, to_date, folder)])
            file_id = self._next_id("exports.export_file")
            ids: dict[str, int] = {}
            for currency, (filename, file_keys) in sorted(files.items()):
                _insert(
                    self._con,
                    "exports.export_file",
                    [(file_id, export_id, currency, filename, "pending", None)],
                )
                _insert(self._con, "exports.exported_line", [(key, file_id) for key in file_keys])
                ids[currency] = file_id
                file_id += 1
            self._con.execute("COMMIT")
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        return ids

    def pending_files(self) -> list[ExportFile]:
        """Implements: AD-116"""
        rows = self._con.execute(
            """
            SELECT f.id, e.id, e.created_at, e.to_date, e.folder, f.currency, f.filename
            FROM exports.export_file f JOIN exports.export e ON f.export = e.id
            WHERE f.status = 'pending' ORDER BY f.id
            """
        ).fetchall()
        return [ExportFile(*row) for row in rows]

    def confirm(self, file_id: int, *, when: dt.datetime | None = None) -> None:
        """Mark a file imported into Xero; its lines are not exported again (D10).

        Implements: AD-116
        """
        found = self._con.execute(
            "SELECT status FROM exports.export_file WHERE id = ?", [file_id]
        ).fetchone()
        if found is None:
            raise DatabaseError(f"There is no exported file {file_id}.")
        if found[0] == "confirmed":
            return
        if found[0] == "discarded":
            raise DatabaseError(f"Exported file {file_id} was discarded; it cannot be confirmed.")
        when = when or dt.datetime.now().replace(microsecond=0)
        self._con.execute(
            "UPDATE exports.export_file SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            [when, file_id],
        )

    def discard(self, file_id: int) -> None:
        """Mark a pending file not imported: its lines are exported again, and it can
        never be confirmed (D67).

        Implements: AD-116
        """
        found = self._con.execute(
            "SELECT status FROM exports.export_file WHERE id = ?", [file_id]
        ).fetchone()
        if found is None:
            raise DatabaseError(f"There is no exported file {file_id}.")
        if found[0] == "confirmed":
            raise DatabaseError(f"Exported file {file_id} was confirmed; it cannot be discarded.")
        self._con.execute(
            "UPDATE exports.export_file SET status = 'discarded' WHERE id = ?", [file_id]
        )

    def export_status(self, keys: Iterable[str]) -> dict[str, str]:
        """Of these lines, those in an exported file: "confirmed", else "pending".

        Implements: AD-116
        """
        keys = list(keys)
        if not keys:
            return {}
        rows = self._con.execute(
            """
            SELECT e.line_key, max(CASE f.status WHEN 'confirmed' THEN 2 ELSE 1 END)
            FROM exports.exported_line e JOIN exports.export_file f ON e.export_file = f.id
            WHERE f.status IN ('confirmed', 'pending') AND e.line_key IN (SELECT unnest(?))
            GROUP BY e.line_key
            """,
            [keys],
        ).fetchall()
        return {key: "confirmed" if rank == 2 else "pending" for key, rank in rows}

    def has_exports(self) -> bool:
        """Whether anything has ever been exported."""
        (count,) = self._con.execute("SELECT count(*) FROM exports.export").fetchone()
        return count > 0

    def _prepare_exports(self) -> None:
        if not _has_table(self._con, "exports", "meta"):
            self._con.execute(_EXPORTS_SCHEMA)
            _insert(self._con, "exports.meta", [("schema_version", str(EXPORTS_SCHEMA_VERSION))])
            return
        found = self._con.execute(
            "SELECT value FROM exports.meta WHERE key = 'schema_version'"
        ).fetchone()
        if found is None or found[0] != str(EXPORTS_SCHEMA_VERSION):
            raise DatabaseError(
                f"The export record is of version {found and found[0]!r}, "
                "which this version cannot read."
            )


# --- Building, one report at a time ----------------------------------------------


@dataclass(frozen=True)
class _Placed:
    id: int
    name: str
    report: Report
    superseded_by: int | None


def _read(name: str, read: Callable[[str], bytes], gst_rate: Decimal) -> Report:
    data = read(name)
    try:
        return read_report(data, gst_rate)
    except (StatementError, ReconciliationError) as error:
        raise BuildFailed(f"A kept report ({_describe(data)}) cannot be built: {error}") from None


def _describe(data: bytes) -> str:
    try:
        statement = read_flex(data).statement
    except StatementError:
        return "unreadable"
    return f"{statement.account}, {statement.period_start} to {statement.period_end}"


def _place_periods(reports: list[tuple[str, Report]], first_id: int) -> list[_Placed]:
    """Number the periods; of reports of one period, the latest generated is used (D5).

    Implements: AD-115
    """
    ordered = sorted(
        reports,
        key=lambda item: (
            item[1].statement.account,
            item[1].statement.period_start,
            item[1].statement.period_end,
            item[1].generated_at,
            item[0],
        ),
    )
    ids = {name: first_id + index for index, (name, _) in enumerate(ordered)}
    placed: list[_Placed] = []
    for index, (name, report) in enumerate(ordered):
        statement = report.statement
        later = [
            other_name
            for other_name, other in ordered[index + 1 :]
            if _same_period(statement, other.statement)
        ]
        superseded_by = ids[later[-1]] if later else None
        for other_name, other in ordered[index + 1 :]:
            if not _same_period(statement, other.statement) and _overlaps(
                other.statement, statement.account, statement.period_start, statement.period_end
            ):
                raise BuildFailed(
                    f"Two kept reports of {statement.account} overlap without covering the same "
                    f"period ({statement.period_start} to {statement.period_end}, and "
                    f"{other.statement.period_start} to {other.statement.period_end})."
                )
        placed.append(_Placed(ids[name], name, report, superseded_by))
    _check_no_gaps(
        (e.report.statement.account, e.report.statement.period_start, e.report.statement.period_end)
        for e in placed
    )
    return placed


def _check_no_gaps(spans: Iterable[tuple[str, dt.date, dt.date]]) -> None:
    """Per IB account, the periods cover every day from the first to the last (D69).

    Implements: AD-115
    """
    by_account: dict[str, list[tuple[dt.date, dt.date]]] = {}
    for account, start, end in set(spans):
        by_account.setdefault(account, []).append((start, end))
    for account, periods in by_account.items():
        periods.sort()
        for (_, end), (start, _) in zip(periods, periods[1:]):
            if start != end + dt.timedelta(days=1):
                raise BuildFailed(
                    f"The kept reports of {account} leave a gap: nothing from "
                    f"{end + dt.timedelta(days=1)} to {start - dt.timedelta(days=1)}."
                )


def _same_period(a: Statement, b: Statement) -> bool:
    return (a.account, a.period_start, a.period_end) == (b.account, b.period_start, b.period_end)


def _overlaps(statement: Statement, account: str, start: dt.date, end: dt.date) -> bool:
    return (
        statement.account == account
        and statement.period_start <= end
        and start <= statement.period_end
    )


def _write_report(con: duckdb.DuckDBPyConnection, db: str, entry: _Placed) -> None:
    """Implements: AD-112"""
    statement, report = entry.report.statement, entry.report
    account, period = statement.account, entry.id
    _insert(con, f"{db}.period", [(
        period, account, statement.period_start, statement.period_end, entry.name,
        report.generated_at, entry.superseded_by,
    )])
    if entry.superseded_by is not None:
        return  # kept as a record that it was seen; its rows are the later report's
    con.execute(
        f"INSERT OR REPLACE INTO {db}.ib_account VALUES (?, ?)", [account, statement.base_currency]
    )
    _insert(con, f"{db}.cash_report", [
        (period, account, currency, component, amount)
        for currency, components in statement.cash_report.items()
        for component, amount in components.items()
    ])
    _insert(con, f"{db}.trade", [
        (t.id, period, account, t.report_date, t.when, t.currency, t.asset_class.value, t.symbol,
         t.quantity, t.price, t.proceeds, t.commission, t.commission_currency,
         ";".join(sorted(t.codes)))
        for t in statement.trades
    ])
    _insert(con, f"{db}.cash_txn", [
        (c.id, period, account, c.report_date, c.date, c.currency, c.kind.value, c.description,
         c.amount)
        for c in statement.cash_txns
    ])
    _insert(con, f"{db}.corporate_action", [
        (a.id, period, account, a.report_date, a.when, a.currency, a.description, a.proceeds)
        for a in statement.corporate_actions
    ])
    _insert(con, f"{db}.tax_item", [
        (x.id, period, account, x.report_date, x.when, x.currency, x.kind.value, x.symbol,
         x.amount, x.taxed_id)
        for x in statement.tax_items
    ])
    try:
        _insert(con, f"{db}.line", [
            (line.key, period, account, line.currency, position, line.date, line.report_date,
             line.amount, line.description, line.reference, source_id)
            for line, position, source_id in report.lines
        ])
    except duckdb.ConstraintException as error:
        raise BuildFailed(
            f"A kept report ({account}, {statement.period_start} to {statement.period_end}) "
            f"repeats a transaction already stored: {error}"
        ) from None
    _insert(con, f"{db}.note", [
        (period, account, currency, position, text)
        for position, (currency, text) in enumerate(report.notes)
    ])


def _line_key(statement: Statement, currency: str, row: OutputRow) -> tuple[str, str | None]:
    """A line's key, the same in every rebuild (D18), and the kind of record it came from.

    Implements: AD-113
    """
    account = statement.account
    if not row.source_id:
        if row.reference not in _SYNTHETIC:
            raise StatementError(f"a row names no record: {row.description!r}")
        span = f"{statement.period_start:%Y%m%d}-{statement.period_end:%Y%m%d}"
        return f"{row.reference}:{account}:{span}:{currency}", None
    if row.source_section == "Trades":
        kind = "T"
    elif row.source_section == "Corporate Actions":
        kind = "A"
    else:
        kind = "C"  # a cash transaction, named by its kind
    key = f"{account}:{kind}:{row.source_id}"
    return (f"{key}:{row.leg}" if row.leg else key), kind


def _report_dates(statement: Statement) -> dict[tuple[str, str], dt.date | None]:
    dates: dict[tuple[str, str], dt.date | None] = {}
    dates.update({("T", t.id): t.report_date for t in statement.trades})
    dates.update({("A", a.id): a.report_date for a in statement.corporate_actions})
    dates.update({("C", c.id): c.report_date for c in statement.cash_txns})
    return dates


def _check_numbers(statement: Statement, lines: list[tuple[Line, int, str | None]]) -> None:
    """Refuse what DECIMAL(38,11) would round or cannot hold (D18).

    Implements: AD-112
    """
    values: list[tuple[str, Decimal | None]] = []
    for currency, components in statement.cash_report.items():
        values += [(f"Cash Report {currency} {name}", v) for name, v in components.items()]
    for t in statement.trades:
        values += [(t.where, v) for v in (t.quantity, t.price, t.proceeds, t.commission)]
    values += [(c.where, c.amount) for c in statement.cash_txns]
    values += [(a.where, a.proceeds) for a in statement.corporate_actions]
    values += [(x.where, x.amount) for x in statement.tax_items]
    values += [(line.key, line.amount) for line, _, _ in lines]
    for where, value in values:
        if value is None:
            continue
        if not value.is_finite() or abs(value) >= _TOO_LARGE:
            raise StatementError(f"{where}: {value} is too large to store")
        if value.as_tuple().exponent < -_SCALE and value != value.quantize(
            _STEP, context=_WIDE
        ):
            raise StatementError(
                f"{where}: {value} has more than {_SCALE} decimal places; the store would round it"
            )


def _build_meta(gst_rate: Decimal) -> dict[str, str]:
    return {
        "parser_version": str(PARSER_VERSION),
        "schema_version": str(SCHEMA_VERSION),
        "gst_rate": format(gst_rate.normalize(), "f"),
    }


# --- DuckDB -------------------------------------------------------------------------


def _attach(con: duckdb.DuckDBPyConnection, path: Path, alias: str, key: str) -> None:
    if not all(c in "0123456789abcdef" for c in key):
        raise ValueError("a database key is hex text")
    quoted = str(path).replace("'", "''")
    con.execute(f"ATTACH '{quoted}' AS {alias} (ENCRYPTION_KEY '{key}')")


def _has_table(con: duckdb.DuckDBPyConnection, database: str, table: str) -> bool:
    (count,) = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
        [database, table],
    ).fetchone()
    return count > 0


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Multi-row INSERTs, `_CHUNK` rows at a time.

    Implements: AD-112
    """
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        width = len(chunk[0])
        values = ",".join(["(" + ",".join(["?"] * width) + ")"] * len(chunk))
        con.execute(f"INSERT INTO {table} VALUES {values}", [v for row in chunk for v in row])


def _remove(path: Path) -> None:
    for leftover in (path, path.with_name(path.name + ".wal")):
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
