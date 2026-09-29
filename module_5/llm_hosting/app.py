# -*- coding: utf-8 -*-
"""Flask + tiny local LLM standardizer with incremental JSONL CLI output."""

from __future__ import annotations

import json
import os
import re
import sys
import difflib
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request
from huggingface_hub import hf_hub_download
from llama_cpp import Llama  # CPU-only by default if N_GPU_LAYERS=0

app = Flask(__name__)

# ---------------- Model config ----------------
MODEL_REPO = os.getenv(
    "MODEL_REPO",
    "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
)
MODEL_FILE = os.getenv(
    "MODEL_FILE",
    "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
)

N_THREADS = int(os.getenv("N_THREADS", str(os.cpu_count() or 2)))
N_CTX = int(os.getenv("N_CTX", "2048"))
N_GPU_LAYERS = int(os.getenv("N_GPU_LAYERS", "0"))  # 0 → CPU-only

CANON_UNIS_PATH = os.getenv("CANON_UNIS_PATH", "canon_universities.txt")
CANON_PROGS_PATH = os.getenv("CANON_PROGS_PATH", "canon_programs.txt")

# Precompiled, non-greedy JSON object matcher to tolerate chatter around JSON
JSON_OBJ_RE = re.compile(r"\{.*?\}", re.DOTALL)

