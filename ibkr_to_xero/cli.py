"""Command-line interface: ibkr2xero <statement.csv> [-o OUTDIR]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import RunOptions, run
from .model import ReconciliationError, StatementError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ibkr2xero",
        description=(
            "Convert an IB Activity Statement CSV into per-currency Xero import CSVs. "
            "Refuses to write anything unless cash reconciles per currency."
        ),
    )
    parser.add_argument("statement", type=Path, help="IB Activity Statement CSV file")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="output directory (default: a subfolder next to the statement, "
        "named after it without the extension)",
    )
    parser.add_argument(
        "-f",
        "--force-overwrite",
        action="store_true",
        help="overwrite existing output files (default: abort without writing anything)",
    )
    parser.add_argument(
        "-s",
        "--skip-zero-transactions",
        action="store_true",
        help="omit zero-amount transactions (e.g. option expiries) from the output; "
        "they carry no cash and Xero discards them on import anyway",
    )
    parser.add_argument(
        "-r",
        "--report-name",
        default="ibkr2xero_report.txt",
        help="file name for the conversion report saved into the output "
        "directory (default: %(default)s)",
    )
    parser.add_argument(
        "--save-report",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="save the conversion report (the summary printed to the console) "
        "alongside the output CSVs",
    )
    parser.add_argument(
        "--accept-unattributed-gst",
        action="store_true",
        help="accept a GST amount that cannot be verified as 10%% GST on specific "
        "fee rows, as a single lump-sum line (default: reject the input)",
    )
    args = parser.parse_args(argv)

    options = RunOptions(
        output_dir=args.output_dir,
        force_overwrite=args.force_overwrite,
        skip_zero_transactions=args.skip_zero_transactions,
        report_name=args.report_name if args.save_report else None,
        accept_unattributed_gst=args.accept_unattributed_gst,
    )

    try:
        result = run(args.statement, options)
    except (StatementError, ReconciliationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("no output files were written.", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "no output files were written. Use --force-overwrite to overwrite.",
            file=sys.stderr,
        )
        return 2

    # Report text carries bare filenames (the saved form); the console shows
    # full paths, rebuilt here from the structured result.
    print(result.account_line)
    for file in result.files:
        print(f"  {file.path}: {file.summary}")
        for note in file.notes:
            print(f"    note: {note}")
    if not result.files:
        print("  no cash activity in any currency; nothing to write")
    if result.report_path is not None:
        print(f"  report saved to {result.report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
