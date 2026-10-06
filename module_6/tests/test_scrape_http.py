"""
test_scrape_http.py - robots.txt, HTTP errors, retries and the "Pull Data" scrape.

Every test here uses the fake_site fixture: urllib's urlopen() is answered by
fake_gradcafe.FakeSite, so the scraper runs its real code against canned
responses -- 403s, Cloudflare challenge pages, dropped connections -- that
would be impossible (and impolite) to provoke on the real site.  time.sleep()
is recorded instead of slept, so the tests also check the scraper's waits
without spending a single second waiting.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

import scrape
from fake_gradcafe import ROBOTS_TXT, ROBOTS_URL, SITE, applicant, http_error, listing_page, page, survey_url

pytestmark = pytest.mark.db

CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"


class UnreadableBody(io.BytesIO):
    """An error page whose body cannot be read (the connection dropped mid-response)."""

    def read(self, *args):
        raise OSError("connection reset while reading the error page")


def result_ids(entries):
    return [entry["result_id"] for entry in entries]


# --------------------------------------------------------------------------- #
#                                  robots.txt                                 #
# --------------------------------------------------------------------------- #

def test_check_robots_allows_the_listing_and_keeps_a_copy(fake_site, scraper, tmp_path):
    fake_site.serve(ROBOTS_URL, page(ROBOTS_TXT))

    assert scraper.check_robots() is True
    assert (tmp_path / "robots.txt").read_text(encoding="utf-8") == ROBOTS_TXT
    assert fake_site.user_agents == [scrape.USER_AGENT]                      # our honest agent, not Python's
    assert fake_site.timeouts == [scrape.REQUEST_TIMEOUT_SECONDS]            # never wait forever


@pytest.mark.parametrize(
    ("crawl_delay", "delay_seconds"),
    [("10", 10.0), ("1", 2.0)],                                             # only ever slower, never faster
    ids=["longer-is-honoured", "shorter-is-ignored"],
)
def test_check_robots_crawl_delay(fake_site, scraper, crawl_delay, delay_seconds):
    fake_site.serve(ROBOTS_URL, page(f"User-agent: *\nCrawl-delay: {crawl_delay}\nAllow: /\n"))

    assert scraper.check_robots() is True
    assert scraper.delay_seconds == delay_seconds


@pytest.mark.parametrize(
    ("response", "allowed"),
    [
        (http_error(404), True),                                             # no robots.txt: no restrictions
        (page("<html><body>Down for maintenance</body></html>"), False),      # not a robots file: stop
        (page("User-agent: *\nDisallow: /survey\n"), False),                 # the listing is disallowed
        (page("User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /survey\n"), False),   # only RFC 9309 sees it
        (page("User-agent: *\nDisallow: /survey\nAllow: /survey/\n"), False),              # only urllib sees it
    ],
    ids=["missing", "html-instead", "disallowed", "rfc-only-disallows", "urllib-only-disallows"],
)
def test_check_robots_decision(fake_site, scraper, response, allowed):
    fake_site.serve(ROBOTS_URL, response)

    assert scraper.check_robots() is allowed


def test_check_robots_passes_other_http_errors_up(fake_site, scraper):
    fake_site.serve(ROBOTS_URL, http_error(400))

    with pytest.raises(urllib.error.HTTPError):
        scraper.check_robots()


def test_a_disallowed_url_is_never_requested(fake_site, scraper):
    fake_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /result/\n"))
    scraper.check_robots()

    with pytest.raises(PermissionError):
        scraper._http_get(f"{SITE}/result/1001")

    assert fake_site.requested == [ROBOTS_URL]


# --------------------------------------------------------------------------- #
#                          One request: _http_get()                           #
# --------------------------------------------------------------------------- #

def test_a_normal_page_comes_back_as_text(fake_site, scraper):
    fake_site.serve(survey_url(), page("<html>listing</html>"))

    assert scraper._http_get(f"{SITE}/survey/?page=1") == "<html>listing</html>"
    assert fake_site.requested == [survey_url()]                             # normalised before the request


@pytest.mark.parametrize(
    ("response", "retryable", "message"),
    [
        (http_error(403, headers={"cf-mitigated": "challenge"}), False, "Cloudflare mitigation 'challenge'"),
        (http_error(429), False, "HTTP 429 Too Many Requests"),
        (http_error(503, body=CHALLENGE), False, "(challenge page)"),
        (http_error(500), True, "HTTP 500 Internal Server Error"),
        (urllib.error.HTTPError(SITE, 502, "Bad Gateway", {}, UnreadableBody()), True, "HTTP 502"),
        (page("<html>ok</html>", headers={"cf-mitigated": "challenge"}), False, "mitigation 'challenge' applied"),
        (page(CHALLENGE), False, "challenge page returned"),
    ],
    ids=["403-cloudflare", "429", "503-challenge", "500", "502-unreadable", "200-mitigated", "200-challenge"],
)
def test_blocks_and_server_errors_raise_scrape_blocked_error(fake_site, scraper, response, retryable, message):
    fake_site.serve(survey_url(), response)

    with pytest.raises(scrape.ScrapeBlockedError) as blocked:
        scraper._http_get(survey_url())

    assert blocked.value.retryable is retryable                              # only a plain 5xx is retried
    assert message in str(blocked.value)


def test_permanent_http_errors_pass_through_untouched(fake_site, scraper):
    fake_site.serve(survey_url(), http_error(404))

    with pytest.raises(urllib.error.HTTPError) as error:
        scraper._http_get(survey_url())

    assert error.value.code == 404


# --------------------------------------------------------------------------- #
#                      Retries: _fetch_page() waits, slowly                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("responses", "waits"),
    [
        ([http_error(500), page("ok")], [30]),                                             # one retry after a 5xx
        ([urllib.error.URLError("reset"), TimeoutError("timed out"), page("ok")], [30, 120]),  # two after network errors
    ],
    ids=["server-error", "network-errors"],
)
def test_transient_failures_are_retried_slowly(fake_site, scraper, responses, waits):
    fake_site.serve(survey_url(), *responses)

    assert scraper._fetch_page(survey_url()) == "ok"
    assert fake_site.sleeps == waits


@pytest.mark.parametrize(
    ("responses", "error", "waits"),
    [
        ([http_error(500)], scrape.ScrapeBlockedError, [30]),                # still failing after its one retry
        ([urllib.error.URLError("down")], scrape.ScrapeNetworkError, [30, 120]),
        ([http_error(403)], scrape.ScrapeBlockedError, []),                  # a block is never retried
        ([http_error(404)], urllib.error.HTTPError, []),                     # nor is a missing page
    ],
    ids=["5xx-twice", "network-keeps-failing", "403", "404"],
)
def test_failures_that_are_not_retried_again(fake_site, scraper, responses, error, waits):
    fake_site.serve(survey_url(), *responses)

    with pytest.raises(error):
        scraper._fetch_page(survey_url())

    assert fake_site.sleeps == waits


# --------------------------------------------------------------------------- #
#                 Pull Data: scrape_new_entries() stops early                 #
# --------------------------------------------------------------------------- #

def test_pull_stops_at_the_first_page_with_a_stored_entry(fake_site, scraper):
    urls = fake_site.serve_listing([[105, 104], [103, 102], [101, 100]])
    progress = []

    entries, pages, resume_url = scraper.scrape_new_entries({102, 101}, progress=lambda *p: progress.append(p))

    assert result_ids(entries) == [105, 104, 103]
    assert (pages, resume_url) == (2, None)
    assert urls[2] not in fake_site.requested                                 # older pages are never fetched
    assert progress == [(1, 2), (2, 3)]
    assert fake_site.sleeps == [2.0]                                          # one polite pause between pages


def test_pull_hands_back_where_to_resume_when_the_page_limit_is_hit(fake_site, scraper):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])

    entries, pages, resume_url = scraper.scrape_new_entries(set(), max_pages=1)

    assert (result_ids(entries), pages, resume_url) == ([105, 104], 1, urls[1])
    assert fake_site.sleeps == []                                             # no pause after the last page


def test_pull_can_start_from_a_saved_resume_url(fake_site, scraper):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])

    entries, _, _ = scraper.scrape_new_entries(set(), start_url=urls[1])

    assert result_ids(entries) == [103, 102]
    assert survey_url() not in fake_site.requested


def test_pull_stops_at_an_empty_page(fake_site, scraper):
    urls = fake_site.serve_listing([[105], [], [104]])

    entries, pages, _ = scraper.scrape_new_entries(set())

    assert (result_ids(entries), pages) == ([105], 2)
    assert urls[2] not in fake_site.requested


def test_pull_stops_when_next_points_back_at_the_same_page(fake_site, scraper):
    fake_site.serve_listing([[105]])
    fake_site.serve(survey_url(), page(listing_page([applicant(105)], next_url=survey_url())))

    entries, pages, _ = scraper.scrape_new_entries(set())

    assert (result_ids(entries), pages) == ([105], 1)                        # fetched once, not in a loop


def test_pull_refuses_when_robots_disallows(fake_site, scraper):
    fake_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /\n"))

    with pytest.raises(PermissionError):
        scraper.scrape_new_entries(set())

    assert fake_site.requested == [ROBOTS_URL]