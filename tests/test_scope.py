"""Question -> company scoping (gold/scope.py)."""

import pytest

from filingsage.gold.scope import detect_ticker

COMPANIES = [
    ("AAPL", "Apple Inc."), ("AMZN", "AMAZON COM INC"), ("GOOGL", "Alphabet Inc."),
    ("JPM", "JPMORGAN CHASE & CO"), ("META", "Meta Platforms, Inc."), ("V", "VISA INC."),
    ("UNH", "UNITEDHEALTH GROUP INC"),
]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Okay tell me abt amazon, how are they doing this quarter", "AMZN"),
        ("What are Apple's main competitive risks?", "AAPL"),
        ("How did AAPL's margins change?", "AAPL"),
        ("Any lawsuits at JPMorgan?", "JPM"),
        ("What does Google say about AI spending?", "GOOGL"),
        ("Is facebook still investing in VR?", "META"),
        ("$V revenue growth", "V"),
        ("UnitedHealth medical cost trends", "UNH"),
    ],
)
def test_names_and_tickers_resolve_to_one_company(question, expected):
    assert detect_ticker(question, COMPANIES) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Compare Apple and Amazon on margins",  # two companies: don't guess
        "Which companies mention tariffs?",       # none named
        "What is a 10-K?",
        "Is v the right letter here?",            # lower-case single letter isn't a ticker
    ],
)
def test_ambiguous_or_unnamed_questions_stay_unscoped(question):
    assert detect_ticker(question, COMPANIES) is None


def test_known_limitation_a_company_name_that_is_also_a_common_word():
    """'visa' is Visa's name, so a travel question gets scoped to Visa.
    String matching can't see context; the LangGraph planner (L9) can."""
    assert detect_ticker("Do I need a visa for travel?", COMPANIES) == "V"


def test_untracked_companies_are_never_returned():
    assert detect_ticker("What about Google?", [("AAPL", "Apple Inc.")]) is None
    assert detect_ticker("How is TSLA doing?", COMPANIES) is None
