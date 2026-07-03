"""Write per-currency CSVs in the Xero bank-statement import format."""

from __future__ import annotations

import csv
from pathlib import Path

from .model import CurrencyResult, fmt_number

HEADER = ["*Date", "*Amount", "Payee", "Description", "Reference", "Cheque Number"]


def write_results(
    results: list[CurrencyResult],
    out_dir: str | Path,
    overwrite: bool = False,
    report_name: str | None = None,
) -> list[Path]:
    """Write one {CCY}.csv per result. Only call this after reconciliation.

    Unless overwrite is set, refuses to touch anything if any target file
    already exists — all or nothing, like the rest of the pipeline.
    report_name reserves one more target in that check for the run report,
    which the caller composes and saves afterwards via write_report().
    """
    out_dir = Path(out_dir)
    targets = [out_dir / f"{result.currency}.csv" for result in results]
    if not overwrite:
        reserved = targets + ([out_dir / report_name] if report_name else [])
        existing = [str(path) for path in reserved if path.exists()]
        if existing:
            raise FileExistsError(
                f"output file(s) already exist: {', '.join(existing)}"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result, path in zip(results, targets):
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            for row in result.rows:
                writer.writerow(
                    [
                        row.date.isoformat(),
                        fmt_number(row.amount),
                        row.payee,
                        row.description,
                        row.reference,
                        "",
                    ]
                )
        written.append(path)
    return written


def write_report(out_dir: str | Path, name: str, text: str) -> Path:
    """Save the human-readable run report next to the CSVs."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(text, encoding="utf-8")
    return path
