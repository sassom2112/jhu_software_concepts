"""
scrape.py - Grad Cafe admissions-results scraper.

JHU EN.605.256 Modern Software Concepts in Python - Module 2.

Workflow (urllib-only; no browser automation was needed for this site):

  1. Fetch https://www.thegradcafe.com/robots.txt with urllib and confirm with
     urllib.robotparser that the survey listing is allowed for our user agent.
  2. Build the first listing URL with urllib.parse and fetch it with
     urllib.request.
  3. Parse the server-rendered HTML with BeautifulSoup into "raw" entry dicts
     (every value is the visible text exactly as shown on the listing row).
  4. Follow the page's "Next" link (Grad Cafe uses cursor-based pagination, so
     the next URL is read from the page rather than computed) until the target
     number of entries has been collected, sleeping between requests.
  5. Persist progress after every page (JSONL + checkpoint) so an interrupted
     run picks up where it left off, then write the raw entries to JSON.

The scraper never tries to get around a block: a 401/403/429/5xx response or a
Cloudflare challenge page stops the run immediately.  Re-running the script
later resumes from the saved checkpoint.

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
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

# --------------------------------------------------------------------------- #
#                                Constants                                    #
# --------------------------------------------------------------------------- #

BASE_URL = "https://www.thegradcafe.com"
SURVEY_PATH = "/survey/"
ROBOTS_PATH = "/robots.txt"

# Honest, descriptive user agent.  Grad Cafe (via Cloudflare) answers 403 to
# Python's default "Python-urllib/3.x" agent but serves this one normally.
PRODUCT_TOKEN = "JHU-EN605256-GradCafeScraper"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN}/1.0; "
    "student coursework; contact msasso1@jh.edu)"
)

DEFAULT_TARGET_ENTRIES = 30_000
DEFAULT_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30
RETRY_WAIT_SECONDS = 30
SAVE_JSON_EVERY_N_PAGES = 25

# Strings that only appear on a Cloudflare interstitial, never on a real page.
CHALLENGE_MARKERS = ("<title>Just a moment...</title>", "cf-chl-bypass", "challenge-error-text")

RESULT_HREF_PATTERN = re.compile(r"^/result/(\d+)")

# The listing is an Inertia.js page: <div id="app" data-page="{...}"> carries a
# JSON copy of the 20 rendered entries (with full ISO decision dates).
LISTING_JSON_ELEMENT_ID = "app"
LISTING_JSON_ATTRIBUTE = "data-page"

logger = logging.getLogger("gradcafe.scrape")


class ScrapeBlockedError(RuntimeError):
    """Raised when the site blocks, rate-limits, or otherwise rejects a request."""


class ScrapeNetworkError(RuntimeError):
    """Raised when the network fails twice in a row (the run stops; resume later)."""


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
        robots_text = self._http_get(robots_url)
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
        """Refuse to fetch anything robots.txt disallows or that is off-site."""
        parsed = urlparse(url)
        if parsed.netloc != urlparse(BASE_URL).netloc:
            raise ValueError(f"Refusing to fetch off-site URL: {url}")
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
        """Single GET with our user agent.  Raises ScrapeBlockedError on a block."""
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
        except urllib.error.HTTPError as err:
            if err.code in (401, 403, 429) or err.code >= 500:
                raise ScrapeBlockedError(f"HTTP {err.code} {err.reason} for {url}") from err
            raise
        if self._looks_like_challenge(body):
            raise ScrapeBlockedError(f"Cloudflare challenge page returned for {url}")
        return body

    def _fetch_page(self, url: str) -> str:
        """GET with one polite retry for transient network errors only."""
        try:
            return self._http_get(url)
        except ScrapeBlockedError as err:
            if "HTTP 5" in str(err):  # transient server error: one retry
                logger.warning("%s; waiting %ss then retrying once", err, RETRY_WAIT_SECONDS)
                time.sleep(RETRY_WAIT_SECONDS)
                return self._http_get(url)
            raise  # 401/403/429/challenge: stop immediately, never retry
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            logger.warning("Network error %s; waiting %ss then retrying once", err, RETRY_WAIT_SECONDS)
            time.sleep(RETRY_WAIT_SECONDS)
            try:
                return self._http_get(url)
            except (urllib.error.URLError, TimeoutError, OSError) as second_err:
                raise ScrapeNetworkError(f"network failed twice for {url}: {second_err}") from second_err

    @staticmethod
    def _looks_like_challenge(body: str) -> bool:
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
                if urlparse(candidate).netloc != urlparse(BASE_URL).netloc:
                    logger.warning("Ignoring off-site next link %s", candidate)
                    return None
                return candidate
        return None

    # ------------------------------------------------------------------ #
    #                      Parsing (BeautifulSoup)                       #
    # ------------------------------------------------------------------ #

    def _parse_page(self, html: str, page_url: str) -> tuple[list[dict], str | None]:
        """Turn one listing page into raw entries plus the next-page URL.

        Each applicant occupies one main <tr> (school, program/degree, date
        added, decision, link) optionally followed by a tag row (term,
        nationality, GPA, GRE badges) and a comment row.  Rows are grouped by
        walking the table in order and starting a new group at every row that
        carries a /result/<id> link.
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
                    entries.append(self._parse_entry(current_main, current_extra, page_url))
                current_main, current_extra = row, []
            elif current_main is not None:
                current_extra.append(row)
        if current_main is not None:
            entries.append(self._parse_entry(current_main, current_extra, page_url))

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

    def _parse_entry(self, main_row: Tag, extra_rows: list[Tag], page_url: str) -> dict:
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
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------ #
    #                         Persistence / resume                       #
    # ------------------------------------------------------------------ #

    def _load_checkpoint(self) -> dict | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Checkpoint file unreadable; starting fresh")
            return None

    def _save_checkpoint(self, next_url: str | None, finished: bool) -> None:
        payload = {
            "next_url": next_url,
            "pages_fetched": self.pages_fetched,
            "entries_collected": len(self.entries),
            "finished": finished,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.checkpoint_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_jsonl(self) -> None:
        if not self.jsonl_path.exists():
            return
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry["result_id"] not in self._seen_ids:
                    self._seen_ids.add(entry["result_id"])
                    self.entries.append(entry)

    def _append_jsonl(self, entries: list[dict]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _cache_page_html(self, html: str, page_number: int, page_url: str) -> None:
        """Keep a copy of the fetched page so it can be re-parsed offline.

        The first line records the URL it came from; everything after it is the
        untouched server response.
        """
        if not self.cache_html:
            return
        self.html_cache_dir.mkdir(parents=True, exist_ok=True)
        header = f"<!-- {PRODUCT_TOKEN} source-url: {page_url} -->\n"
        (self.html_cache_dir / f"page_{page_number:05d}.html").write_text(header + html, encoding="utf-8")

    def reparse_cached_pages(self) -> list[dict]:
        """Rebuild the raw entries from data/raw_html without any network access.

        Useful after changing the parser: the pages already on disk are parsed
        again in the order they were fetched and de-duplicated by result id.
        """
        self.entries.clear()
        self._seen_ids.clear()
        cached = sorted(self.html_cache_dir.glob("page_*.html"))
        if not cached:
            raise FileNotFoundError(f"No cached pages under {self.html_cache_dir}")
        for path in cached:
            text = path.read_text(encoding="utf-8")
            first_line, _, html = text.partition("\n")
            match = re.search(r"source-url: (\S+)", first_line)
            page_url = match.group(1) if match else f"file://{path}"
            page_entries, _ = self._parse_page(html if match else text, page_url)
            new_entries = [e for e in page_entries if e["result_id"] not in self._seen_ids]
            self._seen_ids.update(e["result_id"] for e in new_entries)
            self.entries.extend(new_entries)
        logger.info("Re-parsed %d cached pages -> %d entries", len(cached), len(self.entries))
        return self.entries

    def _reset_progress(self) -> None:
        for path in (self.jsonl_path, self.checkpoint_path):
            if path.exists():
                path.unlink()
        self.entries.clear()
        self._seen_ids.clear()
        self.pages_fetched = 0

    # ------------------------------------------------------------------ #
    #                            Main loop                               #
    # ------------------------------------------------------------------ #

    def scrape_data(self, resume: bool = True) -> list[dict]:
        """Pull listing pages until *target_entries* raw entries are collected.

        With resume=True (default) a previous interrupted run is continued from
        its checkpoint.  Returns the accumulated list of raw entry dicts; the
        same list is also streamed to data/raw_entries.jsonl as it grows.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.check_robots():
            raise PermissionError("robots.txt does not allow scraping the survey pages")

        checkpoint = self._load_checkpoint() if resume else None
        if checkpoint:
            self._load_jsonl()
            self.pages_fetched = int(checkpoint.get("pages_fetched", 0))
            url = checkpoint.get("next_url")
            logger.info(
                "Resuming: %d entries from %d pages already saved; next page = %s",
                len(self.entries), self.pages_fetched, self._describe_cursor(url or ""),
            )
            if checkpoint.get("finished") or url is None:
                if len(self.entries) >= self.target_entries:
                    logger.info("Previous run already reached the target; nothing to do")
                    return self.entries
                if url is None:
                    logger.info("Previous run exhausted the listing; nothing more to fetch")
                    return self.entries
        else:
            self._reset_progress()
            url = self._build_start_url()

        pages_this_run = 0
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
                new_entries = [e for e in page_entries if e["result_id"] not in self._seen_ids]
                for entry in new_entries:
                    self._seen_ids.add(entry["result_id"])
                self.entries.extend(new_entries)
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

                if not page_entries:
                    logger.warning("Page had no entries; stopping to avoid looping on an empty listing")
                    break
                if self.pages_fetched % SAVE_JSON_EVERY_N_PAGES == 0:
                    save_data(self.entries, self.data_dir / "raw_entries.json")

                url = next_url
                if url and len(self.entries) < self.target_entries:
                    time.sleep(self.delay_seconds)  # politeness delay between requests
        except (ScrapeBlockedError, ScrapeNetworkError) as err:
            # Site said no (or the network is down): persist what we have and
            # stop.  The checkpoint still points at the page that failed, so a
            # later run resumes there.
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


def _lxml_available() -> bool:
    try:
        import lxml  # noqa: F401
    except ImportError:
        return False
    return True


def _configure_logging(log_path: Path) -> None:
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
    parser = argparse.ArgumentParser(description="Scrape Grad Cafe admissions results.")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET_ENTRIES,
                        help="stop after collecting at least this many entries (default 30000)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                        help="seconds to wait between page requests (default 2.0)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="fetch at most this many pages in this run (useful for testing)")
    parser.add_argument("--data-dir", default="data",
                        help="directory for raw JSONL, checkpoint, HTML cache and log")
    parser.add_argument("--raw-output", default=None,
                        help="raw entries JSON path (default <data-dir>/raw_entries.json)")
    parser.add_argument("--output", default="applicant_data.json",
                        help="cleaned JSON written after scraping (default applicant_data.json)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore any checkpoint and start from the first page")
    parser.add_argument("--no-clean", action="store_true",
                        help="skip running clean.py on the scraped entries")
    parser.add_argument("--no-cache-html", action="store_true",
                        help="do not keep a copy of each fetched page under <data-dir>/raw_html")
    parser.add_argument("--reparse-cache", action="store_true",
                        help="skip the network and rebuild the raw entries from <data-dir>/raw_html")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    script_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = script_dir / data_dir
    _configure_logging(data_dir / "scrape.log")

    raw_output = Path(args.raw_output) if args.raw_output else data_dir / "raw_entries.json"
    output = Path(args.output)
    if not output.is_absolute():
        output = script_dir / output

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
    except ScrapeNetworkError:
        entries = scraper.entries
        exit_code = 3
        logger.error("Network failure. Progress is saved; re-run later to resume.")
    except KeyboardInterrupt:
        entries = scraper.entries
        exit_code = 130

    save_data(entries, raw_output)
    logger.info("Saved %d raw entries to %s", len(entries), raw_output)
    # The plain raw file is ~50 MB at 30k entries, so the copy kept in git is
    # the gzip one; clean.py reads either.
    gzip_output = raw_output.with_name(raw_output.name + ".gz")
    save_data(entries, gzip_output)
    logger.info("Saved gzip copy to %s", gzip_output)

    if not args.no_clean and entries:
        from clean import clean_data  # local import avoids a circular import at module load

        cleaned = clean_data(entries)
        save_data(cleaned, output)
        logger.info("Saved %d cleaned entries to %s", len(cleaned), output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
