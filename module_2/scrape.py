"""
scrape.py - Grad Cafe admissions-results scraper.

JHU EN.605.256 Modern Software Concepts in Python - Module 2.

Workflow (urllib-only; no browser automation was needed for this site):

  1. Fetch https://www.thegradcafe.com/robots.txt with urllib and confirm with
     urllib.robotparser (plus an RFC 9309 longest-match check) that the survey
     listing is allowed for our user agent.
  2. Build the first listing URL with urllib.parse and fetch it with
     urllib.request.
  3. Parse the server-rendered HTML with BeautifulSoup into "raw" entry dicts
     (every value is the visible text exactly as shown on the listing row),
     and keep the page's own JSON record of each entry next to it.
  4. Follow the page's "Next" link (Grad Cafe uses cursor-based pagination, so
     the next URL is read from the page rather than computed) until the target
     number of entries has been collected, sleeping between requests.
  5. Persist progress after every page (JSONL + checkpoint + a copy of the
     HTML) so an interrupted run picks up where it left off, then write the
     raw entries to JSON and hand them to clean.py.

The scraper never tries to get around a block: a 401/403/429 response or a
Cloudflare challenge page stops the run immediately; a 5xx or a network error
is retried only a couple of times, slowly.  Re-running the script later
resumes from the saved checkpoint.

Public API used by clean.py and the instructor's tooling:
    GradCafeScraper.scrape_data()  -> list[dict]
    save_data(entries, path)       -> None
    load_data(path)                -> list[dict]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

# --------------------------------------------------------------------------- #
#                                Constants                                    #
# --------------------------------------------------------------------------- #

BASE_URL = "https://www.thegradcafe.com"
SURVEY_PATH = "/survey/"
ROBOTS_PATH = "/robots.txt"
ALLOWED_HOST = "www.thegradcafe.com"

# Honest, descriptive user agent.  Grad Cafe (via Cloudflare) answers 403 to
# Python's default "Python-urllib/3.x" agent but serves this one normally.
PRODUCT_TOKEN = "JHU-EN605256-GradCafeScraper"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN}/1.0; "
    "student coursework; contact msasso1@jh.edu)"
)

DEFAULT_TARGET_ENTRIES = 30_000
DEFAULT_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 45
SERVER_ERROR_RETRY_WAIT_SECONDS = 30      # one retry after a plain 5xx response
NETWORK_RETRY_WAITS_SECONDS = (30, 120)   # two retries after timeouts / connection errors
SAVE_JSON_EVERY_N_PAGES = 25
MAX_STALE_PAGES = 3                       # consecutive pages with no new entry -> stop

# Strings that only appear on a Cloudflare interstitial, never on a real page,
# and the response header Cloudflare sets on any challenged or blocked reply.
CHALLENGE_MARKERS = ("<title>Just a moment...</title>", "cf-chl-bypass", "challenge-error-text")
CLOUDFLARE_MITIGATION_HEADER = "cf-mitigated"

RESULT_HREF_PATTERN = re.compile(r"^/result/(\d+)")
CONTINUATION_ROW_CLASS = "tw-border-none"  # tag row / comment row under a main row

# The listing is an Inertia.js page: <div id="app" data-page="{...}"> carries a
# JSON copy of the 20 rendered entries (with full ISO decision dates).
LISTING_JSON_ELEMENT_ID = "app"
LISTING_JSON_ATTRIBUTE = "data-page"

logger = logging.getLogger("gradcafe.scrape")


class ScrapeBlockedError(RuntimeError):
    """The site blocked, rate-limited, challenged, or failed a request.

    ``retryable`` is True only for a plain 5xx server error (retried once);
    401/403/429 and challenge pages are never retried.
    """

    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ScrapeNetworkError(RuntimeError):
    """The network kept failing (timeouts / connection errors); resume later."""


class ScrapeStateError(RuntimeError):
    """Local progress files are inconsistent; the user has to decide what to do."""


# --------------------------------------------------------------------------- #
#       JSON helpers (module level so they can be imported from clean.py)     #
# --------------------------------------------------------------------------- #


def save_data(entries: list[dict], path: str | Path) -> None:
    """Write a list of entry dicts to *path* as pretty-printed UTF-8 JSON.

    A path ending in ".gz" is written gzip-compressed (same JSON inside).  The
    file is written to a temporary name first and then renamed, so a crash can
    never leave a half-written JSON file behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(temp_path, "wt", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def load_data(path: str | Path) -> list[dict]:
    """Read a JSON list of entry dicts from *path* (plain or ".gz")."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path} does not contain a JSON list")
    return data


# --------------------------------------------------------------------------- #
#                               Scraper                                       #
# --------------------------------------------------------------------------- #


class GradCafeScraper:
    """Collects raw admissions entries from the Grad Cafe survey listing."""

    def __init__(
        self,
        target_entries: int = DEFAULT_TARGET_ENTRIES,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        data_dir: str | Path = "data",
        max_pages: int | None = None,
        cache_html: bool = True,
    ) -> None:
        self.target_entries = target_entries
        self.delay_seconds = delay_seconds
        self.max_pages = max_pages
        self.cache_html = cache_html

        self.data_dir = Path(data_dir)
        self.html_cache_dir = self.data_dir / "raw_html"
        self.jsonl_path = self.data_dir / "raw_entries.jsonl"
        self.checkpoint_path = self.data_dir / "checkpoint.json"
        self.robots_copy_path = self.data_dir / "robots.txt"

        self.entries: list[dict] = []
        self._seen_ids: set[int] = set()
        self.pages_fetched = 0
        self._robots = urllib.robotparser.RobotFileParser()
        self._robots_text = ""
        self._robots_checked = False
        self._parser = "lxml" if _lxml_available() else "html.parser"

    # ------------------------------------------------------------------ #
    #                           robots.txt                               #
    # ------------------------------------------------------------------ #

    def check_robots(self) -> bool:
        """Fetch robots.txt, save a copy as evidence, and verify our access.

        Returns True when the survey listing and result pages are allowed for
        this scraper's user agent.  A copy of the file is written to
        data/robots.txt so the check is reproducible.
        """
        robots_url = urljoin(BASE_URL, ROBOTS_PATH)
        logger.info("Checking %s", robots_url)
        # RobotFileParser.read() would use Python's default user agent, which
        # this site rejects with 403 (and 403 is then treated as "disallow
        # everything").  Fetch the text ourselves and hand it to the parser.
        try:
            robots_text = self._http_get(robots_url)
        except urllib.error.HTTPError as err:
            if err.code in (404, 410):
                # RFC 9309 section 2.3.1.3: an unavailable robots.txt means no restrictions.
                logger.warning("robots.txt returned HTTP %s; treating as allow-all", err.code)
                robots_text = ""
            else:
                raise
        if not _looks_like_robots_file(robots_text):
            # An HTML interstitial (or anything else) in place of robots.txt
            # means the site's rules cannot be confirmed, so nothing is fetched.
            logger.error("robots.txt could not be read as a robots file; stopping without scraping")
            return False
        self._robots.parse(robots_text.splitlines())
        self._robots_text = robots_text
        self._robots_checked = True

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.robots_copy_path.write_text(robots_text, encoding="utf-8")

        survey_url = urljoin(BASE_URL, SURVEY_PATH)
        result_url = urljoin(BASE_URL, "/result/1")
        signin_url = urljoin(BASE_URL, "/signin")  # a disallowed path, as a sanity check

        # Check 1: the standard library parser.
        std = {u: self._robots.can_fetch(PRODUCT_TOKEN, u) for u in (survey_url, result_url, signin_url)}
        # Check 2: RFC 9309 semantics.  Grad Cafe's robots.txt splits its
        # "User-agent: *" rules over two groups and RobotFileParser keeps only the
        # first one (so it reports /signin as allowed).  This second evaluation
        # merges every applicable group and lets the longest matching path win.
        rfc = {u: _robots_allows(robots_text, PRODUCT_TOKEN, u) for u in (survey_url, result_url, signin_url)}
        logger.info(
            "robots.txt (urllib.robotparser): survey=%s result=%s signin=%s | "
            "(RFC 9309 longest-match): survey=%s result=%s signin=%s",
            std[survey_url], std[result_url], std[signin_url],
            rfc[survey_url], rfc[result_url], rfc[signin_url],
        )

        # Honour a Crawl-delay directive if the site ever adds one.
        crawl_delay = self._robots.crawl_delay(PRODUCT_TOKEN)
        if crawl_delay and crawl_delay > self.delay_seconds:
            logger.info("robots.txt requests Crawl-delay=%s; raising delay", crawl_delay)
            self.delay_seconds = float(crawl_delay)

        return all((std[survey_url], std[result_url], rfc[survey_url], rfc[result_url]))

    def _assert_allowed(self, url: str) -> None:
        """Refuse to fetch anything robots.txt disallows (host/path are checked by _safe_site_url)."""
        if self._robots_checked:
            allowed = self._robots.can_fetch(PRODUCT_TOKEN, url) and _robots_allows(
                self._robots_text, PRODUCT_TOKEN, url
            )
            if not allowed:
                raise PermissionError(f"robots.txt disallows {url}")

    # ------------------------------------------------------------------ #
    #                                HTTP                                #
    # ------------------------------------------------------------------ #

    def _http_get(self, url: str) -> str:
        """Single GET with our user agent.

        Raises ScrapeBlockedError for 401/403/429/5xx or a challenge page, and
        re-raises any other HTTPError (404, 410, ...) untouched because those
        are permanent, not transient.
        """
        url = _safe_site_url(url)  # fixed host + allow-listed path, or ValueError
        self._assert_allowed(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
                mitigated = response.headers.get(CLOUDFLARE_MITIGATION_HEADER)
        except urllib.error.HTTPError as err:
            if err.headers.get(CLOUDFLARE_MITIGATION_HEADER):
                raise ScrapeBlockedError(
                    f"HTTP {err.code} with Cloudflare mitigation '{err.headers.get(CLOUDFLARE_MITIGATION_HEADER)}' for {url}",
                    status=err.code,
                ) from err
            if err.code in (401, 403, 429) or err.code >= 500:
                # Cloudflare serves its challenge page with HTTP 503: that is a
                # block (never retried), not a transient server error.
                try:
                    error_body = err.read().decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    error_body = ""
                challenged = self._looks_like_challenge(error_body)
                message = f"HTTP {err.code} {err.reason} for {url}" + (" (challenge page)" if challenged else "")
                raise ScrapeBlockedError(message, status=err.code, retryable=err.code >= 500 and not challenged) from err
            raise
        if mitigated:
            # Cloudflare labels every mitigated (challenged / blocked) response,
            # even a 200 with substitute content: treat it as a block, never retry.
            raise ScrapeBlockedError(f"Cloudflare mitigation '{mitigated}' applied to {url}")
        if self._looks_like_challenge(body):
            raise ScrapeBlockedError(f"Cloudflare challenge page returned for {url}")
        return body

    def _fetch_page(self, url: str) -> str:
        """GET with a small, slow retry budget for transient failures only.

        * 401 / 403 / 429, a challenge page, or any other HTTP error such as
          404: stop at once, never retry.
        * plain 5xx: wait 30 s and retry once.
        * timeout / connection error: wait 30 s, retry; wait 120 s, retry;
          then give up with ScrapeNetworkError.
        """
        server_error_retried = False
        network_failures = 0
        while True:
            try:
                return self._http_get(url)
            except ScrapeBlockedError as err:
                if not err.retryable or server_error_retried:
                    raise
                server_error_retried = True
                wait_seconds = SERVER_ERROR_RETRY_WAIT_SECONDS
                logger.warning("%s; waiting %ss then retrying once", err, wait_seconds)
            except urllib.error.HTTPError:
                raise  # 404/410/...: permanent, not transient (HTTPError subclasses URLError)
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if network_failures >= len(NETWORK_RETRY_WAITS_SECONDS):
                    raise ScrapeNetworkError(f"network failed {network_failures + 1} times for {url}: {err}") from err
                wait_seconds = NETWORK_RETRY_WAITS_SECONDS[network_failures]
                network_failures += 1
                logger.warning("Network error %s; waiting %ss then retrying", err, wait_seconds)
            time.sleep(wait_seconds)

    @staticmethod
    def _looks_like_challenge(body: str) -> bool:
        """True when the response is a Cloudflare interstitial, not a listing."""
        return any(marker in body for marker in CHALLENGE_MARKERS)

    # ------------------------------------------------------------------ #
    #                  URL management (urllib.parse)                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_start_url() -> str:
        """Construct the first listing URL from its components."""
        base = urlparse(BASE_URL)
        query = urlencode({"page": 1})
        return urlunparse((base.scheme, base.netloc, SURVEY_PATH, "", query, ""))

    @staticmethod
    def _describe_cursor(url: str) -> str:
        """Decode the base64 pagination cursor for readable log lines."""
        query = parse_qs(urlparse(url).query)
        cursor = query.get("cursor", [""])[0]
        if not cursor:
            return "start of listing"
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            return f"created_at<{payload.get('created_at')} id<{payload.get('admitid')}"
        except (ValueError, TypeError):
            return f"cursor={cursor[:16]}..."

    def _next_page_url(self, soup: BeautifulSoup, page_url: str) -> str | None:
        """Return the absolute URL of the "Next" pagination link, if any."""
        nav = soup.find("nav", attrs={"aria-label": "Results pagination"})
        anchors = nav.find_all("a", href=True) if nav else soup.find_all("a", href=True)
        for anchor in anchors:
            if anchor.get_text(" ", strip=True).lower().startswith("next"):
                candidate = urljoin(page_url, anchor["href"])
                try:
                    return _safe_site_url(candidate)
                except ValueError as err:
                    logger.warning("Ignoring next link: %s", err)
                    return None
        return None

    # ------------------------------------------------------------------ #
    #                      Parsing (BeautifulSoup)                       #
    # ------------------------------------------------------------------ #

    def _parse_page(self, html: str, page_url: str, scraped_at: str | None = None) -> tuple[list[dict], str | None]:
        """Turn one listing page into raw entries plus the next-page URL.

        Each applicant occupies one main <tr> (school, program/degree, date
        added, decision, link) optionally followed by "tw-border-none" rows: a
        tag row (term, nationality, GPA, GRE badges) and a comment row.  Rows
        are grouped by walking the table in order, starting a new group at
        every row that carries a /result/<id> link.  The ad and spacer rows the
        site inserts between applicants carry neither and are ignored.
        """
        soup = BeautifulSoup(html, self._parser)
        table = soup.find("table")
        rows = table.find_all("tr") if table else []

        entries: list[dict] = []
        current_main: Tag | None = None
        current_extra: list[Tag] = []
        for row in rows:
            if row.find("th") is not None:
                continue  # header row
            link = row.find("a", href=RESULT_HREF_PATTERN)
            if link is not None and len(row.find_all("td", recursive=False)) >= 4:
                if current_main is not None:
                    entries.append(self._parse_entry(current_main, current_extra, page_url, scraped_at))
                current_main, current_extra = row, []
            elif current_main is not None and CONTINUATION_ROW_CLASS in row.get("class", []):
                current_extra.append(row)
        if current_main is not None:
            entries.append(self._parse_entry(current_main, current_extra, page_url, scraped_at))

        # Attach the page's own JSON record for each entry (may be None).
        json_by_id = self._extract_listing_json(soup)
        for entry in entries:
            entry["listing_json"] = json_by_id.get(entry["result_id"])
        if json_by_id and set(json_by_id) != {entry["result_id"] for entry in entries}:
            logger.warning("Listing JSON ids differ from the table rows on %s", page_url)

        return entries, self._next_page_url(soup, page_url)

    @staticmethod
    def _extract_listing_json(soup: BeautifulSoup) -> dict[int, dict]:
        """Return the entry records embedded in the page's JSON payload, by id.

        The visible badges show decision dates as month/day only; the embedded
        payload has the full date ("date_of_notification"), so it is kept next
        to the visible text for traceability.  HTML parsing remains the primary
        path: a missing or malformed payload simply yields an empty mapping.
        """
        app_div = soup.find("div", id=LISTING_JSON_ELEMENT_ID)
        payload = app_div.get(LISTING_JSON_ATTRIBUTE) if app_div else None
        if not payload:
            return {}
        try:
            records = json.loads(payload)["props"]["results"]["data"]
        except (ValueError, KeyError, TypeError):
            logger.warning("Listing JSON payload missing or malformed; using HTML only")
            return {}
        return {
            record["id"]: record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("id"), int)
        }

    def _parse_entry(self, main_row: Tag, extra_rows: list[Tag], page_url: str, scraped_at: str | None = None) -> dict:
        """Extract the raw visible text of one applicant listing."""
        cells = main_row.find_all("td", recursive=False)
        link = main_row.find("a", href=RESULT_HREF_PATTERN)
        href = link["href"]
        result_url = urljoin(BASE_URL, href)
        result_id = int(RESULT_HREF_PATTERN.match(href).group(1))

        school_text = _clean_text(cells[0]) if len(cells) > 0 else None

        # Program cell: <span>Program</span> <svg/> <span>Degree</span>
        program_text = degree_text = None
        if len(cells) > 1:
            spans = cells[1].find_all("span")
            if spans:
                program_text = _clean_text(spans[0])
                if len(spans) > 1:
                    degree_text = _clean_text(spans[1])
            else:
                program_text = _clean_text(cells[1])

        date_added_text = _clean_text(cells[2]) if len(cells) > 2 else None
        decision_text = _clean_text(cells[3]) if len(cells) > 3 else None

        tags: list[str] = []
        comment_parts: list[str] = []
        for row in extra_rows:
            for badge in row.select("div.tw-inline-flex"):
                classes = badge.get("class", [])
                if "md:tw-hidden" in classes:
                    continue  # mobile-only duplicate of the decision badge
                badge_text = _clean_text(badge)
                if badge_text:
                    tags.append(badge_text)
            for paragraph in row.find_all("p"):
                paragraph_text = _clean_text(paragraph)
                if paragraph_text:
                    comment_parts.append(paragraph_text)

        return {
            "result_id": result_id,
            "url": result_url,
            "school_text": school_text or None,
            "program_text": program_text or None,
            "degree_text": degree_text or None,
            "date_added_text": date_added_text or None,
            "decision_text": decision_text or None,
            "tags_text": tags,
            "comment_text": "\n".join(comment_parts) if comment_parts else None,
            "listing_json": None,  # filled in by _parse_page from the page payload
            "source_page_url": page_url,
            "scraped_at": scraped_at or _utc_now(),
        }

    # ------------------------------------------------------------------ #
    #                         Persistence / resume                       #
    # ------------------------------------------------------------------ #

    def _add_new_entries(self, entries: list[dict]) -> list[dict]:
        """Append entries whose result_id has not been seen; return those added."""
        added: list[dict] = []
        for entry in entries:
            result_id = entry["result_id"]
            if result_id in self._seen_ids:
                continue
            self._seen_ids.add(result_id)
            self.entries.append(entry)
            added.append(entry)
        return added

    def _load_checkpoint(self) -> dict | None:
        """Return the saved checkpoint dict, or None when absent or unreadable."""
        if not self.checkpoint_path.exists():
            return None
        try:
            checkpoint = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("%s is unreadable", self.checkpoint_path)
            return None
        return checkpoint if isinstance(checkpoint, dict) else None

    def _save_checkpoint(self, next_url: str | None, finished: bool) -> None:
        """Atomically record where the next run should continue."""
        payload = {
            "next_url": next_url,
            "pages_fetched": self.pages_fetched,
            "entries_collected": len(self.entries),
            "finished": finished,
            "updated_at": _utc_now(),
        }
        temp_path = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.checkpoint_path)

    def _load_jsonl(self) -> None:
        """Load previously saved entries; a damaged line is skipped, not fatal."""
        if not self.jsonl_path.exists():
            return
        loaded: list[dict] = []
        skipped = 0
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
                    logger.warning("Skipping unreadable line %d of %s", line_number, self.jsonl_path)
        self._add_new_entries(loaded)
        if skipped:
            logger.warning("%d unreadable line(s) ignored in %s", skipped, self.jsonl_path)

    def _append_jsonl(self, entries: list[dict]) -> None:
        """Append entries to the progress log (one JSON object per line)."""
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _cache_page_html(self, html: str, page_number: int, page_url: str) -> None:
        """Keep a copy of the fetched page so it can be re-parsed offline.

        The first line records the source URL and fetch time; everything after
        it is the untouched server response.
        """
        if not self.cache_html:
            return
        self.html_cache_dir.mkdir(parents=True, exist_ok=True)
        header = f"<!-- {PRODUCT_TOKEN} source-url: {page_url} fetched-at: {_utc_now()} -->\n"
        (self.html_cache_dir / f"page_{page_number:05d}.html").write_text(header + html, encoding="utf-8")

    def reparse_cached_pages(self) -> list[dict]:
        """Rebuild the raw entries from data/raw_html without any network access.

        Useful after changing the parser: the pages already on disk are parsed
        again in the order they were fetched and de-duplicated by result id.
        Each entry keeps the fetch time recorded in the cache file header (or
        the file's modification time for older cache files).
        """
        self.entries.clear()
        self._seen_ids.clear()
        cached = sorted(self.html_cache_dir.glob("page_*.html"))
        if not cached:
            raise ScrapeStateError(f"No cached pages under {self.html_cache_dir}; run a scrape first")
        for path in cached:
            text = path.read_text(encoding="utf-8")
            first_line, _, remainder = text.partition("\n")
            url_match = re.search(r"source-url: (\S+)", first_line)
            time_match = re.search(r"fetched-at: (\S+)", first_line)
            if url_match:
                page_url, html = url_match.group(1), remainder
            else:
                page_url, html = f"file://{path}", text
            fetched_at = time_match.group(1) if time_match else datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat(timespec="seconds")
            page_entries, _ = self._parse_page(html, page_url, scraped_at=fetched_at)
            self._add_new_entries(page_entries)
        logger.info("Re-parsed %d cached pages -> %d entries", len(cached), len(self.entries))
        return self.entries

    def _reset_progress(self) -> None:
        """Discard every trace of earlier runs (used by --fresh only)."""
        for path in (self.jsonl_path, self.checkpoint_path):
            if path.exists():
                path.unlink()
        if self.html_cache_dir.exists():
            shutil.rmtree(self.html_cache_dir)
            logger.info("Removed cached pages under %s", self.html_cache_dir)
        self.entries.clear()
        self._seen_ids.clear()
        self.pages_fetched = 0

    # ------------------------------------------------------------------ #
    #                            Main loop                               #
    # ------------------------------------------------------------------ #

    def _resume_point(self, resume: bool) -> str | None:
        """Load earlier progress and return the URL to fetch next (None = nothing to do)."""
        if not resume:
            self._reset_progress()
            return self._build_start_url()

        checkpoint = self._load_checkpoint()
        if checkpoint is None:
            if self.jsonl_path.exists():
                raise ScrapeStateError(
                    f"{self.checkpoint_path} is missing or unreadable but {self.jsonl_path} exists; "
                    "restore the checkpoint or re-run with --fresh to start over"
                )
            return self._build_start_url()

        self._load_jsonl()
        self.pages_fetched = int(checkpoint.get("pages_fetched", 0))
        url = checkpoint.get("next_url")
        logger.info(
            "Resuming: %d entries from %d pages already saved; next page = %s",
            len(self.entries), self.pages_fetched, self._describe_cursor(url or ""),
        )
        if url is None:
            logger.info("Previous run exhausted the listing; nothing more to fetch")
        elif checkpoint.get("finished") and len(self.entries) >= self.target_entries:
            logger.info("Previous run already reached the target; nothing to do")
            url = None
        return url

    def scrape_data(self, resume: bool = True) -> list[dict]:
        """Pull listing pages until *target_entries* raw entries are collected.

        With resume=True (default) a previous interrupted run is continued from
        its checkpoint.  Returns the accumulated list of raw entry dicts; the
        same list is also streamed to data/raw_entries.jsonl as it grows.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        url = self._resume_point(resume)  # local files first, so a block later cannot lose them
        if url is None:
            return self.entries
        if not self.check_robots():
            raise PermissionError("robots.txt does not allow (or could not confirm) scraping the survey pages")

        pages_this_run = 0
        stale_pages = 0
        started = time.monotonic()
        try:
            while url and len(self.entries) < self.target_entries:
                if self.max_pages is not None and pages_this_run >= self.max_pages:
                    logger.info("Reached --max-pages=%d for this run", self.max_pages)
                    break

                fetch_started = time.monotonic()
                html = self._fetch_page(url)
                fetch_seconds = time.monotonic() - fetch_started
                self.pages_fetched += 1
                pages_this_run += 1
                self._cache_page_html(html, self.pages_fetched, url)

                page_entries, next_url = self._parse_page(html, url)
                new_entries = self._add_new_entries(page_entries)
                self._append_jsonl(new_entries)
                self._save_checkpoint(next_url, finished=False)

                remaining = max(0, self.target_entries - len(self.entries))
                per_page = max(1, len(page_entries))
                eta_minutes = remaining / per_page * (self.delay_seconds + fetch_seconds) / 60
                logger.info(
                    "page %d: %d entries (%d new) in %.1fs | total %d/%d | eta %.0f min | next: %s",
                    self.pages_fetched, len(page_entries), len(new_entries), fetch_seconds,
                    len(self.entries), self.target_entries, eta_minutes,
                    self._describe_cursor(next_url) if next_url else "none",
                )

                # Guards against re-requesting the same listing forever.
                if not page_entries:
                    logger.warning("Page had no entries; stopping to avoid looping on an empty listing")
                    break
                if next_url == url:
                    logger.warning("Next link points at the current page; stopping")
                    next_url = None
                stale_pages = 0 if new_entries else stale_pages + 1
                if stale_pages >= MAX_STALE_PAGES:
                    logger.warning("%d consecutive pages brought no new entries; stopping", stale_pages)
                    break

                if self.pages_fetched % SAVE_JSON_EVERY_N_PAGES == 0:
                    save_data(self.entries, self.data_dir / "raw_entries.json")

                url = next_url
                if url and len(self.entries) < self.target_entries:
                    time.sleep(self.delay_seconds)  # politeness delay between requests
        except (ScrapeBlockedError, ScrapeNetworkError, urllib.error.HTTPError) as err:
            # Site said no, the network is down, or an unexpected HTTP status:
            # persist what we have and stop.  The checkpoint still points at the
            # page that failed, so a later run resumes there.
            logger.error("Stopping: %s", err)
            self._save_checkpoint(url, finished=False)
            raise
        except KeyboardInterrupt:
            logger.warning("Interrupted by user; progress saved (re-run to resume)")
            self._save_checkpoint(url, finished=False)
            raise

        finished = url is None or len(self.entries) >= self.target_entries
        self._save_checkpoint(url, finished=finished)
        logger.info(
            "Done this run: %d pages in %.1f min; %d entries total (finished=%s)",
            pages_this_run, (time.monotonic() - started) / 60, len(self.entries), finished,
        )
        return self.entries


# --------------------------------------------------------------------------- #
#                                  Helpers                                    #
# --------------------------------------------------------------------------- #


CURSOR_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _safe_cursor(cursor: str) -> str:
    """Decode a pagination cursor, validate its three fields, and re-encode it.

    Grad Cafe's cursor is URL-safe base64 of {"created_at": "<timestamp>",
    "admitid": <int>, "_pointsToNextItems": <bool>}.  Only those typed values
    are carried over, so a damaged checkpoint or an odd link cannot smuggle
    anything else into the query string.  A valid cursor re-encodes to the
    identical string.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
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


def _safe_site_url(url: str) -> str:
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
            clean_query["cursor"] = _safe_cursor(query["cursor"][0])
    except (ValueError, KeyError, TypeError) as err:
        raise ValueError(f"refusing a URL with an invalid query: {url}") from err
    base = urlparse(BASE_URL)
    return urlunparse((base.scheme, base.netloc, path, "", urlencode(clean_query), ""))


def _local_name(value: str | Path) -> str:
    """Reduce a command-line path to a bare file or folder name.

    Everything this tool reads or writes lives inside the module_2 folder, so
    only the last path component of an option is used: "../../etc" collapses
    to "etc" and still lands inside the module.  Empty names are refused.
    """
    name = os.path.basename(os.path.normpath(str(value)))
    if not name or name in (".", ".."):
        raise ValueError(f"not a usable file or folder name: {value!r}")
    return name


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(node: Tag | None) -> str | None:
    """Visible text of a tag with entities decoded and whitespace collapsed."""
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    text = " ".join(text.split())
    return text or None


def _robots_allows(robots_text: str, agent_token: str, url: str) -> bool:
    """Evaluate robots.txt for *url* with RFC 9309 semantics.

    All groups naming *agent_token* (case-insensitive substring match) are
    merged; if none do, all "User-agent: *" groups are merged instead.  The
    rule with the longest matching path decides, and a tie goes to Allow.
    Unknown directives (Sitemap, Content-Signal, Crawl-delay...) are ignored.
    """
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    reading_agents = True
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not reading_agents:  # rules were read, so this starts a new group
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            reading_agents = True
        elif key in ("allow", "disallow"):
            reading_agents = False
            if value:  # an empty Disallow means "no restriction"
                rules.append((key == "allow", value))
    if agents or rules:
        groups.append((agents, rules))

    token = agent_token.lower()
    specific = [r for a, r in groups if any(agent != "*" and agent in token for agent in a)]
    applicable = specific or [r for a, r in groups if "*" in a]

    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    best_length, best_allow = -1, True
    for group_rules in applicable:
        for allow, pattern in group_rules:
            regex = re.escape(pattern).replace(r"\*", ".*")
            if regex.endswith(r"\$"):
                regex = regex[:-2] + "$"
            if re.match(regex, path):
                length = len(pattern)
                if length > best_length or (length == best_length and allow):
                    best_length, best_allow = length, allow
    return best_allow


def _looks_like_robots_file(text: str) -> bool:
    """True for an empty file or one with robots directives; False for HTML or other content."""
    lowered = text.lower()
    if "<html" in lowered or "<!doctype" in lowered:
        return False
    return not text.strip() or "user-agent" in lowered


def _lxml_available() -> bool:
    """True when the faster lxml parser is installed (html.parser otherwise)."""
    try:
        import lxml  # noqa: F401
    except ImportError:
        return False
    return True


def _configure_logging(log_path: Path) -> None:
    """Log to stdout and to <data-dir>/scrape.log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")],
    )


# --------------------------------------------------------------------------- #
#                                  Command line                               #
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options for a scrape run."""
    parser = argparse.ArgumentParser(description="Scrape Grad Cafe admissions results.")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET_ENTRIES,
                        help="stop after collecting at least this many entries (default 30000)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                        help="seconds to wait between page requests (default 2.0)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="fetch at most this many pages in this run (useful for testing)")
    parser.add_argument("--data-dir", default="data",
                        help="folder name inside module_2 for raw JSONL, checkpoint, HTML cache and log (default data)")
    parser.add_argument("--raw-output", default=None,
                        help="raw entries JSON file name inside the data folder (default raw_entries.json)")
    parser.add_argument("--output", default="applicant_data.json",
                        help="cleaned JSON file name inside module_2 (default applicant_data.json)")
    parser.add_argument("--fresh", action="store_true",
                        help="discard checkpoint, progress log and cached pages, and start from the first page")
    parser.add_argument("--no-clean", action="store_true",
                        help="skip running clean.py on the scraped entries")
    parser.add_argument("--no-cache-html", action="store_true",
                        help="do not keep a copy of each fetched page under <data-dir>/raw_html")
    parser.add_argument("--reparse-cache", action="store_true",
                        help="skip the network and rebuild the raw entries from <data-dir>/raw_html")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run a scrape (or a cache re-parse), save the raw JSON, then clean it.

    Exit codes: 0 ok, 2 the site rejected a request, 3 the network kept
    failing, 4 an unexpected HTTP status, 5 inconsistent local files,
    130 interrupted.  Progress is always saved, so re-running resumes.
    """
    args = _parse_args(argv)
    script_dir = Path(__file__).resolve().parent
    # Command-line options name files and folders inside module_2 (see _local_name).
    try:
        data_dir = script_dir / _local_name(args.data_dir)
        raw_output = data_dir / _local_name(args.raw_output or "raw_entries.json")
        output = script_dir / _local_name(args.output)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    _configure_logging(data_dir / "scrape.log")

    scraper = GradCafeScraper(
        target_entries=args.target,
        delay_seconds=args.delay,
        data_dir=data_dir,
        max_pages=args.max_pages,
        cache_html=not args.no_cache_html,
    )

    exit_code = 0
    try:
        if args.reparse_cache:
            entries = scraper.reparse_cached_pages()
        else:
            entries = scraper.scrape_data(resume=not args.fresh)
    except ScrapeBlockedError:
        entries = scraper.entries
        exit_code = 2
        logger.error("The site rejected a request. Nothing was retried; re-run later to resume.")
    except PermissionError as err:
        entries = scraper.entries
        exit_code = 2
        logger.error("%s; nothing was fetched", err)
    except ScrapeNetworkError:
        entries = scraper.entries
        exit_code = 3
        logger.error("Network failure. Progress is saved; re-run later to resume.")
    except urllib.error.HTTPError as err:
        entries = scraper.entries
        exit_code = 4
        logger.error("Unexpected HTTP %s for %s; stopping. Progress is saved.", err.code, err.url)
    except ScrapeStateError as err:
        logger.error("%s", err)
        return 5
    except KeyboardInterrupt:
        entries = scraper.entries
        exit_code = 130

    if not entries:
        logger.warning("No entries collected; leaving %s untouched", raw_output)
        return exit_code

    save_data(entries, raw_output)
    logger.info("Saved %d raw entries to %s", len(entries), raw_output)
    # The plain raw file is ~50 MB at 30k entries, so the copy kept in git is
    # the gzip one; clean.py reads either.
    gzip_output = raw_output.with_name(raw_output.name + ".gz")
    save_data(entries, gzip_output)
    logger.info("Saved gzip copy to %s", gzip_output)

    if not args.no_clean:
        from clean import clean_data  # local import avoids a circular import at module load

        cleaned = clean_data(entries)
        save_data(cleaned, output)
        logger.info("Saved %d cleaned entries to %s", len(cleaned), output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
