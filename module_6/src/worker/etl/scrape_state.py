"""
scrape_state.py - Where a Grad Cafe scrape keeps its files, and what it has collected.

JHU EN.605.256 Modern Software Concepts in Python - Module 5 (split out of
scrape.py, Module 2).

ScrapeFiles names the progress files a run keeps in its data folder: the
progress log, the checkpoint, the copy of robots.txt and the cached pages.
ScrapeProgress is the in-memory side of the same progress: the entries
collected so far, their result ids (for de-duplication) and the number of
listing pages fetched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ScrapeFiles:
    """The data folder of one scraper and the files it keeps there."""

    data_dir: Path
    cache_html: bool = True  # keep a copy of every fetched page under html_cache_dir

    @property
    def html_cache_dir(self) -> Path:
        """Copies of the fetched listing pages (page_00001.html, ...)."""
        return self.data_dir / "raw_html"

    @property
    def jsonl_path(self) -> Path:
        """Progress log: every new raw entry, one JSON object per line."""
        return self.data_dir / "raw_entries.jsonl"

    @property
    def checkpoint_path(self) -> Path:
        """Where the next run continues (next_url, pages_fetched, finished)."""
        return self.data_dir / "checkpoint.json"

    @property
    def robots_copy_path(self) -> Path:
        """The robots.txt text the run obeyed, kept as evidence."""
        return self.data_dir / "robots.txt"


@dataclass
class ScrapeProgress:
    """Entries collected so far (in fetch order), their result ids, and pages fetched."""

    entries: list[dict] = field(default_factory=list)
    seen_ids: set[int] = field(default_factory=set)
    pages_fetched: int = 0

    def add_new(self, entries: list[dict]) -> list[dict]:
        """Append entries whose result_id has not been seen; return those added."""
        added: list[dict] = []
        for entry in entries:
            result_id = entry["result_id"]
            if result_id in self.seen_ids:
                continue
            self.seen_ids.add(result_id)
            self.entries.append(entry)
            added.append(entry)
        return added

    def forget_entries(self) -> None:
        """Drop every collected entry (the same list object is emptied, not replaced)."""
        self.entries.clear()
        self.seen_ids.clear()
