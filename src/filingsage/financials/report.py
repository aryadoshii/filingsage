"""Stored financial facts -> what a reader wants: a statement table (quarterly
or annual columns, with margins and growth) and headline key stats.

Pure functions over plain rows, so the API and tests share one
implementation and no number is computed in two places. Every ratio is
computed only when both inputs exist for the same period — a missing input
yields None, never a guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Protocol

FLOW_METRICS = (
    "revenue", "gross_profit", "operating_income", "net_income",
    "operating_cash_flow", "capex",
)
INSTANT_METRICS = ("cash", "total_assets", "total_liabilities", "equity", "long_term_debt")
PER_SHARE_METRICS = ("eps_diluted",)

# A balance-sheet date and a period end are "the same date" within this
# tolerance (some filers tag the balance sheet a day off the period end).
SAME_DATE = timedelta(days=7)
# "A year earlier", for growth: 52/53-week fiscal years drift by days.
YEAR_AGO = (timedelta(days=350), timedelta(days=380))


class FactLike(Protocol):
    metric: str
    period: str
    start: date | None
    end: date
    value: float
    derived: bool


@dataclass
class Column:
    start: date | None
    end: date
    values: dict[str, float | None] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def _near(target: date, candidates: Iterable[date], tolerance: timedelta) -> date | None:
    best = min(candidates, key=lambda d: abs(d - target), default=None)
    return best if best is not None and abs(best - target) <= tolerance else None


def _year_ago(end: date, ends: Iterable[date]) -> date | None:
    lo, hi = YEAR_AGO
    matches = [d for d in ends if lo <= end - d <= hi]
    return min(matches, key=lambda d: abs((end - d) - timedelta(days=365)), default=None)


def statement(
    facts: Iterable[FactLike], *, period: Literal["quarter", "annual"], limit: int = 8
) -> list[Column]:
    """The newest `limit` periods, oldest first (charts read left to right).

    Columns are defined by revenue periods when revenue exists, otherwise by
    net income — a company with no revenue tag (some banks) still gets a
    table. Balance-sheet values attach to the column whose end they match.
    """
    facts = list(facts)
    flows: dict[str, dict[date, FactLike]] = {}
    instants: dict[str, dict[date, FactLike]] = {}
    for f in facts:
        if f.period == period:
            flows.setdefault(f.metric, {})[f.end] = f
        elif f.period == "instant":
            instants.setdefault(f.metric, {})[f.end] = f

    anchor = flows.get("revenue") or flows.get("net_income") or {}
    all_ends = sorted(anchor)
    columns: list[Column] = []
    for end in all_ends[-limit:]:
        col = Column(start=anchor[end].start, end=end)
        for metric in (*FLOW_METRICS, *PER_SHARE_METRICS):
            f = flows.get(metric, {}).get(end)
            col.values[metric] = f.value if f else None
            if f and f.derived:
                col.derived.append(metric)
        for metric in INSTANT_METRICS:
            series = instants.get(metric, {})
            match = _near(end, series, SAME_DATE)
            col.values[metric] = series[match].value if match else None

        v = col.values
        v["gross_margin"] = _ratio(v["gross_profit"], v["revenue"])
        v["operating_margin"] = _ratio(v["operating_income"], v["revenue"])
        v["net_margin"] = _ratio(v["net_income"], v["revenue"])
        v["free_cash_flow"] = (
            v["operating_cash_flow"] - v["capex"]
            if v["operating_cash_flow"] is not None and v["capex"] is not None
            else None
        )
        prior = _year_ago(end, all_ends)
        prior_rev = flows.get("revenue", {}).get(prior) if prior else None
        v["revenue_growth"] = (
            _ratio(v["revenue"] - prior_rev.value, prior_rev.value)
            if prior_rev and v["revenue"] is not None
            else None
        )
        columns.append(col)
    return columns


@dataclass(frozen=True)
class KeyStat:
    key: str
    value: float | None
    as_of: date | None


def _trailing_four(series: dict[date, FactLike]) -> tuple[float, date] | None:
    """Sum of the latest four quarters, only if they're back-to-back."""
    ends = sorted(series)[-4:]
    if len(ends) < 4:
        return None
    quarters = [series[e] for e in ends]
    for prev, cur in zip(quarters, quarters[1:], strict=False):
        if cur.start is None or abs((cur.start - prev.end).days - 1) > 3:
            return None
    return sum(q.value for q in quarters), ends[-1]


def key_stats(facts: Iterable[FactLike]) -> list[KeyStat]:
    facts = list(facts)
    quarters: dict[str, dict[date, FactLike]] = {}
    annuals: dict[str, dict[date, FactLike]] = {}
    instants: dict[str, dict[date, FactLike]] = {}
    for f in facts:
        target = {"quarter": quarters, "annual": annuals, "instant": instants}.get(f.period)
        if target is not None:
            target.setdefault(f.metric, {})[f.end] = f

    def ttm(metric: str) -> tuple[float, date] | None:
        return _trailing_four(quarters.get(metric, {}))

    def latest(bucket: dict[str, dict[date, FactLike]], metric: str) -> KeyStat:
        series = bucket.get(metric, {})
        if not series:
            return KeyStat(metric, None, None)
        end = max(series)
        return KeyStat(metric, series[end].value, end)

    stats: list[KeyStat] = []
    rev, ni = ttm("revenue"), ttm("net_income")
    stats.append(KeyStat("revenue_ttm", rev[0] if rev else None, rev[1] if rev else None))
    stats.append(KeyStat("net_income_ttm", ni[0] if ni else None, ni[1] if ni else None))
    stats.append(KeyStat(
        "net_margin_ttm",
        _ratio(ni[0], rev[0]) if rev and ni and rev[1] == ni[1] else None,
        rev[1] if rev else None,
    ))

    rev_q = quarters.get("revenue", {})
    growth, growth_end = None, None
    if rev_q:
        growth_end = max(rev_q)
        prior = _year_ago(growth_end, rev_q)
        if prior:
            growth = _ratio(rev_q[growth_end].value - rev_q[prior].value, rev_q[prior].value)
    stats.append(KeyStat("revenue_growth_yoy", growth, growth_end))

    ocf, capex = ttm("operating_cash_flow"), ttm("capex")
    stats.append(KeyStat(
        "free_cash_flow_ttm",
        ocf[0] - capex[0] if ocf and capex and ocf[1] == capex[1] else None,
        ocf[1] if ocf else None,
    ))
    eps = latest(annuals, "eps_diluted")
    stats.append(KeyStat("eps_diluted_fy", eps.value, eps.as_of))
    for metric in ("cash", "long_term_debt"):
        stats.append(latest(instants, metric))
    return stats
