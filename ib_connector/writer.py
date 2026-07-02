"""Write per-currency CSVs in the Xero bank-statement import format."""

from __future__ import annotations

import csv
from pathlib import Path

from .model import CurrencyResult, fmt_number

HEADER = ["*Date", "*Amount", "Payee", "Description", "Reference", "Cheque Number"]


def write_results(results: list[CurrencyResult], out_dir: str | Path) -> list[Path]:
    """Write one {CCY}.csv per result. Only call this after reconciliation."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in results:
        path = out_dir / f"{result.currency}.csv"
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
