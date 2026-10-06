"""
site_urls.py - The only URLs the Grad Cafe scraper is allowed to request.

JHU EN.605.256 Modern Software Concepts in Python - Module 5 (split out of
scrape.py, Module 2).

Every URL the scraper fetches - the first listing page, each "Next" link read
from a page, and a URL saved in a checkpoint - goes through safe_site_url(),
which rebuilds it from validated parts: the scheme and host are fixed, only
the listing, robots.txt and result paths are allowed, and the pagination
cursor is decoded and re-encoded from its three typed fields.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

BASE_URL = "https://www.thegradcafe.com"
SURVEY_PATH = "/survey/"
ROBOTS_PATH = "/robots.txt"
ALLOWED_HOST = "www.thegradcafe.com"

RESULT_HREF_PATTERN = re.compile(r"^/result/(\d+)")
CURSOR_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _decode_cursor(cursor: str) -> object:
    """The JSON value inside a URL-safe base64 cursor (padding restored first)."""
    padded = cursor + "=" * (-len(cursor) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def safe_cursor(cursor: str) -> str:
    """Decode a pagination cursor, validate its three fields, and re-encode it.

    Grad Cafe's cursor is URL-safe base64 of {"created_at": "<timestamp>",
    "admitid": <int>, "_pointsToNextItems": <bool>}.  Only those typed values
    are carried over, so a damaged checkpoint or an odd link cannot smuggle
    anything else into the query string.  A valid cursor re-encodes to the
    identical string.
    """
    payload = _decode_cursor(cursor)
    stamp = datetime.strptime(str(payload["created_at"]), "%Y-%m-%d %H:%M:%S")
    created_at = (f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d} "
                  f"{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d}")
    admit_id = int(payload["admitid"])
    points_to_next = bool(payload.get("_pointsToNextItems", True))
    rebuilt = json.dumps(
        {"created_at": created_at, "admitid": admit_id, "_pointsToNextItems": points_to_next},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(rebuilt.encode("ascii")).decode("ascii").rstrip("=")


def describe_cursor(url: str) -> str:
    """Decode the base64 pagination cursor of *url* for readable log lines."""
    query = parse_qs(urlparse(url).query)
    cursor = query.get("cursor", [""])[0]
    if not cursor:
        return "start of listing"
    try:
        payload = _decode_cursor(cursor)
        return f"created_at<{payload.get('created_at')} id<{payload.get('admitid')}"
    except (ValueError, TypeError):
        return f"cursor={cursor[:16]}..."


def safe_site_url(url: str) -> str:
    """Rebuild *url* from validated parts, or raise ValueError.

    The scheme and host are always taken from BASE_URL (never from the input),
    only the listing, robots.txt and result paths are permitted, and the query
    string is rebuilt from a validated page number and cursor, so a link found
    on a page (or a damaged checkpoint) can never send the scraper to another
    server, another part of the site, or an arbitrary query.
    """
    parsed = urlparse(url)
    if parsed.netloc.lower() != ALLOWED_HOST:
        raise ValueError(f"refusing off-site URL {url}")
    # The path is re-created from a fixed table, never copied from the input.
    result_match = RESULT_HREF_PATTERN.match(parsed.path)
    if parsed.path == ROBOTS_PATH:
        path = ROBOTS_PATH
    elif parsed.path.rstrip("/") == SURVEY_PATH.rstrip("/"):
        path = SURVEY_PATH.rstrip("/")
    elif result_match:
        path = f"/result/{int(result_match.group(1))}"
    else:
        raise ValueError(f"refusing a path outside the public listing: {url}")
    query = parse_qs(parsed.query)
    clean_query: dict[str, object] = {}
    try:
        if "page" in query:
            clean_query["page"] = int(query["page"][0])
        if "cursor" in query:
            clean_query["cursor"] = safe_cursor(query["cursor"][0])
    except (ValueError, KeyError, TypeError) as err:
        raise ValueError(f"refusing a URL with an invalid query: {url}") from err
    base = urlparse(BASE_URL)
    return urlunparse((base.scheme, base.netloc, path, "", urlencode(clean_query), ""))
