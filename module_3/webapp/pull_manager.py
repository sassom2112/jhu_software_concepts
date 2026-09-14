"""
pull_manager.py - Start and observe the Pull Data subprocess from Flask.

The web process never scrapes itself.  It starts `python pull_data.py` in the
background and reads the lock and status file that pull_data.py maintains, so:
  * only one pull can run at a time (checked here with a thread lock and the
    OS-level file lock that the pull process holds);
  * the page can report progress and the final outcome even after the web
    server restarts;
  * "Update Analysis" only reads the database and never interferes with a pull.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pull_data

MODULE_DIR = Path(pull_data.__file__).resolve().parent


class PullManager:
    """Process-wide coordinator for the Pull Data button."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._process: subprocess.Popen | None = None

    def is_running(self) -> bool:
        """True while a pull started here, or anywhere else on this machine, is active."""
        if self._process is not None and self._process.poll() is None:
            return True
        return pull_data.is_pull_running()

    def start(self) -> tuple[bool, str]:
        """Start a pull unless one is already running; returns (started, user message)."""
        with self._guard:
            if self.is_running():
                return False, ("Pull Data is already running. New data is currently being retrieved; "
                               "a second pull was not started.")
            pull_data.write_status("running", "Starting Pull Data...",
                                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   step="starting")
            pull_data.DATA_DIR.mkdir(parents=True, exist_ok=True)
            log = open(pull_data.LOG_PATH, "a", encoding="utf-8")
            self._process = subprocess.Popen(
                [sys.executable, str(MODULE_DIR / "pull_data.py")],
                cwd=MODULE_DIR,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # a browser refresh or server reload does not kill the pull
            )
            log.close()
            return True, ("Pull Data started. Grad Café is being checked for new entries; this page shows "
                          "the progress. You can keep using Update Analysis while it runs.")

    def status(self) -> dict:
        """Status for display, correcting a 'running' record whose process has died."""
        status = pull_data.read_status()
        running = self.is_running()
        if status.get("state") == "running" and not running:
            status = {**status, "state": "failed",
                      "message": "The last Pull Data run stopped unexpectedly before finishing. No partial "
                                 "data was saved; you can start it again."}
        status["running"] = running
        return status


manager = PullManager()
