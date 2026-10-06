"""
test_scrape_parsing.py - Everything the scraper decides without the network.

URL safety (the scraper may only ever request the public listing on the real
host), the pagination cursor, the robots.txt rules, and turning a listing
page's HTML into raw entries.  Pages come from fake_gradcafe.listing_page(),
which reproduces the structure of the real survey page.
"""

from __future__ import annotations

import base64
import json
import logging
import sys

import pytest

from worker.etl import scrape
from fake_gradcafe import ROBOTS_TXT, SITE, applicant, listing_page, make_cursor, survey_url
from worker.etl.scrape import GradCafeScraper, _clean_text, _looks_like_robots_file, _robots_allows, _safe_site_url

pytestmark = pytest.mark.db

CURSOR = make_cursor("2026-08-28 16:54:05", 1020462)
AGENT = scrape.PRODUCT_TOKEN


# --------------------------------------------------------------------------- #
#                 URL safety: only the public listing, only this host         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{SITE}/survey/?page=1", f"{SITE}/survey?page=1"),
        ("http://WWW.THEGRADCAFE.COM/survey", f"{SITE}/survey"),               # always https, lower-case host
        (f"{SITE}/result/0042", f"{SITE}/result/42"),
        (f"{SITE}/robots.txt", f"{SITE}/robots.txt"),
        (f"{SITE}/survey?page=1&cursor={CURSOR}&sort=newest", survey_url(CURSOR)),   # unknown parameters dropped
    ],
)
def test_safe_site_url_rebuilds_allowed_urls(url, expected):
    assert _safe_site_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/survey",                                         # another server
        f"{SITE}/signin",                                                      # outside the public listing
        f"{SITE}/survey?page=two",                                             # page must be a number
        f"{SITE}/survey?cursor=not-a-cursor",                                  # cursor must decode
        f"{SITE}/survey?cursor={make_cursor('yesterday', 1)}",                 # its timestamp must parse
    ],
)
def test_safe_site_url_refuses_everything_else(url):
    with pytest.raises(ValueError):
        _safe_site_url(url)


def test_cursor_is_rebuilt_from_its_three_typed_fields():
    tampered = json.dumps({"created_at": "2026-08-28 16:54:05", "admitid": "1020462", "extra": "<script>"})
    tampered_cursor = base64.urlsafe_b64encode(tampered.encode()).decode()

    assert _safe_site_url(survey_url(tampered_cursor)) == survey_url(CURSOR)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (survey_url(), "start of listing"),
        (survey_url(CURSOR), "created_at<2026-08-28 16:54:05 id<1020462"),
        (f"{SITE}/survey?cursor=bm90LWpzb24", "cursor=bm90LWpzb24..."),        # base64 of "not-json"
    ],
)
def test_describe_cursor_for_log_lines(url, expected):
    assert GradCafeScraper._describe_cursor(url) == expected


# --------------------------------------------------------------------------- #
#                          robots.txt (RFC 9309 rules)                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("robots_text", "agent", "path", "allowed"),
    [
        (ROBOTS_TXT, AGENT, "/survey?page=1", True),
        (ROBOTS_TXT, AGENT, "/signin", False),                                 # from the second "*" group
        (ROBOTS_TXT, "GPTBot", "/survey", False),                              # a group naming the bot beats "*"
        ("User-agent: *\nDisallow: /survey\nAllow: /survey?page=1", AGENT, "/survey?page=1", True),  # longest wins
        ("User-agent: *\nDisallow: /a\nAllow: /a", AGENT, "/a", True),        # a tie goes to Allow
        ("User-agent: *\nDisallow: /*.pdf$", AGENT, "/files/x.pdf", False),    # * and $ wildcards
        ("User-agent: *\nDisallow: /*.pdf$", AGENT, "/files/x.pdf?download=1", True),
        ("User-agent: *\nDisallow:   # nothing\n", AGENT, "/survey", True),    # an empty Disallow allows all
        ("", AGENT, "/survey", True),                                          # no rules at all
    ],
)
def test_robots_rules(robots_text, agent, path, allowed):
    assert _robots_allows(robots_text, agent, SITE + path) is allowed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ROBOTS_TXT, True),
        ("", True),                                                            # an empty file allows everything
        ("<!DOCTYPE html><html><body>Sign in</body></html>", False),
        ("<html><body>Blocked: unrecognised User-Agent</body></html>", False),  # HTML, even if it says User-Agent
        ("hello", False),
    ],
)
def test_looks_like_robots_file(text, expected):
    assert _looks_like_robots_file(text) is expected


