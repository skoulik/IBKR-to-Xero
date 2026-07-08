"""Front-end-agnostic conversion engine.

Wraps the pipeline (parse -> convert -> reconcile -> write) behind a single
entry point so the CLI, a GUI, or a web/API adaptor can all drive it the same
way. The engine never performs console I/O: it returns a structured result and
lets errors propagate as the existing typed exceptions (StatementError,
ReconciliationError, and FileExistsError from the overwrite check).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .convert import convert
from .parser import parse_statement
from .reconcile import reconcile
from .writer import write_report, write_results


@dataclass
class RunOptions:
    """Front-end-agnostic knobs for a single conversion run."""

    output_dir: Path | None = None  # None -> default rule (statement stem)
    force_overwrite: bool = False
    skip_zero_transactions: bool = False
    report_name: str | None = "ibkr2xero_report.txt"  # None -> don't save
    accept_unattributed_gst: bool = False


@dataclass
class FileOutput:
    """One written CSV, with the summary and notes that describe it."""

    path: Path
    summary: str  # "N transactions, cash X -> Y"
    notes: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    """Structured outcome of a successful run, ready for any front-end."""

    account_line: str  # "account U..., period ... to ..."
    output_dir: Path
    files: list[FileOutput]
    report_text: str  # canonical report (bare filenames, the saved form)
    report_path: Path | None  # None when saving is disabled


def resolve_output_dir(statement_path: Path, output_dir: Path | None) -> Path:
    """Apply the default output-dir rule: an explicit dir wins, otherwise a
    subfolder next to the statement, named after it without the extension."""
    if output_dir is not None:
        return output_dir
    subfolder = statement_path.stem
    if subfolder == statement_path.name:  # no extension to strip
        subfolder += "_out"
    return statement_path.parent / subfolder


def run(statement_path: Path, options: RunOptions) -> RunResult:
    """Convert one statement into per-currency Xero CSVs.

    Performs parse -> convert -> reconcile -> zero-transaction filtering ->
    output-dir resolution -> write -> report assembly. Never prints. Errors
    propagate as StatementError / ReconciliationError (bad or unreconcilable
    input) or FileExistsError (overwrite check); on any of them no output
    files are written.
    """
    statement = parse_statement(statement_path)
    results = reconcile(
        statement,
        convert(statement),
        accept_unattributed_gst=options.accept_unattributed_gst,
    )

    if options.skip_zero_transactions:
        # Safe only after reconciliation: zero rows contribute nothing to the
        # sums, so dropping them cannot mask a mismatch.
        for result in results:
            kept = [row for row in result.rows if row.amount != 0]
            skipped = len(result.rows) - len(kept)
            if skipped:
                result.rows = kept
                result.notes.append(f"skipped {skipped} zero-amount transaction(s)")
        # A currency left with no rows at all is treated like one with no
        # activity: no file.
        results = [r for r in results if r.rows]

    account_line = (
        f"account {statement.account or '?'}, period "
        f"{statement.period_start} to {statement.period_end}"
    )
    out_dir = resolve_output_dir(statement_path, options.output_dir)

    paths = write_results(
        results,
        out_dir,
        overwrite=options.force_overwrite,
        report_name=options.report_name,
    )

    files = [
        FileOutput(
            path=path,
            summary=(
                f"{len(result.rows)} transactions, "
                f"cash {result.starting_cash} -> {result.ending_cash}"
            ),
            notes=list(result.notes),
        )
        for result, path in zip(results, paths)
    ]

    report_lines = [
        "ibkr2xero conversion report",
        f"statement: {statement_path}",
        f"generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        account_line,
    ]
    for file in files:
        report_lines.append(f"  {file.path.name}: {file.summary}")
        for note in file.notes:
            report_lines.append(f"    note: {note}")
    if not files:
        report_lines.append("  no cash activity in any currency; nothing to write")
    report_text = "\n".join(report_lines) + "\n"

    report_path = None
    if options.report_name is not None:
        report_path = write_report(out_dir, options.report_name, report_text)

    return RunResult(
        account_line=account_line,
        output_dir=out_dir,
        files=files,
        report_text=report_text,
        report_path=report_path,
    )
