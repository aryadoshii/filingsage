"""XBRL companyfacts -> clean financial series (financials/xbrl.py).

The fixture is a synthetic company with an Apple-style fiscal year (ends late
September, 13-week quarters) and round numbers so every expected value can be
checked by hand. It deliberately contains the messy cases real XBRL has:
re-reported comparatives, a restatement, Q4 only inside the annual figure,
year-to-date-only cash flows, a tag switch between years, and an 8-K fact.
"""

from __future__ import annotations

from datetime import date

from filingsage.financials.xbrl import extract_facts

# Fiscal 2025: 29 Sep 2024 -> 27 Sep 2025. Quarter ends: 28 Dec, 29 Mar, 28 Jun.
FY_START, Q1_END, Q2_END, Q3_END, FY_END = (
    "2024-09-29", "2024-12-28", "2025-03-29", "2025-06-28", "2025-09-27",
)


def fact(start, end, val, filed, form="10-Q", accn="acc"):
    entry = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        entry["start"] = start
    return entry


def companyfacts(**concepts) -> dict:
    return {"facts": {"us-gaap": {
        name: {"units": {unit: entries}} for name, (unit, entries) in concepts.items()
    }}}


REVENUE = ("USD", [
    fact(FY_START, Q1_END, 100, "2025-01-30"),
    fact(FY_START, Q1_END, 104, "2025-05-01"),  # restated in a later filing: wins
    fact("2024-12-29", Q2_END, 110, "2025-05-01"),
    fact(FY_START, Q2_END, 214, "2025-05-01"),  # 6-month YTD
    fact("2025-03-30", Q3_END, 120, "2025-07-31"),
    fact(FY_START, Q3_END, 334, "2025-07-31"),  # 9-month YTD
    fact(FY_START, FY_END, 470, "2025-10-31", form="10-K"),
    fact("2025-06-29", FY_END, 999, "2025-10-30", form="8-K"),  # not a reporting form
])

# Cash flow: 10-Qs only ever report year-to-date.
OCF = ("USD", [
    fact(FY_START, Q1_END, 30, "2025-01-30"),
    fact(FY_START, Q2_END, 70, "2025-05-01"),
    fact(FY_START, Q3_END, 100, "2025-07-31"),
    fact(FY_START, FY_END, 150, "2025-10-31", form="10-K"),
])

EPS = ("USD/shares", [
    fact(FY_START, Q1_END, 1.5, "2025-01-30"),
    fact("2024-12-29", Q2_END, 1.6, "2025-05-01"),
    fact("2025-03-30", Q3_END, 1.7, "2025-07-31"),
    fact(FY_START, FY_END, 6.4, "2025-10-31", form="10-K"),
])

CASH = ("USD", [
    fact(None, Q3_END, 50, "2025-07-31"),
    fact(None, FY_END, 60, "2025-10-31", form="10-K"),
    fact(None, FY_END, 61, "2025-12-01", form="10-K/A"),  # amended: later filing wins
])


def by_key(facts, metric, period):
    return {f.end: f for f in facts if f.metric == metric and f.period == period}


def test_reported_quarters_and_annual_are_read_directly():
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE))

    quarters = by_key(facts, "revenue", "quarter")
    annual = by_key(facts, "revenue", "annual")
    assert quarters[date(2025, 3, 29)].value == 110
    assert not quarters[date(2025, 3, 29)].derived
    assert annual[date(2025, 9, 27)].value == 470


def test_the_most_recently_filed_value_wins_for_the_same_period():
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE))
    assert by_key(facts, "revenue", "quarter")[date(2024, 12, 28)].value == 104


def test_q4_is_derived_from_the_annual_minus_nine_months_and_flagged():
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE))

    q4 = by_key(facts, "revenue", "quarter")[date(2025, 9, 27)]
    assert q4.value == 470 - 334
    assert q4.derived
    assert q4.start == date(2025, 6, 29)


def test_facts_from_non_reporting_forms_are_ignored():
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE))
    assert all(f.value != 999 for f in facts)


def test_year_to_date_cash_flows_become_quarters():
    facts = extract_facts(companyfacts(NetCashProvidedByUsedInOperatingActivities=OCF))

    quarters = by_key(facts, "operating_cash_flow", "quarter")
    assert [quarters[d].value for d in sorted(quarters)] == [30, 40, 30, 50]
    first = quarters[date(2024, 12, 28)]
    assert not first.derived  # Q1 year-to-date IS the quarter
    assert all(quarters[d].derived for d in sorted(quarters)[1:])


def test_a_gap_in_the_year_to_date_series_stops_derivation():
    gappy = ("USD", [OCF[1][0], OCF[1][2], OCF[1][3]])  # no 6-month figure
    facts = extract_facts(companyfacts(NetCashProvidedByUsedInOperatingActivities=gappy))

    quarters = by_key(facts, "operating_cash_flow", "quarter")
    assert sorted(quarters) == [date(2024, 12, 28)]  # can't safely derive past the gap