# --------------------------------------------------------------------------- #
#                         Listing HTML -> raw entries                         #
# --------------------------------------------------------------------------- #

def test_parse_page_reads_every_visible_field(scraper):
    html = listing_page(
        [applicant(1002, comment="Funded offer"), applicant(1001, program="Physics", degree=None, tags=[])],
        next_url=survey_url(CURSOR),
    )

    entries, next_url = scraper._parse_page(html, survey_url(), scraped_at="2026-09-20T12:00:00+00:00")

    assert next_url == survey_url(CURSOR)
    first, second = entries
    assert first == {
        "result_id": 1002,
        "url": f"{SITE}/result/1002",
        "school_text": "Johns Hopkins University",
        "program_text": "Computer Science",
        "degree_text": "Masters",
        "date_added_text": "Sep 20, 2026",
        "decision_text": "Accepted on Sep 18",
        "tags_text": ["Fall 2027", "International", "GPA 3.90"],             # mobile-only duplicate skipped
        "comment_text": "Funded offer",
        "listing_json": {"id": 1002, "school": "Johns Hopkins University",
                         "date_of_notification": "2026-09-18T00:00:00.000000Z"},
        "source_page_url": survey_url(),
        "scraped_at": "2026-09-20T12:00:00+00:00",
    }
    assert (second["program_text"], second["degree_text"], second["tags_text"]) == ("Physics", None, [])


def test_scraped_at_defaults_to_the_current_utc_time(scraper):
    entries, _ = scraper._parse_page(listing_page([applicant(1001)]), survey_url())

    assert entries[0]["scraped_at"].endswith("+00:00")


def test_a_page_without_the_json_copy_still_parses(scraper, caplog):
    with caplog.at_level(logging.WARNING, logger="gradcafe.scrape"):
        entries, _ = scraper._parse_page(listing_page([applicant(1001)], payload=None), survey_url())

    assert entries[0]["listing_json"] is None
    assert caplog.records == []                                                # nothing worth a warning


@pytest.mark.parametrize(
    ("payload", "warning"),
    [
        ("{not json", "Listing JSON payload missing or malformed"),
        ('{"props": {}}', "Listing JSON payload missing or malformed"),
        ('{"props": {"results": {"data": [{"id": 999}, "junk"]}}}', "Listing JSON ids differ"),
    ],
)
def test_a_broken_json_copy_falls_back_to_the_html(scraper, caplog, payload, warning):
    with caplog.at_level(logging.WARNING, logger="gradcafe.scrape"):
        entries, _ = scraper._parse_page(listing_page([applicant(1001)], payload=payload), survey_url())

    assert entries[0]["listing_json"] is None
    assert entries[0]["school_text"] == "Johns Hopkins University"            # the HTML still parsed
    assert warning in caplog.text


def test_next_link_is_found_even_without_the_pagination_nav(scraper):
    html = f"<html><body><a href='{survey_url(CURSOR)}'>Next page</a></body></html>"

    assert scraper._parse_page(html, survey_url()) == ([], survey_url(CURSOR))


def test_an_off_site_next_link_is_ignored(scraper, caplog):
    html = listing_page([applicant(1001)], next_url="https://evil.example/survey?page=2")

    with caplog.at_level(logging.WARNING, logger="gradcafe.scrape"):
        _, next_url = scraper._parse_page(html, survey_url())

    assert next_url is None
    assert "Ignoring next link" in caplog.text


def test_the_last_page_has_no_next_link(scraper):
    _, next_url = scraper._parse_page(listing_page([applicant(1001)]), survey_url())

    assert next_url is None


# --------------------------------------------------------------------------- #
#                                Small helpers                                #
# --------------------------------------------------------------------------- #

def test_clean_text_of_a_missing_tag_is_none():
    assert _clean_text(None) is None


def test_falls_back_to_html_parser_without_lxml(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "lxml", None)                            # makes "import lxml" fail

    assert GradCafeScraper(data_dir=tmp_path)._parser == "html.parser"