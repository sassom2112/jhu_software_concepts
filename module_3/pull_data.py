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
     2 s delays and stop-on-block apply).
  3. clean.clean_data() turns the new raw entries into cleaned records.
  4. llm_hosting/run_parallel.py adds llm-generated-program / -university
     using the local model (skipped, with a message, if llm_hosting/.venv is
     missing; the rows are then stored with empty LLM fields).
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
import subprocess
import sys
import traceback
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX (Linux, macOS, WSL)
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback below
    fcntl = None

import psycopg

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
LOCK_PATH = DATA_DIR / "pull_data.lock"
STATUS_PATH = DATA_DIR / "pull_status.json"
LOG_PATH = DATA_DIR / "pull_data.log"
LLM_DIR = HERE / "llm_hosting"
NEW_CLEAN_NAME = "pull_new_applicants.json"
NEW_LLM_NAME = "pull_new_applicants_llm.json"

DEFAULT_MAX_PAGES = 50
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_LLM_WORKERS = 8
DEFAULT_LLM_THREADS = 2

logger = logging.getLogger("gradcafe.pull")


# --------------------------------------------------------------------------- #
#                         Status file and single-run lock                     #
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_status() -> dict:
    """Last recorded Pull Data status ({} if there has never been a run)."""
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(state: str, message: str, **details) -> None:
    """Atomically record the current state: running, succeeded or failed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    previous = read_status()
    payload = {
        "state": state,
        "message": message,
        "started_at": details.pop("started_at", previous.get("started_at") if state == "running" else None),
        "updated_at": _now(),
        **details,
    }
    if payload["started_at"] is None:
        payload["started_at"] = previous.get("started_at")
    temp = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(STATUS_PATH)


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
        if fcntl is None:  # Windows: best effort via exclusive creation of a marker
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


def _llm_python() -> Path | None:
    """Interpreter of the llm_hosting environment (LLM_PYTHON overrides), if present."""
    override = os.environ.get("LLM_PYTHON")
    candidate = Path(override) if override else LLM_DIR / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def _standardize(cleaned: list[dict], workers: int, threads: int) -> tuple[list[dict], str]:
    """Add the LLM-generated fields; returns (records, note for the status message)."""
    from scrape import load_data as load_json, save_data

    python = _llm_python()
    if python is None:
        return cleaned, (" The local LLM environment (llm_hosting/.venv) was not found, so the new rows "
                         "were stored without LLM-generated program and university names.")
    save_data(cleaned, HERE / NEW_CLEAN_NAME)
    command = [str(python), "run_parallel.py", "--input", NEW_CLEAN_NAME, "--output", NEW_LLM_NAME,
               "--workers", str(workers), "--threads", str(threads)]
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        log.write(f"\n[{_now()}] running: {' '.join(command)}\n")
        log.flush()
        completed = subprocess.run(command, cwd=LLM_DIR, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        return cleaned, (" The LLM standardization step failed (see data/pull_data.log), so the new rows "
                         "were stored without LLM-generated names.")
    return load_json(HERE / NEW_LLM_NAME), ""


def run_pull(max_pages: int, delay: float, workers: int, threads: int) -> int:
    """Run the whole pipeline; always leaves a final status behind."""
    from clean import clean_data
    from load_data import connect, count_rows, create_table, load_records
    from scrape import GradCafeScraper, ScrapeBlockedError, ScrapeNetworkError

    started = _now()
    write_status("running", "Checking which entries are already in the database...", started_at=started, step="database")

    try:
        with connect() as conn:
            create_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT p_id FROM applicants")
                known_ids = {row[0] for row in cur}
    except psycopg.Error as err:
        write_status("failed", f"Could not read the database, so nothing was fetched. ({err.__class__.__name__})",
                     finished_at=_now())
        logger.error("database error: %s", err)
        return 2

    write_status("running", f"Checking Grad Café for entries newer than the {len(known_ids):,} already stored...",
                 step="scrape")

    def progress(pages: int, found: int) -> None:
        write_status("running", f"Checked {pages} listing page(s) on Grad Café; {found:,} new entries found so far...",
                     step="scrape", pages=pages, new_entries=found)

    scraper = GradCafeScraper(data_dir=DATA_DIR, delay_seconds=delay, cache_html=False)
    try:
        new_raw, pages = scraper.scrape_new_entries(known_ids, max_pages=max_pages, progress=progress)
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

    if not new_raw:
        write_status("succeeded", f"No new entries: the database already contains everything currently listed "
                     f"on Grad Café (checked {pages} page(s)).", finished_at=_now(), inserted=0, pages=pages)
        return 0

    cleaned = clean_data(new_raw)
    write_status("running", f"Found {len(cleaned):,} new entries. Standardizing program and university names "
                 "with the local LLM (this can take a few minutes)...", step="llm", pages=pages,
                 new_entries=len(cleaned))
    records, llm_note = _standardize(cleaned, workers, threads)

    write_status("running", f"Adding {len(records):,} new entries to the database...", step="load")
    try:
        with connect() as conn:
            inserted, present, unusable = load_records(conn, records)
            total = count_rows(conn)
    except psycopg.Error as err:
        write_status("failed", f"The new entries could not be saved; the database was left unchanged. "
                     f"({err.__class__.__name__})", finished_at=_now())
        logger.error("load error: %s", err)
        return 2

    write_status("succeeded", f"Added {inserted:,} new entries from {pages} Grad Café page(s). "
                 f"The database now holds {total:,} entries. Click Update Analysis to see the new results."
                 f"{llm_note}", finished_at=_now(), inserted=inserted, already_present=present,
                 unusable=unusable, total=total, pages=pages)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add newly posted Grad Café entries to PostgreSQL")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"never fetch more than this many listing pages (default {DEFAULT_MAX_PAGES})")
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
