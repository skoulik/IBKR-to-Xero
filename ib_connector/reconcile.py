"""Cross-check converted transactions against the statement's Cash Report.

The Cash Report is the source of truth: per currency it lists Starting Cash,
Ending Cash and the cash-flow components in between. Reconciliation enforces:

1. Cash Report internal consistency: Starting + sum(components) == Ending.
2. Every component maps to a section we converted, and the section's converted
   cash matches the component sum exactly (to EPS).
3. Unknown components with nonzero amounts => reject.
4. "Cash Settling MTM" (no per-transaction rows) becomes a tagged synthetic row.
5. After rounding to 2 dp, any residual within tolerance becomes a tagged
   ROUNDING row; beyond tolerance => reject.

All failures across all currencies are gathered into one ReconciliationError
so the user sees the complete picture; no output is written on failure.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .model import (
    CURRENCY_RE,
    CurrencyResult,
    OutputRow,
    ReconciliationError,
    Statement,
    StatementError,
    parse_money,
)

# Exact-arithmetic checks tolerate only representation noise, not real cash.
EPS = Decimal("0.000001")
CENT = Decimal("0.01")

MTM_COMPONENT = "Cash Settling MTM"
MTM_DESCRIPTION = "Cash Settling MTM (futures)"
ROUNDING_DESCRIPTION = "Rounding adjustment (2dp vs IB full precision)"

# Cash Report component -> statement section whose converted rows must sum to it.
_COMPONENT_SECTION = {
    "Trades (Sales)": "Trades",
    "Trades (Purchase)": "Trades",
    "Commissions": "Trades",  # commissions are embedded in each trade's Comm/Fee
    "GST": "Trades",  # GST on commissions is part of Comm/Fee too
    "Dividends": "Dividends",
    "Payment In Lieu of Dividends": "Dividends",
    "Withholding Tax": "Withholding Tax",
    "Broker Interest Paid and Received": "Interest",
    "Other Fees": "Fees",
    "Deposits": "Deposits & Withdrawals",
    "Withdrawals": "Deposits & Withdrawals",
    "Account Transfers": "Deposits & Withdrawals",
}

_SPECIAL_COMPONENTS = {"Starting Cash", "Ending Cash", "Ending Settled Cash", MTM_COMPONENT}


def _round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _cash_report(statement: Statement) -> dict[str, dict[str, Decimal]]:
    """Per-currency component amounts from the Cash Report section.

    The Cash Report is organised in blocks, each opened by a "Starting Cash"
    row: first the base-currency summary, then one block per actual currency.
    Rows must be bucketed by their *block*, not their own Currency cell — the
    base block can contain rows whose Currency cell names a real currency
    (e.g. GST charged in AUD shown converted to a base-USD line).
    """
    report: dict[str, dict[str, Decimal]] = {}
    rows = statement.rows("Cash Report")
    if not rows:
        raise StatementError("statement has no Cash Report section; cannot reconcile")
    block_currency: str | None = None
    for row in rows:
        component = row["Currency Summary"]
        if component == "Starting Cash":
            block_currency = row["Currency"]
            if CURRENCY_RE.match(block_currency) and block_currency in report:
                raise StatementError(
                    f"line {row.line_no}: duplicate Cash Report block for {block_currency}"
                )
        if block_currency is None:
            raise StatementError(
                f"line {row.line_no}: Cash Report row before any 'Starting Cash' block"
            )
        if not CURRENCY_RE.match(block_currency):
            continue  # base-currency summary block: out of scope
        amount = parse_money(row["Total"])
        per_ccy = report.setdefault(block_currency, {})
        if component in per_ccy:
            raise StatementError(
                f"line {row.line_no}: duplicate Cash Report component "
                f"{component!r} for {block_currency}"
            )
        per_ccy[component] = amount
    return report


def reconcile(
    statement: Statement, converted: dict[str, list[OutputRow]]
) -> list[CurrencyResult]:
    """Validate all currencies; return ready-to-write results or raise.

    Raises ReconciliationError describing every failed check in every
    currency. Currencies without cash activity yield no result (no file).
    """
    report = _cash_report(statement)
    errors: list[str] = []
    results: list[CurrencyResult] = []

    stray = sorted(set(converted) - set(report))
    if stray:
        errors.append(
            f"transactions found for currencies missing from Cash Report: {', '.join(stray)}"
        )

    for currency in sorted(report):
        components = dict(report[currency])
        rows = list(converted.get(currency, []))
        notes: list[str] = []

        try:
            starting = components.pop("Starting Cash")
            ending = components.pop("Ending Cash")
        except KeyError as exc:
            errors.append(f"{currency}: Cash Report is missing {exc.args[0]!r}")
            continue
        components.pop("Ending Settled Cash", None)  # settlement timing; not cash flow
        mtm = components.pop(MTM_COMPONENT, None)

        # (3) Unknown components: any real cash we don't understand => reject.
        for component, amount in sorted(components.items()):
            if component not in _COMPONENT_SECTION and amount != 0:
                errors.append(
                    f"{currency}: unmapped Cash Report component {component!r} "
                    f"with nonzero amount {amount}"
                )

        # (1) Cash Report internal consistency.
        component_sum = sum(components.values(), Decimal(0)) + (mtm or Decimal(0))
        if abs(starting + component_sum - ending) > EPS:
            errors.append(
                f"{currency}: Cash Report does not add up: starting {starting} "
                f"+ components {component_sum} != ending {ending} "
                f"(off by {starting + component_sum - ending})"
            )

        # (2) Component sums vs converted section sums.
        expected_by_section: dict[str, Decimal] = {}
        for component, amount in components.items():
            section = _COMPONENT_SECTION.get(component)
            if section:
                expected_by_section[section] = (
                    expected_by_section.get(section, Decimal(0)) + amount
                )
        actual_by_section: dict[str, Decimal] = {}
        for row in rows:
            actual_by_section[row.source_section] = (
                actual_by_section.get(row.source_section, Decimal(0)) + row.amount
            )
        for section in sorted(set(expected_by_section) | set(actual_by_section)):
            expected = expected_by_section.get(section, Decimal(0))
            actual = actual_by_section.get(section, Decimal(0))
            if abs(expected - actual) > EPS:
                errors.append(
                    f"{currency}: section {section!r} transactions sum to {actual} "
                    f"but Cash Report says {expected} (off by {actual - expected})"
                )

        # (4) Synthetic MTM row for futures cash settlement.
        if mtm is not None and mtm != 0:
            rows.append(
                OutputRow(
                    date=statement.period_end,
                    amount=mtm,
                    description=MTM_DESCRIPTION,
                    reference="MTM",
                    source_section="Cash Report",
                )
            )
            notes.append(f"synthetic MTM row for {MTM_COMPONENT}: {mtm}")

        if not rows:
            continue  # no cash activity in this currency: no file

        # (5) Rounding residual, in the 2dp world the accounting system lives in.
        rows = [
            OutputRow(
                date=r.date,
                amount=_round2(r.amount),
                description=r.description,
                reference=r.reference,
                source_section=r.source_section,
            )
            for r in rows
        ]
        rounded_sum = sum((r.amount for r in rows), Decimal(0))
        residual = _round2(ending) - _round2(starting) - rounded_sum
        tolerance = Decimal("0.005") * (len(rows) + 2)
        if abs(residual) > tolerance:
            errors.append(
                f"{currency}: rounding residual {residual} exceeds tolerance {tolerance} "
                f"(rounded transactions {rounded_sum} vs ending-starting "
                f"{_round2(ending) - _round2(starting)})"
            )
        elif residual != 0:
            rows.append(
                OutputRow(
                    date=statement.period_end,
                    amount=residual,
                    description=ROUNDING_DESCRIPTION,
                    reference="ROUNDING",
                    source_section="",
                )
            )
            notes.append(f"synthetic ROUNDING row: {residual}")

        results.append(
            CurrencyResult(
                currency=currency,
                rows=rows,
                starting_cash=starting,
                ending_cash=ending,
                notes=notes,
            )
        )

    if errors:
        lines = [f"input rejected: cash does not reconcile ({len(errors)} problem(s)):"]
        lines += [f"  - {e}" for e in errors]
        raise ReconciliationError("\n".join(lines))

    return results
