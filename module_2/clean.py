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
    """Build one structured record from a raw listing entry.

    The visible listing text is the primary source.  The page's embedded JSON
    record (``listing_json``, when the scraper captured it) supplies the full
    decision date and fills any field the badges did not show.
    """
    listing = raw.get("listing_json") or {}

    university = _clean_text(raw.get("school_text")) or _clean_text(listing.get("school"))
    program_name = _clean_text(raw.get("program_text")) or _clean_text(listing.get("program"))
    degree = _clean_text(raw.get("degree_text")) or _clean_text(listing.get("level"))
    date_added_text = _clean_text(raw.get("date_added_text")) or _clean_text(listing.get("added_on_label"))
    decision_text = _clean_text(raw.get("decision_text")) or _clean_text(listing.get("decision_label"))
    comment = _clean_text(raw.get("comment_text"), keep_newlines=True) or _clean_text(
        listing.get("notes"), keep_newlines=True
    )
    tags = [t for t in (_clean_text(tag) for tag in raw.get("tags_text") or []) if t]

    date_added = _parse_date_added(date_added_text) or _parse_iso_date(listing.get("created_at"))
    status, inferred_decision_date = _parse_decision(decision_text, date_added)
    if status is None:
        status = _normalize_status(_clean_text(listing.get("decision")))

    # Exact decision date from the page payload beats the month/day badge.
    exact_decision_date = _parse_iso_date(listing.get("date_of_notification"))
    if exact_decision_date:
        decision_date, decision_date_source = exact_decision_date, "site_json"
    elif inferred_decision_date:
        decision_date, decision_date_source = inferred_decision_date, "badge_year_inferred"
    else:
        decision_date, decision_date_source = None, None

    metrics = _classify_tags(tags)
    term = metrics["term"] or _parse_term(_clean_text(listing.get("season")))
    applicant_type = metrics["applicant_type"] or _parse_applicant_type(_clean_text(listing.get("status")))
    gpa = _first_present(metrics["gpa"], _to_number_or_none(listing.get("ugpa")))
    gre = _first_present(metrics["gre"], _to_number_or_none(listing.get("greq")))
    gre_verbal = _first_present(metrics["gre_verbal"], _to_number_or_none(listing.get("grev")))
    gre_aw = _first_present(metrics["gre_analytical_writing"], _to_number_or_none(listing.get("grew")))

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
        "decision_date_source": decision_date_source,
        "term": term,
        "applicant_type": applicant_type,
        "gpa": gpa,
        "gre": gre,
        "gre_verbal": gre_verbal,
        "gre_analytical_writing": gre_aw,
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
            "date_of_notification": listing.get("date_of_notification"),
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


def _first_present(*values: object) -> object:
    """Return the first value that is not None (all None -> None)."""
    for value in values:
        if value is not None:
            return value
    return None


def _parse_iso_date(value: object) -> str | None:
    """'2026-07-10T00:00:00.000000Z' or '2026-07-10' -> '2026-07-10'."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _parse_term(text: str | None) -> str | None:
    """'fall 2026' -> 'Fall 2026'; anything else -> None."""
    if not text:
        return None
    match = TERM_PATTERN.match(text)
    return f"{match.group(1).title()} {match.group(2)}" if match else None


def _parse_applicant_type(text: str | None) -> str | None:
    """'International' / 'American' (any case) -> canonical label; else None."""
    if not text:
        return None
    return text.title() if APPLICANT_TYPE_PATTERN.match(text) else None


def _to_number_or_none(value: object) -> int | float | None:
    """Numeric conversion for JSON payload values that may be None or strings."""
    if value is None or value == "":
        return None
    return _to_number(str(value).strip())


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
            result["term"] = _parse_term(tag)
        elif APPLICANT_TYPE_PATTERN.match(tag):
            result["applicant_type"] = _parse_applicant_type(tag)
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


def _default_raw_input(script_dir: Path) -> Path:
    """data/raw_entries.json if present, else the gzip copy that is kept in git."""
    plain = script_dir / "data" / "raw_entries.json"
    return plain if plain.exists() else plain.with_name(plain.name + ".gz")


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Clean raw Grad Cafe entries into applicant_data.json")
    parser.add_argument("--input", default=str(_default_raw_input(script_dir)),
                        help="raw entries JSON (or .json.gz) produced by scrape.py")
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