def test_eps_quarters_are_never_derived():
    facts = extract_facts(companyfacts(EarningsPerShareDiluted=EPS))

    quarters = by_key(facts, "eps_diluted", "quarter")
    assert date(2025, 9, 27) not in quarters  # no Q4: EPS isn't additive
    assert by_key(facts, "eps_diluted", "annual")[date(2025, 9, 27)].value == 6.4


def test_balance_sheet_values_take_the_latest_filing_per_date():
    facts = extract_facts(companyfacts(CashAndCashEquivalentsAtCarryingValue=CASH))

    cash = by_key(facts, "cash", "instant")
    assert cash[date(2025, 9, 27)].value == 61
    assert cash[date(2025, 9, 27)].start is None


def test_older_periods_are_filled_from_a_previous_tag():
    """ASC 606 moved companies from SalesRevenueNet to RevenueFromContract...
    in 2018 — the history should continue across the switch."""
    old = ("USD", [fact("2023-10-01", "2024-09-28", 400, "2024-11-01", form="10-K")])
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE,
        SalesRevenueNet=old,
    ))

    annual = by_key(facts, "revenue", "annual")
    assert annual[date(2024, 9, 28)].value == 400
    assert annual[date(2024, 9, 28)].concept == "SalesRevenueNet"
    assert annual[date(2025, 9, 27)].concept.startswith("RevenueFromContract")


def test_missing_metrics_simply_produce_no_facts():
    assert extract_facts({"facts": {}}) == []
    assert extract_facts({}) == []


# --- report: statement table + key stats (financials/report.py) ---------------

from filingsage.financials.report import key_stats, statement  # noqa: E402


def _full_year_facts():
    return extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=REVENUE,
        NetIncomeLoss=("USD", [
            fact(FY_START, Q1_END, 26, "2025-05-01"),
            fact("2024-12-29", Q2_END, 22, "2025-05-01"),
            fact("2025-03-30", Q3_END, 24, "2025-07-31"),
            fact(FY_START, Q3_END, 72, "2025-07-31"),
            fact(FY_START, FY_END, 100, "2025-10-31", form="10-K"),
        ]),
        NetCashProvidedByUsedInOperatingActivities=OCF,
        PaymentsToAcquirePropertyPlantAndEquipment=("USD", [
            fact(FY_START, Q1_END, 5, "2025-01-30"),
            fact(FY_START, Q2_END, 10, "2025-05-01"),
            fact(FY_START, Q3_END, 15, "2025-07-31"),
            fact(FY_START, FY_END, 20, "2025-10-31", form="10-K"),
        ]),
        CashAndCashEquivalentsAtCarryingValue=CASH,
        EarningsPerShareDiluted=EPS,
    ))


def test_quarterly_statement_has_margins_and_attaches_balance_sheet():
    cols = statement(_full_year_facts(), period="quarter", limit=8)

    assert [c.end for c in cols] == [
        date(2024, 12, 28), date(2025, 3, 29), date(2025, 6, 28), date(2025, 9, 27),
    ]
    q4 = cols[-1]
    assert q4.values["revenue"] == 136
    assert q4.values["net_income"] == 28
    assert q4.values["net_margin"] == 28 / 136
    assert q4.values["free_cash_flow"] == 50 - 5
    assert q4.values["cash"] == 61
    assert set(q4.derived) >= {"revenue", "net_income"}
    assert cols[2].values["cash"] == 50
    assert cols[0].values["cash"] is None  # no balance sheet on that date: None, not a guess


def test_growth_needs_a_year_ago_period():
    cols = statement(_full_year_facts(), period="quarter")
    assert all(c.values["revenue_growth"] is None for c in cols)


def test_key_stats_use_trailing_four_quarters():
    stats = {s.key: s for s in key_stats(_full_year_facts())}

    assert stats["revenue_ttm"].value == 104 + 110 + 120 + 136
    assert stats["revenue_ttm"].as_of == date(2025, 9, 27)
    assert stats["net_income_ttm"].value == 100
    assert stats["net_margin_ttm"].value == 100 / 470
    assert stats["free_cash_flow_ttm"].value == 150 - 20
    assert stats["eps_diluted_fy"].value == 6.4
    assert stats["cash"].value == 61
    assert stats["long_term_debt"].value is None
    assert stats["revenue_growth_yoy"].value is None


def test_trailing_sum_refuses_non_contiguous_quarters():
    facts = [f for f in _full_year_facts()
             if not (f.metric == "revenue" and f.end == date(2025, 3, 29))]
    stats = {s.key: s for s in key_stats(facts)}
    assert stats["revenue_ttm"].value is None


def test_growth_compares_with_the_same_quarter_a_year_earlier():
    prior_q4 = fact("2024-06-30", "2024-09-28", 100, "2024-11-01", form="10-K")
    revenue = ("USD", [*REVENUE[1], prior_q4])
    facts = extract_facts(companyfacts(
        RevenueFromContractWithCustomerExcludingAssessedTax=revenue))

    cols = statement(facts, period="quarter")
    assert cols[-1].values["revenue_growth"] == (136 - 100) / 100
    stats = {s.key: s for s in key_stats(facts)}
    assert stats["revenue_growth_yoy"].value == (136 - 100) / 100
