"""Command-line interface: ibkr2xero <statement.csv> [-o OUTDIR]."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from .convert import convert
from .model import ReconciliationError, StatementError
from .parser import parse_statement
from .reconcile import reconcile
from .writer import write_report, write_results


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

    try:
        statement = parse_statement(args.statement)
        results = reconcile(
            statement,
            convert(statement),
            accept_unattributed_gst=args.accept_unattributed_gst,
        )
    except (StatementError, ReconciliationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("no output files were written.", file=sys.stderr)
        return 2

    if args.skip_zero_transactions:
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
    print(account_line)
    if args.output_dir is not None:
        out_dir = args.output_dir
    else:
        subfolder = args.statement.stem
        if subfolder == args.statement.name:  # no extension to strip
            subfolder += "_out"
        out_dir = args.statement.parent / subfolder
    report_name = args.report_name if args.save_report else None
    try:
        paths = write_results(
            results, out_dir, overwrite=args.force_overwrite, report_name=report_name
        )
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "no output files were written. Use --force-overwrite to overwrite.",
            file=sys.stderr,
        )
        return 2
    report_lines = [
        "ibkr2xero conversion report",
        f"statement: {args.statement}",
        f"generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        account_line,
    ]
    for result, path in zip(results, paths):
        summary = (
            f"{len(result.rows)} transactions, "
            f"cash {result.starting_cash} -> {result.ending_cash}"
        )
        print(f"  {path}: {summary}")
        report_lines.append(f"  {path.name}: {summary}")
        for note in result.notes:
            print(f"    note: {note}")
            report_lines.append(f"    note: {note}")
    if not paths:
        no_activity = "  no cash activity in any currency; nothing to write"
        print(no_activity)
        report_lines.append(no_activity)
    if report_name is not None:
        report_path = write_report(
            out_dir, report_name, "\n".join(report_lines) + "\n"
        )
        print(f"  report saved to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
