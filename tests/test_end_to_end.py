"""End-to-end: CLI on the real example statement vs the reference outputs.

The hand-made reference files differ from ideal output in two known ways:
- two Fees descriptions carry a stray trailing '?' not present in the source
  statement (an artifact of how the reference was produced);
- USD.csv ends with manual reconciliation scratch rows (blank dates), which
  the tool replaces with tagged synthetic MTM/ROUNDING rows.
The comparison below normalises those away; everything else must be identical.
"""

import csv

from ib_connector.cli import main
from tests.conftest import EXAMPLES


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _reference_rows(name):
    rows = _read(EXAMPLES / name)
    # Drop manual scratch rows (no date) and the stray trailing '?'.
    return [
        [cell.rstrip("?") for cell in row]
        for row in rows
        if row[0].strip()
    ]


def test_cli_end_to_end(statement_csv, tmp_path, capsys):
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 0

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["AUD.csv", "USD.csv"]  # no HKD: no cash activity

    assert _read(tmp_path / "AUD.csv") == _reference_rows("AUD.csv")

    usd = _read(tmp_path / "USD.csv")
    synthetic = [row for row in usd if row[4] in ("MTM", "ROUNDING")]
    real = [row for row in usd if row[4] not in ("MTM", "ROUNDING")]
    assert real == _reference_rows("USD.csv")
    assert [row[4] for row in synthetic] == ["MTM", "ROUNDING"]
    assert synthetic[0][1] == "2575"

    out = capsys.readouterr().out
    assert "account U" in out


def test_cli_skip_zero(statement_csv, tmp_path, capsys):
    default_dir = tmp_path / "default"
    skip_dir = tmp_path / "skip"
    assert main([str(statement_csv), "-o", str(default_dir)]) == 0
    assert main([str(statement_csv), "-o", str(skip_dir), "--skip-zero"]) == 0

    for name in ("AUD.csv", "USD.csv"):
        default_rows = _read(default_dir / name)
        skip_rows = _read(skip_dir / name)
        assert [r for r in default_rows if r[1] == "0"], f"{name}: fixture lost its zero rows"
        assert not [r for r in skip_rows if r[1] == "0"]
        # Skipping zero rows must change nothing else, including the sum.
        assert skip_rows == [r for r in default_rows if r[1] != "0"]

    assert "skipped" in capsys.readouterr().out


def test_cli_default_output_is_statement_folder(statement_csv, tmp_path):
    stmt = tmp_path / statement_csv.name
    stmt.write_bytes(statement_csv.read_bytes())
    assert main([str(stmt)]) == 0
    assert (tmp_path / "AUD.csv").exists()
    assert (tmp_path / "USD.csv").exists()


def test_cli_refuses_to_overwrite_without_force(statement_csv, tmp_path, capsys):
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 0
    before = (tmp_path / "AUD.csv").read_bytes()
    # Second run must abort entirely and change nothing.
    assert main([str(statement_csv), "-o", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "already exist" in err
    assert "--force" in err
    assert (tmp_path / "AUD.csv").read_bytes() == before
    # --force allows the rewrite.
    assert main([str(statement_csv), "-o", str(tmp_path), "--force"]) == 0


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