# ---------------- Canonical lists + abbrev maps ----------------
def _read_lines(path: str) -> List[str]:
    """Read non-empty, stripped lines from a file (UTF-8)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


CANON_UNIS = _read_lines(CANON_UNIS_PATH)
CANON_PROGS = _read_lines(CANON_PROGS_PATH)

ABBREV_UNI: Dict[str, str] = {
    r"(?i)^mcg(\.|ill)?$": "McGill University",
    r"(?i)^(ubc|u\.?b\.?c\.?)$": "University of British Columbia",
    r"(?i)^uoft$": "University of Toronto",
    # Student additions
    r"(?i)^(jhu|johns? hopkins)$": "Johns Hopkins University",
    r"(?i)^(mit)$": "Massachusetts Institute of Technology",
    r"(?i)^(nyu)$": "New York University",
    r"(?i)^(ucla)$": "University of California, Los Angeles",
    r"(?i)^(usc)$": "University of Southern California",
}

COMMON_UNI_FIXES: Dict[str, str] = {
    "McGiill University": "McGill University",
    "Mcgill University": "McGill University",
    # Normalize 'Of' → 'of'
    "University Of British Columbia": "University of British Columbia",
    # Student additions (2026-09-11): aliases seen in the scraped data, mapped
    # onto the names the canonical list already uses.  Sub-schools are mapped
    # to their parent university so admissions statistics group by school.
    "John Hopkins University": "Johns Hopkins University",
    "Johns Hopkins Bloomberg School of Public Health": "Johns Hopkins University",
    "Johns Hopkins School of Advanced International Studies": "Johns Hopkins University",
    "University of Michigan": "University of Michigan, Ann Arbor",
    "University of Michigan - Ann Arbor": "University of Michigan, Ann Arbor",
    "University of Minnesota": "University of Minnesota Twin Cities",
    "University of Minnesota - Twin Cities": "University of Minnesota Twin Cities",
    "University of Maryland": "University of Maryland, College Park",
    "University of Nebraska": "University of Nebraska–Lincoln",
    "University of Nebraska - Lincoln": "University of Nebraska–Lincoln",
    "Rutgers University": "Rutgers University–New Brunswick",
    "Rutgers University - New Brunswick": "Rutgers University–New Brunswick",
    "Stony Brook University": "Stony Brook University, The State University of New York",
    "SUNY Stony Brook": "Stony Brook University, The State University of New York",
    "UNC Chapel Hill": "University of North Carolina at Chapel Hill",
    "University of North Carolina - Chapel Hill": "University of North Carolina at Chapel Hill",
    "Penn State University": "Pennsylvania State University",
    "Penn State": "Pennsylvania State University",
    "Cambridge University": "University of Cambridge",
    "Oxford University": "University of Oxford",
    "University at Buffalo": "University at Buffalo, The State University of New York",
    "SUNY Buffalo": "University at Buffalo, The State University of New York",
    "SUNY Albany": "University at Albany, The State University of New York",
    "University at Albany": "University at Albany, The State University of New York",
    "Binghamton University": "Binghamton University, The State University of New York",
    "SUNY Binghamton": "Binghamton University, The State University of New York",
    "Hunter College": "Hunter College, City University of New York",
    "CUNY Hunter College": "Hunter College, City University of New York",
    "Columbia University in the City of New York": "Columbia University",
    "Teachers College at Columbia University": "Columbia University",
    "Teachers College, Columbia University": "Columbia University",
    "NYU Steinhardt": "New York University",
    "NYU Tandon School of Engineering": "New York University",
    "Harvard Graduate School of Education": "Harvard University",
    "Harvard Kennedy School": "Harvard University",
    "Western University": "University of Western Ontario",
    # Short forms and sub-schools seen at least twice in the scraped data
    "Harvard": "Harvard University",
    "Harvard Divinity School": "Harvard University",
    "Yale Divinity School": "Yale University",
    "Brown": "Brown University",
    "Princeton": "Princeton University",
    "Northwestern": "Northwestern University",
    "Northwestern University Feinberg School of Medicine": "Northwestern University",
    "Emory": "Emory University",
    "Emory Rollins School of Public Health": "Emory University",
    "Clemson": "Clemson University",
    "Oxford": "University of Oxford",
    "Toronto": "University of Toronto",
    "University of Toronto OISE": "University of Toronto",
    "Caltech": "California Institute of Technology",
    "Georgia Tech": "Georgia Institute of Technology",
    "MIT": "Massachusetts Institute of Technology",
    "MIT-WHOI": "Massachusetts Institute of Technology",
    "MIT Media Lab": "Massachusetts Institute of Technology",
    "Georgetown University School of Foreign Service": "Georgetown University",
    "Ohio State University - Columbus": "Ohio State University",
    "The Ohio State University": "Ohio State University",
    "UC Berkeley": "University of California, Berkeley",
    "UC San Diego": "University of California, San Diego",
    "UCSD": "University of California, San Diego",
    "UC Irvine": "University of California, Irvine",
    "UC Riverside": "University of California, Riverside",
    "UC Davis": "University of California, Davis",
    "UC Santa Barbara": "University of California, Santa Barbara",
    "UC Santa Cruz": "University of California, Santa Cruz",
    "UNC Greensboro": "University of North Carolina at Greensboro",
    "UNC Charlotte": "University of North Carolina at Charlotte",
    "NC State University": "North Carolina State University",
    "NC State": "North Carolina State University",
    "University of Massachusetts": "University of Massachusetts Amherst",
    "UMass Amherst": "University of Massachusetts Amherst",
    "UMass Boston": "University of Massachusetts Boston",
    "Texas A&M University - College Station": "Texas A&M University",
    "University of Texas": "University of Texas at Austin",
    "UT Austin": "University of Texas at Austin",
    "The Wharton School": "University of Pennsylvania",
    "Chicago Booth": "University of Chicago",
    "NYU Stern": "New York University",
    "NYU Courant": "New York University",
    "Courant Institute of Mathematical Sciences": "New York University",
    "Institute of Fine Arts, New York University": "New York University",
    "UIUC": "University of Illinois Urbana-Champaign",
    "University of Illinois": "University of Illinois Urbana-Champaign",
    "University of Illinois at Urbana-Champaign": "University of Illinois Urbana-Champaign",
    "ETH": "ETH Zurich",
    "ETHZ - ETH Zurich": "ETH Zurich",
    "ETH Zürich": "ETH Zurich",
    "Kent State": "Kent State University",
    "London School of Economics": "London School of Economics and Political Science",
    "State University of New York at Albany": "University at Albany, The State University of New York",
    "State University of New York at Buffalo": "University at Buffalo, The State University of New York",
    "Stony Brook": "Stony Brook University, The State University of New York",
    "Teachers College": "Columbia University",
    "Indiana University": "Indiana University Bloomington",
    "University of Wisconsin": "University of Wisconsin–Madison",
    "University of Washington Seattle": "University of Washington",
    "University of Virginia, Charlottesville": "University of Virginia",
    "Sarah Lawrence": "Sarah Lawrence College",
    "University College London, University of London": "University College London",
    "UCL": "University College London",
    "CUNY": "City University of New York",
    "CUNY Graduate School and University Center": "CUNY Graduate Center",
    "Carn": "Carnegie Mellon University",
    "CMU": "Carnegie Mellon University",
    "CUNY Brooklyn College": "Brooklyn College",
    "CUNY Lehman College": "Lehman College",
    "CUNY Bernard M Baruch College": "Baruch College",
    "CUNY Queens College": "Queens College",
    "University of Luxemburg": "University of Luxembourg",
    "University of Roma la Sapienza": "Sapienza University of Rome",
    "Universiteit Leiden": "Leiden University",
    "St. Andrews University": "University of St Andrews",
    "Virginia Polytechnic Institute and State University": "Virginia Tech",
    "University of Montreal": "Université de Montréal",
    "William & Mary": "College of William & Mary",
    "New York University Steinhardt": "New York University",
    "New York University Tandon School of Engineering": "New York University",
    "WashU/WUSTL": "Washington University in St. Louis",
    "WashU": "Washington University in St. Louis",
    "WUSTL": "Washington University in St. Louis",
    "Massachusetts Institute of Technology (MIT) - Woods Hole Oceanographic Institution": "Massachusetts Institute of Technology",
    "Massachusetts Institute of Technology - Woods Hole Oceanographic Institution": "Massachusetts Institute of Technology",
}
_UNI_FIXES_CI: Dict[str, str] = {k.lower(): v for k, v in COMMON_UNI_FIXES.items()}

COMMON_PROG_FIXES: Dict[str, str] = {
    "Mathematic": "Mathematics",
    "Info Studies": "Information Studies",
    # Student additions
    "Information": "Information Studies",
    "Statistic": "Statistics",
    "Computer Sciences": "Computer Science",
    "Speech Language Pathology": "Speech-Language Pathology",
    "Speech Pathology": "Speech-Language Pathology",
    # Fragments and acronyms seen at least twice in the scraped data
    "Mec": "Mechanical Engineering",
    "Mech": "Mechanical Engineering",
    "Bio": "Biology",
    "Computer": "Computer Science",
    "MSCS": "Computer Science",
    "Arch": "Architecture",
    "Master of Architecture": "Architecture",
    "ICME": "Computational and Mathematical Engineering",
    "ECE": "Electrical and Computer Engineering",
    "EECS": "Electrical Engineering and Computer Science",
    "MPA": "Public Administration",
    "MPP": "Public Policy",
    "MSW": "Social Work",
    "Master of Social Work": "Social Work",
    "Masters of Social Work": "Social Work",
    "Creative Writing - Fiction": "Creative Writing Fiction",
    "Creative Writing - Poetry": "Creative Writing Poetry",
}
_PROG_FIXES_CI: Dict[str, str] = {k.lower(): v for k, v in COMMON_PROG_FIXES.items()}

# ---------------- Few-shot prompt ----------------
SYSTEM_PROMPT = (
    "You are a data cleaning assistant. Standardize degree program and university "
    "names.\n\n"
    "Rules:\n"
    "- Input provides a single string under key `program` that may contain both "
    "program and university.\n"
    "- Split into (program name, university name).\n"
    "- Trim extra spaces and commas.\n"
    '- Expand obvious abbreviations (e.g., "McG" -> "McGill University", '
    '"UBC" -> "University of British Columbia").\n'
    "- Use Title Case for program; use official capitalization for university "
    "names (e.g., \"University of X\").\n"
    '- Ensure correct spelling (e.g., "McGill", not "McGiill").\n'
    '- If university cannot be inferred, return "Unknown".\n\n'
    "Return JSON ONLY with keys:\n"
    "  standardized_program, standardized_university\n"
)

FEW_SHOTS: List[Tuple[Dict[str, str], Dict[str, str]]] = [
    (
        {"program": "Information Studies, McGill University"},
        {
            "standardized_program": "Information Studies",
            "standardized_university": "McGill University",
        },
    ),
    (
        {"program": "Information, McG"},
        {
            "standardized_program": "Information Studies",
            "standardized_university": "McGill University",
        },
    ),
    (
        {"program": "Mathematics, University Of British Columbia"},
        {
            "standardized_program": "Mathematics",
            "standardized_university": "University of British Columbia",
        },
    ),
]

_LLM: Llama | None = None


def _load_llm() -> Llama:
    """Download (or reuse) the GGUF file and initialize llama.cpp."""
    global _LLM
    if _LLM is not None:
        return _LLM

    # Student edit (2026-09-11): huggingface_hub >= 1.0 removed the
    # `local_dir_use_symlinks` and `force_filename` arguments; with
    # `local_dir` set the file is written as models/<MODEL_FILE> anyway.
    model_path = hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="models",
    )

    # Student edit (2026-09-11): llama-cpp-python defaults n_threads_batch to
    # every CPU core, so several parallel app.py processes would oversubscribe
    # the machine; keep prompt processing on the same N_THREADS as generation.
    _LLM = Llama(
        model_path=model_path,
        n_ctx=N_CTX,
        n_threads=N_THREADS,
        n_threads_batch=N_THREADS,
        n_gpu_layers=N_GPU_LAYERS,
        verbose=False,
    )
    return _LLM


def _split_fallback(text: str) -> Tuple[str, str]:
    """Simple, rules-first parser if the model returns non-JSON."""
    s = re.sub(r"\s+", " ", (text or "")).strip().strip(",")
    parts = [p.strip() for p in re.split(r",| at | @ ", s) if p.strip()]
    prog = parts[0] if parts else ""
    uni = parts[1] if len(parts) > 1 else ""

    # High-signal expansions
    if re.fullmatch(r"(?i)mcg(ill)?(\.)?", uni or ""):
        uni = "McGill University"
    if re.fullmatch(
        r"(?i)(ubc|u\.?b\.?c\.?|university of british columbia)",
        uni or "",
    ):
        uni = "University of British Columbia"

    # Title-case program; normalize 'Of' → 'of' for universities
    prog = prog.title()
    if uni:
        uni = re.sub(r"\bOf\b", "of", uni.title())
    else:
        uni = "Unknown"
    return prog, uni


GENERIC_NAME_WORDS = {
    "university", "college", "institute", "school", "of", "the", "and", "at", "in",
    "for", "state", "technology", "sciences", "science", "studies", "department",
}


def _distinctive(name: str) -> str:
    """Student edit: the words of a name that actually identify it.

    "University of Dhaka" and "University of Dallas" share most of their
    characters, so plain difflib similarity is high; after dropping generic
    words only "dhaka" vs "dallas" is compared.
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [w for w in words if w not in GENERIC_NAME_WORDS]
    return " ".join(kept or words)


