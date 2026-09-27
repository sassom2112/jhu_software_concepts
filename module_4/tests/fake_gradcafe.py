"""
fake_gradcafe.py - A stand-in for www.thegradcafe.com, so scraper tests never touch the internet.

Not a test file itself (the name does not start with test_); the scraper
tests import from it.  It provides:

* make_cursor() / survey_url(): the cursor-paginated listing URLs, spelled
  exactly the way the scraper requests them.
* applicant() / listing_page(): a survey page with the same structure the
  real site serves -- a results table, badge and comment rows under each
  applicant, a "Next" link, and the JSON copy of the rows in <div id="app">.
* page() / http_error() / FakeSite: canned responses.  FakeSite.urlopen
  replaces urllib.request.urlopen (see the fake_site fixture in conftest.py)
  and records every URL requested and every sleep the scraper asked for.
"""

from __future__ import annotations

import base64
import html
import io
import json
import urllib.error
from urllib.parse import urlencode

SITE = "https://www.thegradcafe.com"
ROBOTS_URL = f"{SITE}/robots.txt"

# The parts of the real robots.txt that matter: allow-all for "*", a block for
# a named bot, and a second "*" group that disallows the account pages.
ROBOTS_TXT = """\
User-agent: *
Allow: /

User-agent: GPTBot
Disallow: /

User-agent: *
Disallow: /signin
Disallow: /profile
Sitemap: https://www.thegradcafe.com/sitemap.xml
"""


def make_cursor(created_at: str, admit_id: int, points_to_next: bool = True) -> str:
    """Grad Café's pagination cursor: URL-safe base64 of a small JSON object, without padding."""
    payload = json.dumps({"created_at": created_at, "admitid": admit_id, "_pointsToNextItems": points_to_next},
                         separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def survey_url(cursor: str | None = None) -> str:
    """A listing URL exactly as the scraper requests it."""
    query = {"page": 1, "cursor": cursor} if cursor else {"page": 1}
    return f"{SITE}/survey?{urlencode(query)}"


def applicant(result_id: int, **fields) -> dict:
    """The visible values of one listing row; override any of them by keyword."""
    row = {
        "id": result_id,
        "school": "Johns Hopkins University",
        "program": "Computer Science",
        "degree": "Masters",                  # None: a program cell with no <span>s
        "added": "Sep 20, 2026",
        "decision": "Accepted on Sep 18",
        "tags": ["Fall 2027", "International", "GPA 3.90"],
        "comment": None,
    }
    row.update(fields)
    return row


def _applicant_rows(a: dict) -> str:
    if a["degree"] is None:
        program_cell = f"<td>{a['program']}</td>"
    else:
        program_cell = f"<td><div><span>{a['program']}</span><svg></svg><span>{a['degree']}</span></div></td>"
    rows = (f"<tr><td><div>{a['school']}</div></td>{program_cell}<td>{a['added']}</td>"
            f"<td><div class='tw-inline-flex'>{a['decision']}</div></td>"
            f"<td><a href='/result/{a['id']}'>comments</a></td></tr>")
    badges = f"<div class='tw-inline-flex md:tw-hidden'>{a['decision']}</div>"        # mobile-only duplicate
    badges += "".join(f"<div class='tw-inline-flex'>{tag}</div>" for tag in a["tags"])
    rows += f"<tr class='tw-border-none'><td colspan='100%'>{badges}</td></tr>"
    if a["comment"]:
        rows += f"<tr class='tw-border-none'><td colspan='100%'><p>{a['comment']}</p></td></tr>"
    return rows + "<tr><td colspan='100%'>advertisement</td></tr>"                    # the site's ad rows


def listing_page(applicants: list[dict], next_url: str | None = None, payload: str | None = "auto") -> str:
    """One survey page.

    payload="auto" embeds a JSON record for every applicant, as the real site
    does; None leaves the JSON out; any other string is used verbatim.
    """
    if payload == "auto":
        records = [{"id": a["id"], "school": a["school"], "date_of_notification": "2026-09-18T00:00:00.000000Z"}
                   for a in applicants]
        payload = json.dumps({"props": {"results": {"data": records}}})
    app_div = f'<div id="app" data-page="{html.escape(payload)}"></div>' if payload is not None else ""
    next_link = f'<a href="{html.escape(next_url)}">Next</a>' if next_url else ""
    nav = f'<nav aria-label="Results pagination"><a href="{SITE}/survey?page=1">Previous</a>{next_link}</nav>'
    header = "<tr><th>School</th><th>Program</th><th>Added On</th><th>Decision</th><th></th></tr>"
    body = "".join(_applicant_rows(a) for a in applicants)
    return f"<html><body>{app_div}<table>{header}{body}</table>{nav}</body></html>"


def page(body: str, headers: dict | None = None) -> tuple[str, dict]:
    """A 200 response."""
    return body, headers or {}


def http_error(code: int, body: str = "", headers: dict | None = None) -> urllib.error.HTTPError:
    """An HTTP error response; urlopen raises these, just like the real one."""
    return urllib.error.HTTPError(SITE, code, f"HTTP {code}", headers or {}, io.BytesIO(body.encode("utf-8")))


class FakeResponse:
    """What urlopen() returns for a 200: a body to read() and a headers mapping."""

    def __init__(self, body: str, headers: dict) -> None:
        self._body = body.encode("utf-8")
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeSite:
    """Answers urlopen() from canned responses; any URL not served is a 404.

    serve(url, *responses) queues responses for one URL; they are used in
    order and the last one repeats.  A response is page(...) or an exception
    instance to raise (http_error(503), URLError(...), KeyboardInterrupt()...).
    """

    def __init__(self) -> None:
        self.responses: dict[str, list] = {}
        self.requested: list[str] = []
        self.user_agents: list[str] = []
        self.sleeps: list[float] = []

    def serve(self, url: str, *responses) -> None:
        self.responses[url] = list(responses)

    def serve_listing(self, pages: list[list[int]]) -> list[str]:
        """Serve robots.txt and a chain of listing pages joined by "Next" links.

        pages[n] is the list of result ids shown on page n (newest first).
        Returns the page URLs in order.
        """
        self.serve(ROBOTS_URL, page(ROBOTS_TXT))
        urls = [survey_url()] + [survey_url(make_cursor("2026-09-01 12:00:00", 1000 - n)) for n in range(1, len(pages))]
        for n, ids in enumerate(pages):
            next_url = urls[n + 1] if n + 1 < len(pages) else None
            self.serve(urls[n], page(listing_page([applicant(i) for i in ids], next_url=next_url)))
        return urls

    def urlopen(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        self.user_agents.append(request.get_header("User-agent"))
        queue = self.responses.get(url) or [http_error(404)]
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, BaseException):
            raise response
        body, headers = response
        return FakeResponse(body, headers)