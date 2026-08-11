"""PyInstaller entry point for the command-line application."""

from ibkr_to_xero.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
