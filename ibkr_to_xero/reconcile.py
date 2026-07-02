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
GST_DESCRIPTION = "GST (not itemised in statement)"

# Cash Report component -> statement section whose converted rows must sum to it.
_COMPONENT_SECTION = {
    "Trades (Sales)": "Trades",
    "Trades (Purchase)": "Trades",
    "Commissions": "Trades",  # commissions are embedded in each trade's Comm/Fee
    "GST": "Trades",  # GST on commissions is part of Comm/Fee (see envelope check)
    "Transaction Fees": "Trades",  # stamp duty etc. is embedded in trade Comm/Fee too
    "Dividends": "Dividends",
    "Payment In Lieu of Dividends": "Dividends",
    "Withholding Tax": "Withholding Tax",
    "Broker Interest Paid and Received": "Interest",
    "Bond Interest Paid and Received": "Interest",  # coupons + accrued interest rows
    "Other Fees": "Fees",
    "Deposits": "Deposits & Withdrawals",
    "Withdrawals": "Deposits & Withdrawals",
    "Account Transfers": "Deposits & Withdrawals",
}

# Converted rows are grouped into check buckets by source section; corporate
# action cash flows through the Trades (Sales)/(Purchase) components.
_SECTION_BUCKET = {"Corporate Actions": "Trades"}

# Non-cash informational lines of the Cash Report (Stock Yield Enhancement
# Program collateral tracking). They sit outside the Starting->Ending cash
# flow and are excluded from all sums regardless of value.
_INFORMATIONAL_COMPONENTS = {
    "Ending Settled Cash",
    "Starting Collateral Value",
    "Net Securities Lent Activity",
    "Ending Collateral Value",
    "Net Cash Balance",
    "Net Settled Cash Balance",
}


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

    txfees_by_currency: dict[str, Decimal] = {}
    for row in statement.rows("Transaction Fees"):
        fee_currency = row.values.get("Currency", "")
        if CURRENCY_RE.match(fee_currency):
            txfees_by_currency[fee_currency] = txfees_by_currency.get(
                fee_currency, Decimal(0)
            ) + parse_money(row["Amount"])

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
        for informational in _INFORMATIONAL_COMPONENTS:
            components.pop(informational, None)
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

        # (2) Component sums vs converted section sums. The GST component
        # needs an envelope check: the part levied on commissions is embedded
        # in trade Comm/Fee (so it is already inside the trade rows), but the
        # part levied on account fees appears in no dated row anywhere in the
        # statement. That unattributed gap must stay within the GST component
        # (same sign, not larger), and is emitted as a tagged synthetic row.
        expected_by_section: dict[str, Decimal] = {}
        for component, amount in components.items():
            section = _COMPONENT_SECTION.get(component)
            if section:
                expected_by_section[section] = (
                    expected_by_section.get(section, Decimal(0)) + amount
                )
        actual_by_section: dict[str, Decimal] = {}
        for row in rows:
            bucket = _SECTION_BUCKET.get(row.source_section, row.source_section)
            actual_by_section[bucket] = actual_by_section.get(bucket, Decimal(0)) + row.amount
        gst = components.get("GST", Decimal(0))
        gst_gap = Decimal(0)
        for section in sorted(set(expected_by_section) | set(actual_by_section)):
            expected = expected_by_section.get(section, Decimal(0))
            actual = actual_by_section.get(section, Decimal(0))
            gap = expected - actual
            if abs(gap) <= EPS:
                continue
            if (
                section == "Trades"
                and gst != 0
                and (gap < 0) == (gst < 0)
                and abs(gap) <= abs(gst) + EPS
            ):
                gst_gap = gap
                continue
            errors.append(
                f"{currency}: section {section!r} transactions sum to {actual} "
                f"but Cash Report says {expected} (off by {actual - expected})"
            )

        # (2b) Transaction Fees (stamp duty etc.) are embedded in trade
        # Comm/Fee; the section is a breakdown, not extra cash. Its rows must
        # still sum to the component, or something is off.
        txfees_section = txfees_by_currency.get(currency, Decimal(0))
        txfees_component = components.get("Transaction Fees", Decimal(0))
        if abs(txfees_section - txfees_component) > EPS:
            errors.append(
                f"{currency}: Transaction Fees section rows sum to {txfees_section} "
                f"but Cash Report says {txfees_component}"
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

        # (4b) Synthetic row for GST charged on account fees: real cash with
        # no per-item rows in the statement. TODO(TODO.md M3): itemise it if
        # IB ever exposes a GST details section in this export format.
        if gst_gap != 0:
            rows.append(
                OutputRow(
                    date=statement.period_end,
                    amount=gst_gap,
                    description=GST_DESCRIPTION,
                    reference="GST",
                    source_section="",
                )
            )
            # Display quantized to cents: IB truncates Cash Report components
            # at ~9 decimals, so raw embedded/unattributed figures can carry
            # nanocent dust that is just their display rounding, not cash.
            notes.append(
                f"synthetic GST row (GST component {gst}, embedded in trades "
                f"{_round2(gst - gst_gap)}, unattributed {_round2(gst_gap)})"
            )

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