def _best_match(name: str, candidates: List[str], cutoff: float = 0.86) -> str | None:
    """Fuzzy match via difflib (lightweight, Replit-friendly).

    Student edit: a candidate is accepted only if the distinctive words of the
    two names are also similar (>= 0.8).  This stops the canonical list from
    absorbing universities it does not contain ("University of Dhaka" ->
    "University of Dallas", "University of Michigan" -> "University of Milan",
    "Penn State University" -> "Kent State University") while still repairing
    small spelling differences ("San Jose State" -> "San José State").
    """
    if not name or not candidates:
        return None
    for match in difflib.get_close_matches(name, candidates, n=3, cutoff=cutoff):
        ratio = difflib.SequenceMatcher(None, _distinctive(name), _distinctive(match)).ratio()
        if ratio >= 0.8:
            return match
    return None


SMALL_WORDS = {"and", "of", "in", "for", "the", "on", "at", "to", "de", "del", "la", "du", "des", "und"}


def _smart_title(text: str) -> str:
    """Student edit: title case that keeps connecting words and acronyms intact.

    str.title() turns "Materials Science and Engineering" into "... And ..."
    and "McGill" into "Mcgill"; this keeps small words lower-case after the
    first word, leaves short all-caps tokens (MIT, UCLA) alone, and only
    upper-cases the first letter of every other word.
    """
    words = (text or "").split()
    out: List[str] = []
    for index, word in enumerate(words):
        lower = word.lower()
        if index > 0 and lower in SMALL_WORDS:
            out.append(lower)
        elif word.isupper() and len(word) <= 6:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def _source_parts(program_text: str) -> List[str]:
    """Comma-delimited parts of the input (and joins of adjacent parts)."""
    parts = [p.strip() for p in (program_text or "").split(",") if p.strip()]
    candidates = list(parts)
    for i in range(len(parts)):
        for j in range(i + 2, len(parts) + 1):
            candidates.append(", ".join(parts[i:j]))
    return candidates


