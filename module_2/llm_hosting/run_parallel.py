"""
run_parallel.py - Run the instructor's app.py standardizer on many CPU cores.

JHU EN.605.256 Module 2 - added by the student (app.py is the instructor's
file with the small edits described in module_2/README.md).

app.py processes one row at a time with a single llama.cpp instance, which is
far too slow for 30,000 rows.  This wrapper:

  1. collects the DISTINCT `program` strings (the model runs at temperature 0,
     so identical inputs always give identical answers; roughly four rows in
     ten carry a string already seen),
  2. keeps every answer in work/answers.jsonl, so a second run only sends the
     strings that are new since the last run (the scrape can still be growing)
     and an interrupted run resumes where it stopped,
  3. splits the pending strings into N shards and starts N copies of
     `app.py --file shard --out shard.jsonl`, each with N_THREADS CPU threads
     and its own model instance,
  4. re-applies app.py's post-processing to every cached answer at merge time,
     so edits to the canonical lists take effect without re-running the model,
  5. maps the answers back onto every original row (adding
     `llm-generated-program` and `llm-generated-university`, exactly what a
     plain `app.py --file` run adds) and writes one JSON array to --output.

Usage (from module_2/llm_hosting):
    python run_parallel.py --input ../applicant_data.json \
        --output ../llm_extend_applicant_data.json --workers 10 --threads 2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
PROGRAM_KEY, UNIVERSITY_KEY = "llm-generated-program", "llm-generated-university"
PROGRESS_EVERY_SECONDS = 60


def _read_rows(path: Path) -> list[dict]:
    """Load a JSON array (or {'rows': [...]}) of applicant records."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload["rows"] if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path} does not contain a list of rows")
    return rows


