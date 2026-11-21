"""Command-line interface: ibkr2xero <statement.csv|report.xml> [-o OUTDIR], or
ibkr2xero COMMAND ... over the store (store_cli.py).

Implements: AD-060, AD-001, AD-005, AD-074, AD-129
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .engine import RunOptions, run
from .model import ReconciliationError, StatementError

# The commands over the store (store_cli.py), imported only when one is named: the
# store's dependencies take longer to load than a one-shot conversion does to run.
STORE_COMMANDS = ("fetch", "export", "confirm", "refetch", "status", "account", "password",
                  "start-over")


def _app_version() -> str:
    try:
        return version("ibkr-to-xero")
    except PackageNotFoundError:  # running from a source tree without metadata
        return "dev"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in STORE_COMMANDS:
        # Implements: AD-129 — the first word names a command over the store
        from . import store_cli

        return store_cli.main(argv)
    parser = argparse.ArgumentParser(
        prog="ibkr2xero",
        description=(
            "Convert an IB Activity Statement CSV (or an Activity Flex Query report "
            "in XML) into per-currency Xero import CSVs. "
            "Refuses to write anything unless cash reconciles per currency."
        ),
        epilog=(
            "Reports fetched from IBKR's Flex Web Service are kept in a store, worked on with "
            f"commands: {', '.join(STORE_COMMANDS)}. `ibkr2xero COMMAND --help` says more. "
            "A statement file named like a command is given as ./NAME."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ibkr2xero {_app_version()}",
        help="show the version and exit",
    )
    parser.add_argument(
        "statement", type=Path, help="IB Activity Statement CSV, or Flex report XML file"
    )
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