def _prefer_source_spelling(llm_value: str, program_text: str, canon: List[str], cutoff: float, normalize=None) -> str:
    """Student edit: undo spelling noise introduced by the tiny model.

    TinyLlama sometimes rewrites names it does not know ("University of
    Dhaka" -> "University of Dhaaka", "Jewish" -> "Jewis").  When the model's
    answer is not close to any canonical name but is nearly identical
    (similarity >= 0.8) to a part of the input text, the applicant's own
    spelling wins: the listing is a more reliable source than a
    1.1B-parameter model.  Genuine expansions ("UBC" -> "University of
    British Columbia") are kept because they match the canon list.
    """
    def known(name: str) -> bool:
        """In the canon list directly, through the alias maps, or by a safe fuzzy match."""
        if name in canon or _best_match(name, canon, cutoff=cutoff):
            return True
        return bool(normalize) and normalize(name) in canon

    value = (llm_value or "").strip()
    if not value or known(value):
        return value
    best, best_ratio = value, 0.0
    for candidate in _source_parts(program_text):
        if known(candidate):
            ratio = 1.0  # a known name in the input beats an unknown model answer
        else:
            ratio = difflib.SequenceMatcher(None, value.lower(), candidate.lower()).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    # 0.7: when neither side is a known name, a moderately similar input part
    # is still more trustworthy than the model's rewrite of it.
    if best_ratio >= 0.7 and best.lower() != value.lower():
        return best
    return value


