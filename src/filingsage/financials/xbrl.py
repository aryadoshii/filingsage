"""SEC XBRL companyfacts -> clean quarterly / annual / balance-sheet series.

Pure functions, no I/O: input is the companyfacts JSON document, output is a
list of Fact rows ready for the financial_facts table. Everything that makes
raw XBRL awkward is handled here, in one place:

1. **Concept choice.** The same line item has different tags across
   companies and years (revenue alone: RevenueFromContractWithCustomer...,
   Revenues, SalesRevenueNet...). Each metric lists candidates; the one with
   the most recent data leads, and older periods are filled in from the
   others — companies switched tags when ASC 606 landed in 2018.

2. **Duplicates.** A fact is re-reported in later filings as a comparative
   (last year's Q2 appears again in this year's Q2 10-Q). Facts are keyed by
   their exact period and the most recently filed value wins, so a restated
   number replaces the original.

3. **Missing quarters.** Companies report Q4 only inside the annual 10-K,
   and cash-flow statements in 10-Qs are year-to-date (6 and 9 months), not
   three months. Quarters are therefore derived from the cumulative
   year-to-date series: Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M. Every
   derived value is flagged (Fact.derived) so the UI can say so. Per-share
   figures are never derived — EPS isn't additive, since the share count
   changes between quarters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

# Forms whose facts describe the company's own reported periods. Excludes
# e.g. 8-K exhibits and registration statements, which can carry pro forma
# or partial-period figures.
REPORTING_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT"})

# Period-length windows, in days. A fiscal quarter runs 84-98 days in
# practice (13-week quarters, 52/53-week years); anything in these bands is
# treated as a quarter / half / nine months / year.
QUARTER_DAYS = range(80, 101)
YEAR_DAYS = range(350, 381)

Kind = Literal["flow", "instant", "per_share"]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    kind: Kind
    unit: str
    concepts: tuple[str, ...]  # in priority order (us-gaap taxonomy)


METRICS: dict[str, MetricSpec] = {
    "revenue": MetricSpec("flow", "USD", (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "RevenuesNetOfInterestExpense",
    )),
    "gross_profit": MetricSpec("flow", "USD", ("GrossProfit",)),
    "operating_income": MetricSpec("flow", "USD", ("OperatingIncomeLoss",)),
    "net_income": MetricSpec("flow", "USD", ("NetIncomeLoss", "ProfitLoss")),
    "eps_diluted": MetricSpec("per_share", "USD/shares", ("EarningsPerShareDiluted",)),
    "operating_cash_flow": MetricSpec("flow", "USD", (
        "NetCashProvidedByUsedInOperatingActivities",
    )),
    "capex": MetricSpec("flow", "USD", (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    )),
    "cash": MetricSpec("instant", "USD", (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    )),
    "total_assets": MetricSpec("instant", "USD", ("Assets",)),
    "total_liabilities": MetricSpec("instant", "USD", ("Liabilities",)),
    "equity": MetricSpec("instant", "USD", (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    )),
    "long_term_debt": MetricSpec("instant", "USD", ("LongTermDebtNoncurrent", "LongTermDebt")),
}


@dataclass(frozen=True, slots=True)
class Fact:
    metric: str
    period: Literal["quarter", "annual", "instant"]
    start: date | None
    end: date
    value: float
    unit: str
    derived: bool
    concept: str
    accession_no: str | None
    filed: date | None


@dataclass(frozen=True, slots=True)
class _Raw:
    start: date | None
    end: date
    value: float
    accession_no: str | None
    filed: date | None
    concept: str


def _days(start: date, end: date) -> int:
    return (end - start).days + 1


def _raw_facts(facts: dict, concept: str, unit: str) -> list[_Raw]:
    entries = facts.get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit, [])
    out = []
    for entry in entries:
        if entry.get("form") not in REPORTING_FORMS or "val" not in entry or "end" not in entry:
            continue
        out.append(
            _Raw(
                start=date.fromisoformat(entry["start"]) if entry.get("start") else None,
                end=date.fromisoformat(entry["end"]),
                value=float(entry["val"]),
                accession_no=entry.get("accn"),
                filed=date.fromisoformat(entry["filed"]) if entry.get("filed") else None,
                concept=concept,
            )
        )
    return out


def _latest_per_period(raws: list[_Raw]) -> dict[tuple[date | None, date], _Raw]:
    """One value per exact (start, end) period: the most recently filed."""
    best: dict[tuple[date | None, date], _Raw] = {}
    for raw in raws:
        key = (raw.start, raw.end)
        current = best.get(key)
        if current is None or (raw.filed or date.min) >= (current.filed or date.min):
            best[key] = raw
    return best


def _merged_periods(facts: dict, spec: MetricSpec) -> dict[tuple[date | None, date], _Raw]:
    """Lead with the candidate concept that has the most recent data, then
    fill any periods it lacks from the others (older tags, renamed lines)."""
    per_concept = {
        concept: _latest_per_period(_raw_facts(facts, concept, spec.unit))
        for concept in spec.concepts
    }
    ranked = sorted(
        (c for c in spec.concepts if per_concept[c]),
        key=lambda c: (max(end for _, end in per_concept[c]), len(per_concept[c])),
        reverse=True,
    )
    merged: dict[tuple[date | None, date], _Raw] = {}
    for concept in ranked:
        for key, raw in per_concept[concept].items():
            merged.setdefault(key, raw)
    return merged


def _fact(metric: str, spec: MetricSpec, period, raw: _Raw, *, start=None, value=None,
          derived=False) -> Fact:
    return Fact(
        metric=metric,
        period=period,
        start=raw.start if start is None and period != "instant" else start,
        end=raw.end,
        value=raw.value if value is None else value,
        unit=spec.unit,
        derived=derived,
        concept=raw.concept,
        accession_no=raw.accession_no,
        filed=raw.filed,
    )


def _duration_facts(metric: str, spec: MetricSpec, periods: dict) -> list[Fact]:
    durations = [r for (start, _), r in periods.items() if start is not None]
    annuals = [r for r in durations if _days(r.start, r.end) in YEAR_DAYS]
    quarters = {r.end: r for r in durations if _days(r.start, r.end) in QUARTER_DAYS}

    out = [_fact(metric, spec, "annual", r) for r in annuals]
    out += [_fact(metric, spec, "quarter", r) for r in quarters.values()]
    if spec.kind == "per_share":
        return out  # never derive: EPS isn't additive across quarters

    # Derive missing quarters by walking each fiscal year quarter by quarter,
    # keeping a running year-to-date total. Each step uses a directly
    # reported quarter if there is one, otherwise the next year-to-date
    # figure (YTD minus the running total = that quarter). Q4 always comes
    # from the annual figure, since the annual IS the 12-month YTD.
    for year in annuals:
        ytd_by_end = {
            r.end: r for r in durations
            if r.start == year.start and r.end <= year.end
        }
        prev_end, running = year.start - timedelta(days=1), 0.0
        while prev_end < year.end:
            next_start = prev_end + timedelta(days=1)
            # Only genuinely reported 3-month facts count as "direct" — the
            # quarters dict is never mutated, so a YTD figure can't be
            # mistaken for a quarter here.
            direct = next(
                (q for q in quarters.values() if abs((q.start - next_start).days) <= 3),
                None,
            )
            ytd = next(
                (r for end, r in ytd_by_end.items() if _days(next_start, end) in QUARTER_DAYS),
                None,
            )
            if direct is not None:
                end = direct.end
                same_end_ytd = ytd_by_end.get(end)
                running = same_end_ytd.value if same_end_ytd else running + direct.value
            elif ytd is not None:
                end = ytd.end
                out.append(
                    _fact(metric, spec, "quarter", ytd, start=next_start,
                          value=ytd.value - running, derived=ytd.start != next_start)
                )
                running = ytd.value
            else:
                break  # neither a quarter nor a YTD figure: can't derive past the gap
            prev_end = end
    return out


def extract_facts(companyfacts: dict) -> list[Fact]:
    """All metrics in METRICS, as clean quarterly/annual/instant facts."""
    facts = companyfacts.get("facts", {})
    out: list[Fact] = []
    for metric, spec in METRICS.items():
        periods = _merged_periods(facts, spec)
        if spec.kind == "instant":
            by_end: dict[date, _Raw] = {}
            for (start, end), raw in periods.items():
                if start is None:
                    current = by_end.get(end)
                    if current is None or (raw.filed or date.min) >= (current.filed or date.min):
                        by_end[end] = raw
            out += [_fact(metric, spec, "instant", r) for r in by_end.values()]
        else:
            out += _duration_facts(metric, spec, periods)
    return sorted(out, key=lambda f: (f.metric, f.period, f.end))