def _read_jsonl(path: Path) -> list[dict]:
    """Read complete JSON Lines; a truncated last line (crash) is ignored."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                break  # partial final line from an interrupted worker
    return rows


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _load_answers(work_dir: Path) -> dict[str, tuple[str, str]]:
    """Harvest every answer written so far (answers.jsonl plus any shard outputs)."""
    answers: dict[str, tuple[str, str]] = {}
    sources = [work_dir / "answers.jsonl"] + sorted(work_dir.glob("shard_*.jsonl"))
    for path in sources:
        for row in _read_jsonl(path):
            text = row.get("program") or ""
            if PROGRAM_KEY in row and UNIVERSITY_KEY in row:
                answers[text] = (row[PROGRAM_KEY], row[UNIVERSITY_KEY])
    return answers


def _save_answers(work_dir: Path, answers: dict[str, tuple[str, str]]) -> None:
    """Consolidate all answers into answers.jsonl and remove the shard files."""
    with (work_dir / "answers.jsonl").open("w", encoding="utf-8") as handle:
        for text, (program, university) in answers.items():
            handle.write(json.dumps({"program": text, PROGRAM_KEY: program, UNIVERSITY_KEY: university},
                                    ensure_ascii=False) + "\n")
    for path in list(work_dir.glob("shard_*.jsonl")) + list(work_dir.glob("shard_*.json")):
        path.unlink()


def _warm_up_model(python: str) -> None:
    """Download / load the GGUF once before the workers start (avoids N parallel downloads)."""
    print("Loading the model once (downloads it on first use)...", flush=True)
    subprocess.run(
        [python, "-c", "import app; app._load_llm(); print('model ready')"],
        cwd=HERE, check=True, env={**os.environ, "N_THREADS": "4"},
    )


def _start_worker(python: str, shard_in: Path, shard_out: Path, threads: int) -> subprocess.Popen:
    """Launch one app.py process on a shard of {"program": ...} rows."""
    command = [python, str(APP), "--file", str(shard_in), "--out", str(shard_out)]
    env = {**os.environ, "N_THREADS": str(threads)}
    log = shard_out.with_suffix(".log").open("a", encoding="utf-8")
    return subprocess.Popen(command, cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)


def _run_workers(python: str, pending: list[str], work_dir: Path, workers: int, threads: int) -> bool:
    """Standardize *pending* strings with N app.py processes; True when all finished cleanly."""
    workers = max(1, min(workers, len(pending)))
    shard_size = -(-len(pending) // workers)  # ceiling division
    print(f"{len(pending)} strings to standardize -> {workers} workers x {threads} threads "
          f"(shards of {shard_size})", flush=True)
    _warm_up_model(python)

    processes: list[tuple[subprocess.Popen, Path, int]] = []
    for index in range(workers):
        shard_texts = pending[index * shard_size:(index + 1) * shard_size]
        if not shard_texts:
            continue
        shard_in = work_dir / f"shard_{index:02d}.json"
        shard_out = work_dir / f"shard_{index:02d}.jsonl"
        with shard_in.open("w", encoding="utf-8") as handle:
            json.dump([{"program": text} for text in shard_texts], handle, ensure_ascii=False)
        processes.append((_start_worker(python, shard_in, shard_out, threads), shard_out, len(shard_texts)))

    started = time.monotonic()
    total = len(pending)
    while any(proc.poll() is None for proc, _, _ in processes):
        time.sleep(PROGRESS_EVERY_SECONDS)
        done = sum(min(_count_lines(out), size) for _, out, size in processes)
        elapsed = time.monotonic() - started
        rate = done / elapsed * 60 if elapsed else 0.0
        eta = (total - done) / rate if rate else float("inf")
        print(f"{done}/{total} strings | {rate:.0f}/min | eta {eta:.0f} min", flush=True)

    failed = [index for index, (proc, _, _) in enumerate(processes) if proc.returncode != 0]
    if failed:
        print(f"Workers failed: {failed} (see work/shard_XX.log); re-run to resume", file=sys.stderr)
    print(f"Workers finished in {(time.monotonic() - started) / 60:.1f} min", flush=True)
    return not failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel driver for app.py")
    parser.add_argument("--input", required=True, help="cleaned JSON array, e.g. ../applicant_data.json")
    parser.add_argument("--output", required=True, help="merged JSON array to write")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--threads", type=int, default=2, help="CPU threads per worker (N_THREADS)")
    parser.add_argument("--work-dir", default=str(HERE / "work"), help="where answers.jsonl and shards live")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N rows (testing)")
    parser.add_argument("--no-merge", action="store_true", help="only standardize; do not write --output")
    args = parser.parse_args(argv)

    python = sys.executable
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    rows = _read_rows(Path(args.input))
    if args.limit:
        rows = rows[: args.limit]
    texts = list(dict.fromkeys((row or {}).get("program") or "" for row in rows))  # distinct, first-seen order

    answers = _load_answers(work_dir)
    _save_answers(work_dir, answers)  # fold any earlier shard output into answers.jsonl
    pending = [text for text in texts if text not in answers]
    print(f"{len(rows)} rows, {len(texts)} distinct program strings, {len(answers)} already answered, "
          f"{len(pending)} pending", flush=True)

    if pending:
        ok = _run_workers(python, pending, work_dir, args.workers, args.threads)
        answers = _load_answers(work_dir)
        _save_answers(work_dir, answers)
        if not ok:
            return 1
    if args.no_merge:
        return 0

    missing = [text for text in texts if text not in answers]
    if missing:
        print(f"{len(missing)} strings still have no answer; re-run to resume", file=sys.stderr)
        return 1

    # Re-apply the post-processing so canonical-list edits made after the model
    # ran are honoured (the normalizers are safe to apply twice).
    import app  # noqa: WPS433  (loads the canon lists; the model is not loaded)

    merged: list[dict] = []
    for row in rows:
        program, university = answers[(row or {}).get("program") or ""]
        extended = dict(row)
        extended[PROGRAM_KEY] = app._post_normalize_program(program)
        extended[UNIVERSITY_KEY] = app._post_normalize_university(university)
        merged.append(extended)

    output = Path(args.output)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2, ensure_ascii=False)
    print(f"Wrote {len(merged)} rows ({len(texts)} distinct strings) -> {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
