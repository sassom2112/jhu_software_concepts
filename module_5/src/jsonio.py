"""
jsonio.py - JSON files and command-line file names shared by the ETL scripts.

JHU EN.605.256 Modern Software Concepts in Python - Module 5.

scrape.py, clean.py and load_data.py all read and write lists of entry dicts
as UTF-8 JSON (plain or gzip-compressed), and all reduce a command-line option
to a bare name inside the module folder.  These helpers live in their own
module so each script can import them without importing another script
(clean.py and scrape.py used to import each other for them).  scrape.py
re-exports save_data and load_data, so ``from scrape import save_data`` still
works.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path


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


def local_name(value: str | Path) -> str:
    """Reduce a command-line path to a bare file or folder name.

    Everything the command-line tools read or write lives inside the module
    folder, so only the last path component of an option is used: "../../etc"
    collapses to "etc" and still lands inside the module.  Empty names are
    refused.
    """
    name = os.path.basename(os.path.normpath(str(value)))
    if not name or name in (".", ".."):
        raise ValueError(f"not a usable file or folder name: {value!r}")
    return name
