"""End-to-end: CLI on the real example statement vs the reference outputs.

The reference files were originally hand-made; on 2026-07-03 they were
regenerated from verified converter output when the unified trade-description
format landed (the originals are kept locally as examples/*.csv.orig). The
regeneration was gated on the Date and Amount columns being identical
row-by-row to the hand-made files — only Description and Reference changed —
so the reconciliation identity (Starting + transactions = Ending) remains the
independent check on the amounts.
"""

import csv

from ibkr_to_xero.cli import main
from tests.conftest import EXAMPLES


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_cli_end_to_end(statement_csv, tmp_path, capsys):
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 0

    written = sorted(p.name for p in tmp_path.iterdir())
    # no HKD: no cash activity
    assert written == ["AUD.csv", "USD.csv", "ibkr2xero_report.txt"]

    for name in ("AUD.csv", "USD.csv"):
        assert _read(tmp_path / name) == _read(EXAMPLES / name)

    # The synthetic rows are part of the references; spot-check they exist.
    usd = _read(tmp_path / "USD.csv")
    assert [row[1] for row in usd if row[4] == "MTM"] == ["2575"]
    assert len([row for row in usd if row[4] == "ROUNDING"]) == 1

    out = capsys.readouterr().out
    assert "account U" in out
    assert "report saved to" in out

    # The saved report mirrors the console summary (filenames, not paths).
    report = (tmp_path / "ibkr2xero_report.txt").read_text(encoding="utf-8")
    assert "account U" in report
    assert "AUD.csv:" in report
    assert "USD.csv:" in report
    assert f"statement: {statement_csv}" in report


def test_cli_skip_zero(statement_csv, tmp_path, capsys):
    default_dir = tmp_path / "default"
    skip_dir = tmp_path / "skip"
    assert main([str(statement_csv), "-o", str(default_dir)]) == 0
    assert main([str(statement_csv), "-o", str(skip_dir), "--skip-zero-transactions"]) == 0

    for name in ("AUD.csv", "USD.csv"):
        default_rows = _read(default_dir / name)
        skip_rows = _read(skip_dir / name)
        assert [r for r in default_rows if r[1] == "0"], f"{name}: fixture lost its zero rows"
        assert not [r for r in skip_rows if r[1] == "0"]
        # Skipping zero rows must change nothing else, including the sum.
        assert skip_rows == [r for r in default_rows if r[1] != "0"]

    assert "skipped" in capsys.readouterr().out


def test_cli_default_output_is_statement_subfolder(statement_csv, tmp_path):
    stmt = tmp_path / statement_csv.name
    stmt.write_bytes(statement_csv.read_bytes())
    assert main([str(stmt)]) == 0
    out_dir = tmp_path / statement_csv.stem
    assert (out_dir / "AUD.csv").exists()
    assert (out_dir / "USD.csv").exists()


def test_cli_default_output_extensionless_gets_out_suffix(statement_csv, tmp_path):
    stmt = tmp_path / "statement"
    stmt.write_bytes(statement_csv.read_bytes())
    assert main([str(stmt)]) == 0
    assert (tmp_path / "statement_out" / "AUD.csv").exists()
    assert (tmp_path / "statement_out" / "USD.csv").exists()


def test_cli_refuses_to_overwrite_without_force(statement_csv, tmp_path, capsys):
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 0
    before = (tmp_path / "AUD.csv").read_bytes()
    # Second run must abort entirely and change nothing.
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "already exist" in err
    assert "--force-overwrite" in err
    assert (tmp_path / "AUD.csv").read_bytes() == before
    # --force-overwrite allows the rewrite.
    assert main([str(statement_csv), "-o", str(tmp_path), "--force-overwrite"]) == 0


# A minimal synthetic statement whose GST component (-2) cannot be verified
# as 10% GST on its single fee row (-15 -> -1.50). No real account data.
_UNVERIFIABLE_GST_STATEMENT = """\
Statement,Header,Field Name,Field Value
Statement,Data,Period,"June 1, 2026 - June 30, 2026"
Account Information,Header,Field Name,Field Value
Account Information,Data,Account,U0000000
Cash Report,Header,Currency Summary,Currency,Total
Cash Report,Data,Starting Cash,AUD,100
Cash Report,Data,Other Fees,AUD,-15
Cash Report,Data,GST,AUD,-2
Cash Report,Data,Ending Cash,AUD,83
Fees,Header,Subtitle,Currency,Date,Description,Amount
Fees,Data,Other Fees,AUD,2026-06-10,Withdrawal Fee,-15
"""


def test_cli_accept_unattributed_gst_flag(tmp_path, capsys):
    stmt = tmp_path / "gst.csv"
    stmt.write_text(_UNVERIFIABLE_GST_STATEMENT, encoding="utf-8")
    out_dir = tmp_path / "out"
    assert main([str(stmt), "-o", str(out_dir)]) == 2
    err = capsys.readouterr().err
    assert "unattributed GST -2.00 cannot be verified" in err
    assert "--accept-unattributed-gst" in err
    assert not out_dir.exists()  # nothing written on rejection

    assert main([str(stmt), "-o", str(out_dir), "--accept-unattributed-gst"]) == 0
    assert "accepted unverified" in capsys.readouterr().out
    rows = _read(out_dir / "AUD.csv")
    assert [
        "2026-06-30",
        "-2",
        "Interactive Brokers",
        "GST (not itemised in statement)",
        "GST",
        "",
    ] in rows


def test_cli_report_name_and_no_save_report(statement_csv, tmp_path):
    custom = tmp_path / "custom"
    assert main([str(statement_csv), "-o", str(custom), "-r", "run.txt"]) == 0
    assert (custom / "run.txt").exists()
    assert not (custom / "ibkr2xero_report.txt").exists()

    off = tmp_path / "off"
    assert main([str(statement_csv), "-o", str(off), "--no-save-report"]) == 0
    assert not (off / "ibkr2xero_report.txt").exists()
    assert sorted(p.name for p in off.iterdir()) == ["AUD.csv", "USD.csv"]


def test_cli_existing_report_blocks_without_force(statement_csv, tmp_path, capsys):
    report = tmp_path / "ibkr2xero_report.txt"
    report.write_text("old", encoding="utf-8")
    # All or nothing: a stale report blocks the run just like a stale CSV.
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 2
    assert "already exist" in capsys.readouterr().err
    assert not (tmp_path / "AUD.csv").exists()
    assert report.read_text(encoding="utf-8") == "old"

    assert main([str(statement_csv), "-o", str(tmp_path), "--force-overwrite"]) == 0
    assert "account U" in report.read_text(encoding="utf-8")


def test_cli_rejects_tampered_input(statement_csv, tmp_path, capsys):
    text = statement_csv.read_text(encoding="utf-8-sig")
    tampered = tmp_path / "tampered.csv"
    tampered.write_text(
        text.replace(
            "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.33",
            "Interest,Data,AUD,2026-06-03,AUD Debit Interest for May-2026,-1108.34",
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    assert main([str(tampered), "-o", str(out_dir)]) == 2
    assert not out_dir.exists()  # nothing written on rejection
    err = capsys.readouterr().err
    assert "input rejected" in err
    assert "no output files were written" in err
