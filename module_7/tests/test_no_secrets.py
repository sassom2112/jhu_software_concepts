"""
No AWS keys or private keys anywhere in module_7.

This also covers a notebook saved with its outputs: if a cell ever printed a
key, this test fails before the notebook is committed.  Files that hold
secrets on purpose never belong in module_7 (the root .gitignore keeps out
.env files, *.pem keys and the access key CSV files), so they are skipped here
rather than checked.
"""

from __future__ import annotations

import re
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
# Virtual environments hold third-party example keys (botocore's IAM examples, for one)
# and never belong in the submission; "site-packages" catches one with any other name.
SKIPPED_DIRS = {".venv", "venv", "env", "site-packages", "__pycache__", ".pytest_cache",
                ".ipynb_checkpoints"}
SKIPPED_NAMES = {".env", "applicant_data.json", "applicant_data_SM.json"}   # 36 MB data
TEXT_SUFFIXES = {".py", ".ipynb", ".md", ".txt", ".yml", ".yaml", ".ini", ".example", ".json"}

PATTERNS = {
    "AWS access key ID": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS secret access key": re.compile(
        r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+]{40}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def _text_files() -> list[Path]:
    """Every text file in module_7 that could end up in git."""
    found = []
    for path in sorted(MODULE_DIR.rglob("*")):
        if not path.is_file() or SKIPPED_DIRS.intersection(path.relative_to(MODULE_DIR).parts):
            continue
        if path.name in SKIPPED_NAMES or path.suffix not in TEXT_SUFFIXES:
            continue
        found.append(path)
    return found


def test_the_scan_sees_the_deliverables():
    """A scan that found nothing to read would pass for the wrong reason."""
    names = {path.name for path in _text_files()}
    assert {"s3_fetch.py", "grad-cafe-pipeline.ipynb", "README.md",
            "docker-compose.ec2.yml", "EC2_DEPLOYMENT.md"} <= names


def test_no_keys_in_module_7():
    """No access key ID, secret access key or private key in any text file."""
    problems = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                problems.append(f"{path.relative_to(MODULE_DIR)}: looks like an {label}")
    assert not problems, problems


def test_the_patterns_do_catch_keys():
    """The patterns match keys of the real shapes (AWS's documented example key).  The
    samples are put together from pieces so that this file does not match itself."""
    assert PATTERNS["AWS access key ID"].search("id " + "AKIA" + "IOSFODNN7EXAMPLE" + " here")
    assert PATTERNS["AWS secret access key"].search(
        "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY")
    assert PATTERNS["private key"].search("-----BEGIN RSA " + "PRIVATE KEY-----")
