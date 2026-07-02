"""Command-line interface: ib-connector <statement.csv> [-o OUTDIR]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .convert import convert
from .model import ReconciliationError, StatementError
from .parser import parse_statement
from .reconcile import reconcile
from .writer import write_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ib-connector",
        description=(
            "Convert an IB Activity Statement CSV into per-currency Xero import CSVs. "
            "Refuses to write anything unless cash reconciles per currency."
        ),
    )
    parser.add_argument("statement", type=Path, help="IB Activity Statement CSV file")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="output directory (default: the statement file's directory)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite existing output files (default: abort without writing anything)",
    )
    parser.add_argument(
        "--skip-zero",
        action="store_true",
        help="omit zero-amount transactions (e.g. option expiries) from the output; "
        "they carry no cash and Xero discards them on import anyway",
    )
    args = parser.parse_args(argv)

    try:
        statement = parse_statement(args.statement)
        results = reconcile(statement, convert(statement))
    except (StatementError, ReconciliationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("no output files were written.", file=sys.stderr)
        return 2

    if args.skip_zero:
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

    print(
        f"account {statement.account or '?'}, period "
        f"{statement.period_start} to {statement.period_end}"
    )
    out_dir = args.out if args.out is not None else args.statement.parent
    try:
        paths = write_results(results, out_dir, overwrite=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("no output files were written. Use --force to overwrite.", file=sys.stderr)
        return 2
    for result, path in zip(results, paths):
        print(
            f"  {path}: {len(result.rows)} transactions, "
            f"cash {result.starting_cash} -> {result.ending_cash}"
        )
        for note in result.notes:
            print(f"    note: {note}")
    if not paths:
        print("  no cash activity in any currency; nothing to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
