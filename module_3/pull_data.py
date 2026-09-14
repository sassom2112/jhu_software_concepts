"""
pull_data.py - Fetch newly posted Grad Café entries and add them to PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

This is the program behind the web page's "Pull Data" button (the Flask app
starts it as a subprocess), and it can also be run by hand:

    python pull_data.py

Pipeline, reusing the Module 2 code:
  1. Read the p_ids already stored in the applicants table.
  2. GradCafeScraper.scrape_new_entries() walks the newest listing pages and
     stops as soon as it reaches an entry that is already stored (robots.txt,
     2 s delays and stop-on-block apply).  One run fetches at most
     --max-pages pages; if that limit is reached first, the position is saved
     in data/pull_resume.json and the next run continues from there, so no gap
     of missing entries is ever left behind.
  3. clean.clean_data() turns the new raw entries into cleaned records.
  4. llm_hosting/run_parallel.py adds llm-generated-program / -university
     using the local model (skipped with a message if llm_hosting/.venv is
     missing, and stopped after 30 minutes if it hangs; the rows are then
     stored with empty LLM fields).
  5. load_data.load_records() inserts them with ON CONFLICT (p_id) DO NOTHING,
     so existing rows are never overwritten.

Only one run can be active: the run holds an exclusive lock on
data/pull_data.lock for its whole lifetime, and a second start exits at once.
Progress and the final outcome are written to data/pull_status.json, which
the web page reads.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import traceback
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX (Linux, macOS, WSL)
    import fcntl
except ImportError:  # pragma: no cover - native Windows
    fcntl = None

import psycopg

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
LOCK_PATH = DATA_DIR / "pull_data.lock"
STATUS_PATH = DATA_DIR / "pull_status.json"
RESUME_PATH = DATA_DIR / "pull_resume.json"
LOG_PATH = DATA_DIR / "pull_data.log"
LLM_DIR = HERE / "llm_hosting"
LLM_PYTHON = LLM_DIR / ".venv" / "bin" / "python"
NEW_CLEAN_NAME = "pull_new_applicants.json"
NEW_LLM_NAME = "pull_new_applicants_llm.json"

DEFAULT_MAX_PAGES = 50
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_LLM_WORKERS = 8
DEFAULT_LLM_THREADS = 2
LLM_TIMEOUT_SECONDS = 30 * 60

logger = logging.getLogger("gradcafe.pull")


# --------------------------------------------------------------------------- #
#                  Status file, resume positions, single-run lock             #
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomically(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def read_status() -> dict:
    """Last recorded Pull Data status ({} if there has never been a run)."""
    try:
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def write_status(state: str, message: str, **details) -> None:
    """Atomically record the current state: running, succeeded or failed."""
    started_at = details.pop("started_at", None) or read_status().get("started_at")
    _write_json_atomically(STATUS_PATH, {"state": state, "message": message, "started_at": started_at,
                                         "updated_at": _now(), **details})


def _read_resume_urls() -> list[str]:
    """Listing pages where earlier runs stopped because they reached --max-pages."""
    try:
        urls = json.loads(RESUME_PATH.read_text(encoding="utf-8")).get("resume_urls", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [url for url in urls if isinstance(url, str)]


def _write_resume_urls(urls: list[str]) -> None:
    if urls:
        _write_json_atomically(RESUME_PATH, {"resume_urls": urls, "updated_at": _now()})
    elif RESUME_PATH.exists():
        RESUME_PATH.unlink()


class PullLock:
    """Exclusive, non-blocking lock held for the lifetime of a pull.

    The operating system releases the lock when the process exits, even after
    a crash, so a stale lock can never block future runs.
    """

    def __init__(self) -> None:
        self._handle = None

    def acquire(self) -> bool:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._handle = open(LOCK_PATH, "a+", encoding="utf-8")
        if fcntl is None:
            # Native Windows has no fcntl: only the web app's in-process guard
            # prevents a second run there (documented in the README).
            return True
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            return False
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle is not None:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def is_pull_running() -> bool:
    """True while some process holds the Pull Data lock."""
    if fcntl is None or not LOCK_PATH.exists():
        return False
    with open(LOCK_PATH, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


# --------------------------------------------------------------------------- #
#                                  Pipeline                                   #
# --------------------------------------------------------------------------- #


def _stop_process_group(process: subprocess.Popen) -> None:
    """Terminate a child started with start_new_session, including its worker processes."""
    for sig, grace_seconds in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError, AttributeError):
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            continue


def _standardize(cleaned: list[dict], workers: int, threads: int) -> tuple[list[dict], str]:
    """Add the LLM-generated fields; returns (records, note for the status message)."""
    from scrape import load_data as load_json, save_data

    if not LLM_PYTHON.exists():
        return cleaned, (" The local LLM environment (llm_hosting/.venv) was not found, so the new rows "
                         "were stored without LLM-generated program and university names (see README).")
    save_data(cleaned, HERE / NEW_CLEAN_NAME)
    command = [str(LLM_PYTHON), "run_parallel.py", "--input", NEW_CLEAN_NAME, "--output", NEW_LLM_NAME,
               "--workers", str(int(workers)), "--threads", str(int(threads))]
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        log.write(f"\n[{_now()}] running: {' '.join(command)}\n")
        log.flush()
        process = subprocess.Popen(command, cwd=LLM_DIR, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            returncode = process.wait(timeout=LLM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
            return cleaned, (f" The LLM step did not finish within {LLM_TIMEOUT_SECONDS // 60} minutes and was "
                             "stopped, so the new rows were stored without LLM-generated names.")
    if returncode != 0:
        return cleaned, (" The LLM standardization step failed (see data/pull_data.log), so the new rows "
                         "were stored without LLM-generated names.")
    return load_json(HERE / NEW_LLM_NAME), ""


def run_pull(max_pages: int, delay: float, workers: int, threads: int) -> int:
    """Run the whole pipeline; always leaves a final status behind."""
    from clean import clean_data
    from load_data import connect, count_rows, create_table, load_records
    from scrape import GradCafeScraper, ScrapeBlockedError, ScrapeNetworkError

    write_status("running", "Checking which entries are already in the database...", started_at=_now(),
                 step="database")
    try:
        with connect() as conn:
            create_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT p_id FROM applicants")
                known_ids = {row[0] for row in cur}
    except psycopg.Error as err:
        write_status("failed", "Could not read the database, so nothing was fetched. Check that PostgreSQL is "
                     f"running. ({err.__class__.__name__})", finished_at=_now())
        logger.error("database error: %s", err)
        return 2

    write_status("running", f"Checking Grad Café for entries newer than the {len(known_ids):,} already stored...",
                 step="scrape")
    scraper = GradCafeScraper(data_dir=DATA_DIR, delay_seconds=delay, cache_html=False)
    seen = set(known_ids)
    new_raw: list[dict] = []
    pages_total = 0
    unfinished: list[str] = []  # listing pages to continue from in the next run
    try:
        # First the newest pages, then any positions earlier runs could not finish.
        for start_url in [None, *_read_resume_urls()]:
            budget = max_pages - pages_total
            if budget <= 0:
                if start_url:
                    unfinished.append(start_url)
                continue
            pages_before, found_before = pages_total, len(new_raw)

            def progress(pages: int, found: int, pages_before=pages_before, found_before=found_before) -> None:
                write_status("running", f"Checked {pages_before + pages} listing page(s) on Grad Café; "
                             f"{found_before + found:,} new entries found so far...", step="scrape",
                             pages=pages_before + pages, new_entries=found_before + found)

            entries, pages, resume_url = scraper.scrape_new_entries(seen, max_pages=budget, progress=progress,
                                                                     start_url=start_url)
            pages_total += pages
            seen.update(entry["result_id"] for entry in entries)
            new_raw.extend(entries)
            if resume_url:
                unfinished.append(resume_url)
    except ScrapeBlockedError as err:
        write_status("failed", "Grad Café refused or rate-limited the request, so the pull stopped immediately "
                     "without retrying. No data was changed; please try again later. "
                     f"(Details: {err})", finished_at=_now())
        return 3
    except ScrapeNetworkError as err:
        write_status("failed", f"Grad Café could not be reached (network error). No data was changed. ({err})",
                     finished_at=_now())
        return 3
    except (PermissionError, urllib.error.HTTPError) as err:
        write_status("failed", f"The pull stopped before fetching entries: {err}. No data was changed.",
                     finished_at=_now())
        return 3

    more_note = ""
    if unfinished:
        more_note = (f" Grad Café has more new entries than one run fetches ({max_pages} pages), so this run "
                     "stopped early; click Pull Data again to continue where it stopped.")

    if not new_raw:
        _write_resume_urls(unfinished)
        write_status("succeeded", f"No new entries: the database already contains everything currently listed "
                     f"on Grad Café (checked {pages_total} page(s)).{more_note}", finished_at=_now(), inserted=0,
                     pages=pages_total)
        return 0

    cleaned = clean_data(new_raw)
    write_status("running", f"Found {len(cleaned):,} new entries. Standardizing program and university names "
                 "with the local LLM (this can take a few minutes)...", step="llm", pages=pages_total,
                 new_entries=len(cleaned))
    records, llm_note = _standardize(cleaned, workers, threads)

    write_status("running", f"Adding {len(records):,} new entries to the database...", step="load")
    try:
        with connect() as conn:
            inserted, present, unusable = load_records(conn, records)
            total = count_rows(conn)
    except psycopg.Error as err:
        write_status("failed", "The new entries could not be saved; the database was left unchanged. "
                     f"({err.__class__.__name__})", finished_at=_now())
        logger.error("load error: %s", err)
        return 2

    _write_resume_urls(unfinished)  # only once the new rows are safely stored
    write_status("succeeded", f"Added {inserted:,} new entries from {pages_total} Grad Café page(s). "
                 f"The database now holds {total:,} entries. Click Update Analysis to see the new results."
                 f"{more_note}{llm_note}", finished_at=_now(), inserted=inserted, already_present=present,
                 unusable=unusable, total=total, pages=pages_total, more_available=bool(unfinished))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add newly posted Grad Café entries to PostgreSQL")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"fetch at most this many listing pages per run (default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                        help="seconds between page requests (default 2.0)")
    parser.add_argument("--workers", type=int, default=DEFAULT_LLM_WORKERS, help="LLM worker processes")
    parser.add_argument("--threads", type=int, default=DEFAULT_LLM_THREADS, help="CPU threads per LLM worker")
    args = parser.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Log to the file; also to the terminal when run by hand (the web app already
    # redirects this process's output into the same log file).
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    lock = PullLock()
    if not lock.acquire():
        print("A Pull Data run is already in progress; not starting another one.")
        return 0
    try:
        return run_pull(args.max_pages, args.delay, args.workers, args.threads)
    except Exception as err:  # the status file must never be left saying "running"
        logger.error("unexpected error: %s\n%s", err, traceback.format_exc())
        write_status("failed", f"Pull Data stopped because of an unexpected error ({err.__class__.__name__}). "
                     "No partial data was saved; see data/pull_data.log.", finished_at=_now())
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
