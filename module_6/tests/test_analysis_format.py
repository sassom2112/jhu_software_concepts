"""
tests/test_analysis_format.py

Covers: every rendered analysis item is labeled "Answer:", every percentage on
the page has exactly two decimals, and the formatter functions that produce
those strings (analysis_common.py) round the way we claim they do.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from worker.etl.analysis_common import format_average, format_count, format_difference, format_percent, format_table
from web.app import create_app

# Mixes plain result lines with a table card, the way the real page does (Q10 and Q11 are tables).
MIXED_ANALYSIS = {
    "summary": {"total_entries": 30503, "newest_entry": None},
    "answers": [
        {
            "number": "2",
            "question": "Among entries that provide a nationality classification, what percentage are international students?",
            "lines": [("Percent international", "46.35%"), ("International entries", "13,885")],
            "columns": [],
            "table": [],
        },
        {
            "number": "3",
            "question": "What are the average GPA, GRE Quantitative, GRE Verbal, and GRE Analytical Writing scores?",
            "lines": [("Average GPA", "3.77 (n = 18,287)")],
            "columns": [],
            "table": [],
        },
        {
            "number": "10",
            "question": "Original question: How do acceptance rates compare across degree types?",
            "lines": [],
            "columns": ["Degree", "Entries", "Acceptances", "Acceptance %", "Avg GPA (accepted)"],
            "table": [["PhD", "21,396", "5,569", "26.03%", "3.81"], ["Masters", "7,515", "5,128", "68.24%", "3.72"]],
        },
    ],
}

# A number (commas allowed, optional decimals) immediately followed by a percent sign.
PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")
TWO_DECIMALS = re.compile(r"^\d[\d,]*\.\d{2}$")


def percentages_in(html: str) -> list[str]:
    """Every number shown as a percentage in the page's visible text (scripts and styles removed)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return PERCENT.findall(soup.get_text(" "))


@pytest.fixture
def mixed_client():
    """This app only ever serves GET /analysis, so the scraper and loader are harmless stubs."""
    app = create_app(scrape_fn=lambda: [], load_fn=lambda rows: 0, query_fn=lambda: MIXED_ANALYSIS)
    return app.test_client()


# ---------------------------------------------------------------------------
# The rendered page
# ---------------------------------------------------------------------------

@pytest.mark.analysis
def test_every_result_line_is_labeled_answer(mixed_client):
    soup = BeautifulSoup(mixed_client.get("/analysis").get_data(as_text=True), "html.parser")
    results = soup.select(".result")
    assert len(results) == 3  # two lines in Q2, one in Q3
    for result in results:
        label = result.select_one(".answer-label")
        assert label is not None and label.get_text(strip=True) == "Answer:"


@pytest.mark.analysis
def test_every_analysis_card_is_labeled_answer(mixed_client):
    """Consistency: table cards (Q10, Q11) need an Answer: label too, not just the line-based ones."""
    soup = BeautifulSoup(mixed_client.get("/analysis").get_data(as_text=True), "html.parser")
    cards = soup.select("article.card")
    assert len(cards) == 3
    for card in cards:
        number = card.select_one(".card-number").get_text(strip=True)
        assert card.select_one(".answer-label") is not None, f"{number} has no Answer: label"


@pytest.mark.analysis
def test_every_percentage_on_the_page_has_two_decimals(mixed_client):
    found = percentages_in(mixed_client.get("/analysis").get_data(as_text=True))
    assert found, "expected at least one percentage on the page"
    wrong = [value for value in found if not TWO_DECIMALS.match(value)]
    assert wrong == [], f"percentages without exactly two decimals: {wrong}"


@pytest.mark.analysis
def test_the_percentage_check_itself_catches_wrong_precision():
    """Test the test: the checker must flag 46.4% and 50%, and accept 46.35%."""
    found = percentages_in("<p>46.4% then 50% then 46.35%</p>")
    assert found == ["46.4", "50", "46.35"]
    assert [value for value in found if not TWO_DECIMALS.match(value)] == ["46.4", "50"]


# ---------------------------------------------------------------------------
# The formatter functions (analysis_common.py)
# ---------------------------------------------------------------------------

@pytest.mark.analysis
@pytest.mark.parametrize(
    "value, expected",
    [
        (46.353, "46.35%"),
        (39.285, "39.29%"),  # rounds half-up, like PostgreSQL's ROUND on numeric
        (50, "50.00%"),
        ("7.1", "7.10%"),
        (Decimal("0.005"), "0.01%"),
        (None, "N/A"),
    ],
)
def test_format_percent_always_shows_two_decimals(value, expected):
    assert format_percent(value) == expected


@pytest.mark.analysis
@pytest.mark.parametrize("value, expected", [(3.7712, "3.77"), (4, "4.00"), (None, "N/A")])
def test_format_average_shows_two_decimals(value, expected):
    assert format_average(value) == expected


@pytest.mark.analysis
@pytest.mark.parametrize("value, expected", [(30066, "30,066"), (0, "0"), (None, "0")])
def test_format_count_is_a_whole_number_with_separators(value, expected):
    assert format_count(value) == expected


@pytest.mark.analysis
@pytest.mark.parametrize("value, expected", [(3, "+3"), (-2, "-2"), (0, "0"), (None, "0")])
def test_format_difference_is_signed(value, expected):
    assert format_difference(value) == expected


@pytest.mark.analysis
def test_format_table_left_aligns_the_first_column_and_right_aligns_the_rest():
    lines = format_table(["Degree", "Entries"], [["PhD", "21,396"], ["MFA", "602"]])
    assert lines == [
        "Degree  Entries",
        "------  -------",
        "PhD      21,396",
        "MFA         602",
    ]