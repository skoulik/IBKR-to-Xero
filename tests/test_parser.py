import datetime as dt
import re

import pytest

from ib_connector import StatementError, parse_statement


def test_metadata(statement):
    assert re.fullmatch(r"U\d+", statement.account)
    assert statement.period_start == dt.date(2026, 6, 1)
    assert statement.period_end == dt.date(2026, 6, 30)


def test_bom_stripped(statement):
    # The first section's name carries a UTF-8 BOM in the raw file.
    assert "Statement" in statement.sections


def test_sections_parsed(statement):
    for section in (
        "Trades",
        "Cash Report",
        "Deposits & Withdrawals",
        "Fees",
        "Dividends",
        "Withholding Tax",
        "Interest",
    ):
        assert statement.rows(section), f"section {section} should have rows"


def test_running_headers_per_section(statement):
    # Trades re-emits a Header per asset category; both must be parsed.
    categories = {row["Asset Category"] for row in statement.rows("Trades")}
    assert categories == {"Stocks", "Equity and Index Options"}


def test_aggregate_row_kinds_skipped(statement):
    # SubTotal/Total/Notes rows never become data rows.
    discriminators = {row["DataDiscriminator"] for row in statement.rows("Trades")}
    assert discriminators == {"Order"}


def test_unknown_row_kind_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "Statement,Header,Field Name,Field Value\n"
        "Statement,Data,Period,\"June 1, 2026 - June 30, 2026\"\n"
        "Trades,Mystery,foo\n",
        encoding="utf-8",
    )
    with pytest.raises(StatementError, match="unknown row kind 'Mystery'"):
        parse_statement(bad)


def test_data_before_header_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Trades,Data,Order,Stocks\n", encoding="utf-8")
    with pytest.raises(StatementError, match="before any Header"):
        parse_statement(bad)
