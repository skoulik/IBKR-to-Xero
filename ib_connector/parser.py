"""Parse the multi-section IB Activity Statement CSV into a Statement model.

File format: every row is `SectionName,RowKind,...` where RowKind is `Header`
or `Data`. A Header row defines the column names for the Data rows that follow
it; a section may re-emit Header rows (e.g. Trades does so per asset category),
so we keep a running "current header" per section. The first cell of the file
carries a UTF-8 BOM (handled by utf-8-sig).
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

from .model import SectionRow, Statement, StatementError

_ROW_KINDS = {"Header", "Data"}

# Aggregate/footnote row kinds IB emits alongside Header/Data. They never hold
# transactions (per-currency sums are cross-checked via the Cash Report), so
# they are skipped; any other unknown kind still rejects the file.
_SKIPPED_ROW_KINDS = {"Total", "SubTotal", "Total in USD", "Notes"}

# Statement section "Period" field: "June 1, 2026 - June 30, 2026"
# (single-day statements use just "June 1, 2026")
_PERIOD_SEP = re.compile(r"\s+-\s+")


def _parse_period(text: str) -> tuple[dt.date, dt.date]:
    parts = _PERIOD_SEP.split(text.strip())
    if len(parts) not in (1, 2):
        raise StatementError(f"cannot parse statement period {text!r}")
    try:
        dates = [dt.datetime.strptime(p.strip(), "%B %d, %Y").date() for p in parts]
    except ValueError as exc:
        raise StatementError(f"cannot parse statement period {text!r}") from exc
    return dates[0], dates[-1]


def _field_map(rows: list[SectionRow]) -> dict[str, str]:
    """Fold Field Name / Field Value rows into a dict."""
    return {r["Field Name"]: r["Field Value"] for r in rows}


def parse_statement(path: str | Path) -> Statement:
    """Read an IB Activity Statement CSV file into a Statement.

    Raises StatementError on any structural surprise: better to reject the
    input than to silently mis-read it.
    """
    path = Path(path)
    sections: dict[str, list[SectionRow]] = {}
    current_headers: dict[str, list[str]] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        for line_no, row in enumerate(csv.reader(fh), start=1):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) < 2:
                raise StatementError(f"line {line_no}: expected 'Section,Kind,...', got {row!r}")
            section, kind, cells = row[0], row[1], row[2:]
            if not kind or kind in _SKIPPED_ROW_KINDS:
                # Aggregate/footnote rows, and one-line summary sections
                # ("Total P/L for Statement Period") with no marker at all:
                # no transactions here, skip rather than reject.
                continue
            if kind not in _ROW_KINDS:
                raise StatementError(
                    f"line {line_no}: unknown row kind {kind!r} in section {section!r} "
                    "(expected Header or Data)"
                )
            if kind == "Header":
                current_headers[section] = cells
                sections.setdefault(section, [])
                continue
            headers = current_headers.get(section)
            if headers is None:
                raise StatementError(
                    f"line {line_no}: Data row in section {section!r} before any Header row"
                )
            if len(cells) > len(headers):
                # Tolerate only trailing empty cells (IB emits trailing commas).
                extra = cells[len(headers):]
                if any(cell.strip() for cell in extra):
                    raise StatementError(
                        f"line {line_no}: section {section!r} row has {len(cells)} cells "
                        f"but header has {len(headers)} columns; extra data: {extra!r}"
                    )
                cells = cells[: len(headers)]
            elif len(cells) < len(headers):
                cells = cells + [""] * (len(headers) - len(cells))
            values = dict(zip(headers, cells))
            sections.setdefault(section, []).append(SectionRow(section, values, line_no))

    meta = _field_map(sections.get("Statement", []))
    period = meta.get("Period")
    if not period:
        raise StatementError("statement has no 'Statement' section with a Period field")
    period_start, period_end = _parse_period(period)

    account = _field_map(sections.get("Account Information", [])).get("Account", "")

    return Statement(
        sections=sections,
        period_start=period_start,
        period_end=period_end,
        account=account,
    )
