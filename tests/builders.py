"""Small typed-model records for synthetic tests, with defaults for the dull fields."""

import csv
import datetime as dt
import io
from decimal import Decimal

from ibkr_to_xero.model import (
    AssetClass,
    CashKind,
    CashTxn,
    Statement,
    TaxItem,
    TaxKind,
    Trade,
)

JUN1 = dt.date(2026, 6, 1)
JUN30 = dt.date(2026, 6, 30)
WHEN = dt.datetime(2026, 6, 15, 10, 0, 0)


def _dec(value):
    return None if value is None else Decimal(value)


def _source(line_no: int) -> dict:
    return {"id": str(line_no), "where": f"line {line_no}", "report_date": None}


def trade(asset_class=AssetClass.STOCK, symbol="XYZ", quantity="1", price="5",
          proceeds="500", commission="-1", codes="", currency="USD", when=WHEN,
          commission_currency=None, line_no=1) -> Trade:
    return Trade(
        **_source(line_no),
        when=when,
        currency=currency,
        asset_class=asset_class,
        symbol=symbol,
        quantity=Decimal(quantity),
        price=_dec(price),
        proceeds=_dec(proceeds),
        commission=Decimal(commission),
        commission_currency=commission_currency or currency,
        codes=frozenset(codes.split(";")) if codes else frozenset(),
    )


def cash_txn(kind=CashKind.FEE, amount="-1", description="fee", currency="USD",
             date=dt.date(2026, 6, 10), line_no=1) -> CashTxn:
    return CashTxn(**_source(line_no), date=date, currency=currency, kind=kind,
                   description=description, amount=Decimal(amount))


def stamp_duty(symbol="XYZ", amount="-15.66", currency="USD", when=WHEN,
               line_no=9) -> TaxItem:
    return TaxItem(**_source(line_no), when=when, currency=currency,
                   kind=TaxKind.STAMP_DUTY, symbol=symbol, amount=Decimal(amount))


def tampered_csv(src, tmp_path, *edits):
    """Copy a real statement with one line changed per edit, quoting none of it.

    Each edit is (predicate, mutate) over a line's CSV cells: the first line the
    predicate accepts is replaced by mutate(cells). Tests stay free of the
    example's contents (real account data), which only ever live in examples/.
    """
    lines = src.read_text(encoding="utf-8-sig").splitlines()
    for predicate, mutate in edits:
        for i, line in enumerate(lines):
            cells = next(csv.reader([line]))
            if predicate(cells):
                out = io.StringIO()
                csv.writer(out, lineterminator="").writerow(mutate(cells))
                lines[i] = out.getvalue()
                break
        else:
            raise AssertionError("tampered_csv: no line matched an edit")
    path = tmp_path / "tampered.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def data_row(section, *leading):
    """Predicate: a Data row of `section` whose first cells are `leading`."""
    return lambda cells: cells[:2] == [section, "Data"] and cells[2:2 + len(leading)] == list(leading)


def add_to_cell(index, delta):
    """Mutation: add `delta` to the amount in cell `index` (negative = from the end)."""
    def mutate(cells):
        cells = list(cells)
        cells[index] = str(Decimal(cells[index].replace(",", "")) + Decimal(delta))
        return cells
    return mutate


def statement(cash_report=None, **records) -> Statement:
    """A June 2026 statement; cash_report maps currency -> [(component, amount)]."""
    return Statement(
        period_start=JUN1,
        period_end=JUN30,
        cash_report={
            ccy: {name: Decimal(total) for name, total in components}
            for ccy, components in (cash_report or {}).items()
        },
        **records,
    )
