"""
clean.py - Convert raw Grad Cafe listing entries into structured records.

JHU EN.605.256 Modern Software Concepts in Python - Module 2.

Input : the raw entry dicts produced by scrape.py (visible listing text only).
Output: one dict per applicant with typed, consistently named fields.  Every
        raw string is carried along unchanged under the "raw" key so any cleaned
        value can be traced back to what the website showed.

Missing or unavailable values are always represented as None (JSON null).

Usage:
    python clean.py                      # data/raw_entries.json -> applicant_data.json
    python clean.py --input X --output Y
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date, datetime
from pathlib import Path

from scrape import load_data, save_data

# --------------------------------------------------------------------------- #
# Patterns for the badge ("tag") texts shown under each listing row
# --------------------------------------------------------------------------- #

TERM_PATTERN = re.compile(r"^(Fall|Spring|Summer|Winter)\s+(\d{4})$", re.IGNORECASE)
APPLICANT_TYPE_PATTERN = re.compile(r"^(International|American)$", re.IGNORECASE)
GRE_AW_PATTERN = re.compile(r"^GRE\s*AW\s*:?\s*([\d.]+)$", re.IGNORECASE)
GRE_V_PATTERN = re.compile(r"^GRE\s*V(?:erbal)?\s*:?\s*([\d.]+)$", re.IGNORECASE)
GRE_PATTERN = re.compile(r"^GRE\s*(?:General|Q(?:uant)?)?\s*:?\s*([\d.]+)$", re.IGNORECASE)
GPA_PATTERN = re.compile(r"^GPA\s*:?\s*([\d.]+)$", re.IGNORECASE)

# "Accepted on Sep 09", "Rejected on 12 Mar", "Wait listed on Sep 10",
# "Interview on Jul 10", "Other"
DECISION_PATTERN = re.compile(
    r"^(?P<status>Accepted|Rejected|Wait\s*listed|Waitlisted|Interview(?:ed)?|Other)"
    r"(?:\s+on\s+(?P<date>.+))?$",
    re.IGNORECASE,
)
STATUS_LABELS = {
    "accepted": "Accepted",
    "rejected": "Rejected",
    "waitlisted": "Waitlisted",
    "wait listed": "Waitlisted",
    "interview": "Interview",
    "interviewed": "Interview",
    "other": "Other",
}

HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
DATE_ADDED_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d")
DECISION_DATE_FORMATS = ("%b %d", "%d %b", "%B %d", "%d %B")  # year is not shown on the listing


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def clean_data(raw_entries: list[dict]) -> list[dict]:
    """Convert every raw listing entry into a structured applicant record."""
    cleaned = [_clean_entry(entry) for entry in raw_entries]
    cleaned.sort(key=lambda record: record["result_id"] or 0, reverse=True)
    return cleaned


# --------------------------------------------------------------------------- #
# Per-entry cleaning
# --------------------------------------------------------------------------- #


def _clean_entry(raw: dict) -> dict:
    university = _clean_text(raw.get("school_text"))
    program_name = _clean_text(raw.get("program_text"))
    degree = _clean_text(raw.get("degree_text"))
    date_added_text = _clean_text(raw.get("date_added_text"))
    decision_text = _clean_text(raw.get("decision_text"))
    comment = _clean_text(raw.get("comment_text"), keep_newlines=True)
    tags = [t for t in (_clean_text(tag) for tag in raw.get("tags_text") or []) if t]

    date_added = _parse_date_added(date_added_text)
    status, decision_date = _parse_decision(decision_text, date_added)
    metrics = _classify_tags(tags)

    return {
        "result_id": raw.get("result_id"),
        "url": raw.get("url"),
        # "program" mirrors the legacy Grad Cafe listing text ("Program, University")
        # that the LLM standardizer expects; the two parts are also kept separately.
        "program": _join_program(program_name, university),
        "program_name": program_name,
        "university": university,
        "degree": degree,
        "date_added": date_added,
        "status": status,
        "decision_date": decision_date,
        "term": metrics["term"],
        "applicant_type": metrics["applicant_type"],
        "gpa": metrics["gpa"],
        "gre": metrics["gre"],
        "gre_verbal": metrics["gre_verbal"],
        "gre_analytical_writing": metrics["gre_analytical_writing"],
        "comments": comment,
        "other_tags": metrics["other_tags"],
        "raw": {
            "school": raw.get("school_text"),
            "program": raw.get("program_text"),
            "degree": raw.get("degree_text"),
            "date_added": raw.get("date_added_text"),
            "decision": raw.get("decision_text"),
            "tags": list(raw.get("tags_text") or []),
            "comment": raw.get("comment_text"),
            "scraped_at": raw.get("scraped_at"),
        },
    }


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #


def _clean_text(value: object, keep_newlines: bool = False) -> str | None:
    """Decode HTML entities, drop any leftover tags, collapse whitespace."""
    if value is None:
        return None
    text = html.unescape(str(value))
    text = HTML_TAG_PATTERN.sub(" ", text)
    if keep_newlines:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
    else:
        text = " ".join(text.split())
    return text or None


def _join_program(program_name: str | None, university: str | None) -> str | None:
    parts = [part for part in (program_name, university) if part]
    return ", ".join(parts) if parts else None


def _parse_date_added(text: str | None) -> str | None:
    """'Sep 11, 2026' -> '2026-09-11'.  Unknown formats return None."""
    if not text:
        return None
    for fmt in DATE_ADDED_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize_status(status_text: str | None) -> str | None:
    if not status_text:
        return None
    key = " ".join(status_text.lower().split())
    return STATUS_LABELS.get(key, status_text.strip().title())


def _parse_decision(decision_text: str | None, date_added: str | None) -> tuple[str | None, str | None]:
    """Split 'Accepted on Sep 09' into ('Accepted', '2026-09-09').

    The listing shows the decision's month and day but not its year.  The year
    is inferred from the 'date added' year: a decision cannot post-date the
    entry it belongs to, so if the month/day falls after the date-added
    month/day the decision is assumed to be from the previous year.
    """
    if not decision_text:
        return None, None
    match = DECISION_PATTERN.match(decision_text)
    if not match:
        return _normalize_status(decision_text), None
    status = _normalize_status(match.group("status"))
    date_part = match.group("date")
    if not date_part:
        return status, None
    return status, _resolve_decision_date(date_part.strip(), date_added)


def _resolve_decision_date(month_day_text: str, date_added: str | None) -> str | None:
    parsed = None
    for fmt in DECISION_DATE_FORMATS:
        try:
            parsed = datetime.strptime(month_day_text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        # Some rows may carry a full date ("Sep 09, 2026"); accept that too.
        for fmt in DATE_ADDED_FORMATS:
            try:
                return datetime.strptime(month_day_text, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    if not date_added:
        return None  # no reference year available; leave the date unknown
    added = date.fromisoformat(date_added)
    year = added.year
    if (parsed.month, parsed.day) > (added.month, added.day):
        year -= 1
    try:
        return date(year, parsed.month, parsed.day).isoformat()
    except ValueError:  # e.g. Feb 29 in a non-leap year
        return None


def _to_number(text: str) -> int | float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() and "." not in text else value


def _classify_tags(tags: list[str]) -> dict:
    """Sort the badge texts into typed fields; unknown badges go to other_tags."""
    result: dict = {
        "term": None,
        "applicant_type": None,
        "gpa": None,
        "gre": None,
        "gre_verbal": None,
        "gre_analytical_writing": None,
        "other_tags": [],
    }
    for tag in tags:
        if TERM_PATTERN.match(tag):
            match = TERM_PATTERN.match(tag)
            result["term"] = f"{match.group(1).title()} {match.group(2)}"
        elif APPLICANT_TYPE_PATTERN.match(tag):
            result["applicant_type"] = tag.title()
        elif GRE_AW_PATTERN.match(tag):
            result["gre_analytical_writing"] = _to_number(GRE_AW_PATTERN.match(tag).group(1))
        elif GRE_V_PATTERN.match(tag):
            result["gre_verbal"] = _to_number(GRE_V_PATTERN.match(tag).group(1))
        elif GRE_PATTERN.match(tag):
            result["gre"] = _to_number(GRE_PATTERN.match(tag).group(1))
        elif GPA_PATTERN.match(tag):
            result["gpa"] = _to_number(GPA_PATTERN.match(tag).group(1))
        elif DECISION_PATTERN.match(tag):
            continue  # decision badge duplicated in the tag row; already captured
        else:
            result["other_tags"].append(tag)
    return result


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Clean raw Grad Cafe entries into applicant_data.json")
    parser.add_argument("--input", default=str(script_dir / "data" / "raw_entries.json"),
                        help="raw entries JSON produced by scrape.py")
    parser.add_argument("--output", default=str(script_dir / "applicant_data.json"),
                        help="destination for the cleaned JSON")
    args = parser.parse_args(argv)

    raw_entries = load_data(args.input)
    cleaned = clean_data(raw_entries)
    save_data(cleaned, args.output)
    print(f"Cleaned {len(cleaned)} entries -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