def _post_normalize_program(prog: str) -> str:
    """Apply common fixes, title case, then canonical/fuzzy mapping."""
    p = (prog or "").strip()
    p = _PROG_FIXES_CI.get(p.lower(), p)  # student edit: case-insensitive lookup
    p = _smart_title(p)  # student edit: was p.title()
    if p in CANON_PROGS:
        return p
    match = _best_match(p, CANON_PROGS, cutoff=0.84)
    return match or p


UC_CAMPUSES = {
    "UCLA": "Los Angeles", "UCSD": "San Diego", "UCSB": "Santa Barbara", "UCSC": "Santa Cruz",
    "UCI": "Irvine", "UCD": "Davis", "UCB": "Berkeley", "UCR": "Riverside", "UCM": "Merced",
    "UCSF": "San Francisco",
}
TRAILING_ABBREVIATION_RE = re.compile(r"\s*\(([A-Za-z&./\- ]{2,24})\)\s*$")


def _expand_site_abbreviation(name: str) -> str:
    """Student edit: normalize Grad Cafe's "Full Name (ABBR)" school names.

    The site lists many schools as "Ecole Polytechnique Federale De Lausanne
    (EPFL)" or "University of California (UCLA)".  The University of
    California campuses are mapped to their campus names (dropping the
    parenthetical would merge every campus into one school); for any other
    short all-caps abbreviation the parenthetical is dropped so the full name
    can be matched against the canonical list.
    """
    match = TRAILING_ABBREVIATION_RE.search(name or "")
    if not match:
        return name
    abbreviation = match.group(1).strip()
    base = name[: match.start()].strip()
    campus = UC_CAMPUSES.get(abbreviation.upper().split("/")[0].strip())
    if campus and base.lower().startswith("university of california"):
        return f"University of California, {campus}"
    letters = re.sub(r"[^A-Za-z]", "", abbreviation)
    mostly_upper = letters and sum(c.isupper() for c in letters) >= 0.6 * len(letters)
    if mostly_upper and " " not in abbreviation and len(abbreviation) <= 12:
        return base  # "(EPFL)", "(MIT)", "(WashU/WUSTL)" but not "(St. George)"
    return name


def _post_normalize_university(uni: str) -> str:
    """Expand abbreviations, apply common fixes, capitalization, and canonical map."""
    u = (uni or "").strip()

    # Abbreviations
    for pat, full in ABBREV_UNI.items():
        if re.fullmatch(pat, u):
            u = full
            break

    # Student edit: "Full Name (ABBR)" handling (see _expand_site_abbreviation)
    u = _expand_site_abbreviation(u)

    # Common spelling fixes (student edit: case-insensitive lookup)
    u = _UNI_FIXES_CI.get(u.lower(), u)

    # Normalize capitalization (student edit: was re.sub(r"\bOf\b", "of", u.title()))
    if u:
        u = _smart_title(u)

    # Canonical or fuzzy map
    if u in CANON_UNIS:
        return u
    match = _best_match(u, CANON_UNIS, cutoff=0.86)
    return match or u or "Unknown"


