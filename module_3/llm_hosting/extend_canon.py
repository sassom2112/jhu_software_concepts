"""
extend_canon.py - Grow the canonical lists from the names that occur in the data.

JHU EN.605.256 Module 2 - added by the student.

The instructor's canon_universities.txt / canon_programs.txt are US-centric
starter lists.  This script reads the cleaned applicant records, runs every
distinct university and program name through app.py's own post-processing
(abbreviation expansion, spelling fixes, canonical/fuzzy matching) and, for
the names that still do not resolve to a canonical entry, appends the name as
it appears on Grad Cafe (after the same abbreviation handling) to the list,
provided it occurs at least --min-count times.  Exact canonical matches win
over fuzzy ones in app.py, so every added name also stops the fuzzy matcher
from mapping it onto a look-alike ("University of Michigan" -> "University of
Milan").

Usage (from module_2/llm_hosting):
    python extend_canon.py --input applicant_data.json --min-count 2
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

import app  # the instructor's module: canon lists, fix maps, post-normalizers

HERE = Path(__file__).resolve().parent


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


def _append_lines(path: Path, names: list[str]) -> None:
    """Append names to a canon file, one per line, keeping the file newline-terminated."""
    existing = path.read_text(encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for name in names:
            handle.write(name + "\n")


def _looks_like_fragment(name: str) -> bool:
    """Acronyms ("UCSD", "EECS") belong in app.py's alias maps, not the lists.

    Short real names such as "Law" or "Art" are kept; truncated words ("Mech",
    "Bio") are already resolved by the alias maps before this check runs.
    """
    letters = "".join(c for c in name if c.isalpha())
    return len(name) < 3 or (len(name.split()) == 1 and letters.isupper())


def _unresolved(counter: collections.Counter, canon: set[str], normalize) -> list[tuple[str, int]]:
    """Names whose normalized form is not a canonical entry, most frequent first."""
    found: dict[str, int] = {}
    for name, count in counter.most_common():
        if not name or _looks_like_fragment(name):
            continue
        normalized = normalize(name)
        if normalized in canon:
            continue
        # app.py's own normalized form (aliases and abbreviations already
        # applied) is what future runs will produce, so that is what we add.
        found[normalized] = found.get(normalized, 0) + count
    return sorted(found.items(), key=lambda item: (-item[1], item[0]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extend the canonical lists from the data")
    parser.add_argument("--input", default="applicant_data.json",
                        help="cleaned JSON file name inside module_2 (default applicant_data.json)")
    parser.add_argument("--min-count", type=int, default=2, help="add a name only if it occurs this often")
    parser.add_argument("--dry-run", action="store_true", help="print the additions without writing")
    args = parser.parse_args(argv)

    try:
        input_path = HERE.parent / _local_name(args.input)  # a file name inside module_2
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    with input_path.open("r", encoding="utf-8") as handle:
        rows = json.load(handle)
    universities = collections.Counter((row.get("university") or "").strip() for row in rows)
    programs = collections.Counter((row.get("program_name") or "").strip() for row in rows)

    canon_unis, canon_progs = set(app.CANON_UNIS), set(app.CANON_PROGS)
    new_unis = [(n, c) for n, c in _unresolved(universities, canon_unis, app._post_normalize_university)
                if c >= args.min_count and n not in canon_unis]
    new_progs = [(n, c) for n, c in _unresolved(programs, canon_progs, app._post_normalize_program)
                 if c >= args.min_count and n not in canon_progs]

    print(f"{len(rows)} rows | {len(universities)} distinct universities, {len(programs)} distinct programs")
    print(f"adding {len(new_unis)} universities and {len(new_progs)} programs (min count {args.min_count})")
    for name, count in new_unis[:25]:
        print(f"  U {count:4d}  {name}")
    for name, count in new_progs[:25]:
        print(f"  P {count:4d}  {name}")
    if args.dry_run:
        return 0
    _append_lines(HERE / "canon_universities.txt", [n for n, _ in new_unis])
    _append_lines(HERE / "canon_programs.txt", [n for n, _ in new_progs])
    print("canon files updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