def _call_llm(program_text: str) -> Dict[str, str]:
    """Query the tiny LLM and return standardized fields."""
    llm = _load_llm()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for x_in, x_out in FEW_SHOTS:
        messages.append(
            {"role": "user", "content": json.dumps(x_in, ensure_ascii=False)}
        )
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(x_out, ensure_ascii=False),
            }
        )
    messages.append(
        {
            "role": "user",
            "content": json.dumps({"program": program_text}, ensure_ascii=False),
        }
    )

    out = llm.create_chat_completion(
        messages=messages,
        temperature=0.0,
        max_tokens=128,
        top_p=1.0,
    )

    text = (out["choices"][0]["message"]["content"] or "").strip()
    try:
        match = JSON_OBJ_RE.search(text)
        obj = json.loads(match.group(0) if match else text)
        std_prog = str(obj.get("standardized_program", "")).strip()
        std_uni = str(obj.get("standardized_university", "")).strip()
    except Exception:
        std_prog, std_uni = _split_fallback(program_text)

    # Student edit: keep the input's spelling when the model only added noise.
    std_prog = _prefer_source_spelling(std_prog, program_text, CANON_PROGS, 0.84, _post_normalize_program)
    std_uni = _prefer_source_spelling(std_uni, program_text, CANON_UNIS, 0.86, _post_normalize_university)

    std_prog = _post_normalize_program(std_prog)
    std_uni = _post_normalize_university(std_uni)
    return {
        "standardized_program": std_prog,
        "standardized_university": std_uni,
    }


def _normalize_input(payload: Any) -> List[Dict[str, Any]]:
    """Accept either a list of rows or {'rows': [...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    return []


@app.get("/")
def health() -> Any:
    """Simple liveness check."""
    return jsonify({"ok": True})


@app.post("/standardize")
def standardize() -> Any:
    """Standardize rows from an HTTP request and return JSON."""
    payload = request.get_json(force=True, silent=True)
    rows = _normalize_input(payload)

    out: List[Dict[str, Any]] = []
    for row in rows:
        program_text = (row or {}).get("program") or ""
        result = _call_llm(program_text)
        row["llm-generated-program"] = result["standardized_program"]
        row["llm-generated-university"] = result["standardized_university"]
        out.append(row)

    return jsonify({"rows": out})


def _confine_path(path: str) -> str:
    """Student edit: command-line paths must resolve inside module_2 or the current folder."""
    resolved = os.path.realpath(path)
    for root in (os.path.dirname(os.path.dirname(os.path.realpath(__file__))), os.getcwd()):
        real_root = os.path.realpath(root)
        if resolved == real_root or resolved.startswith(real_root + os.sep):
            return resolved
    raise ValueError(f"{path} is outside the allowed folders")


def _cli_process_file(
    in_path: str,
    out_path: str | None,
    append: bool,
    to_stdout: bool,
) -> None:
    """Process a JSON file and write JSONL incrementally."""
    in_path = _confine_path(in_path)  # student edit
    with open(in_path, "r", encoding="utf-8") as f:
        rows = _normalize_input(json.load(f))

    sink = sys.stdout if to_stdout else None
    if not to_stdout:
        out_path = _confine_path(out_path or (in_path + ".jsonl"))  # student edit
        mode = "a" if append else "w"
        sink = open(out_path, mode, encoding="utf-8")

    assert sink is not None  # for type-checkers

    try:
        for row in rows:
            program_text = (row or {}).get("program") or ""
            result = _call_llm(program_text)
            row["llm-generated-program"] = result["standardized_program"]
            row["llm-generated-university"] = result["standardized_university"]

            json.dump(row, sink, ensure_ascii=False)
            sink.write("\n")
            sink.flush()
    finally:
        if sink is not sys.stdout:
            sink.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Standardize program/university with a tiny local LLM.",
    )
    parser.add_argument(
        "--file",
        help="Path to JSON input (list of rows or {'rows': [...]})",
        default=None,
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the HTTP server instead of CLI.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for JSON Lines (ndjson). "
        "Defaults to <input>.jsonl when --file is set.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to the output file instead of overwriting.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write JSON Lines to stdout instead of a file.",
    )
    args = parser.parse_args()

    if args.serve or args.file is None:
        port = int(os.getenv("PORT", "8000"))
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        _cli_process_file(
            in_path=args.file,
            out_path=args.out,
            append=bool(args.append),
            to_stdout=bool(args.stdout),
        )
