"""
run_parallel.py - Run the instructor's app.py standardizer on many CPU cores.

JHU EN.605.256 Module 2 - added by the student (app.py itself is unchanged
apart from the small compatibility fix described in module_2/README.md).

app.py processes one row at a time with a single llama.cpp instance, which is
far too slow for 30,000 rows.  This wrapper:

  1. splits the input JSON array into N contiguous shards under work/,
  2. starts N copies of `app.py --file shard --out shard.jsonl`, each with
     N_THREADS CPU threads and its own model instance,
  3. resumes automatically: rows already present in a shard's .jsonl are
     skipped, so an interrupted run just picks up where it stopped,
  4. merges the JSON Lines outputs back into one JSON array in the original
     row order and writes it to --output (llm_extend_applicant_data.json).

Usage (from module_2/llm_hosting):
    python run_parallel.py --input ../applicant_data.json \
        --output ../llm_extend_applicant_data.json --workers 8 --threads 2
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
NEW_KEYS = ("llm-generated-program", "llm-generated-university")


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


def _warm_up_model(python: str) -> None:
    """Download / load the GGUF once before the workers start (avoids N parallel downloads)."""
    print("Loading the model once (downloads it on first use)...", flush=True)
    subprocess.run(
        [python, "-c", "import app; app._load_llm(); print('model ready')"],
        cwd=HERE, check=True, env={**os.environ, "N_THREADS": "4"},
    )


def _start_worker(python: str, shard_in: Path, shard_out: Path, threads: int, append: bool) -> subprocess.Popen:
    """Launch one app.py process on a shard."""
    command = [python, str(APP), "--file", str(shard_in), "--out", str(shard_out)]
    if append:
        command.append("--append")
    env = {**os.environ, "N_THREADS": str(threads)}
    log = shard_out.with_suffix(".log").open("a", encoding="utf-8")
    return subprocess.Popen(command, cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel driver for app.py")
    parser.add_argument("--input", required=True, help="cleaned JSON array, e.g. ../applicant_data.json")
    parser.add_argument("--output", required=True, help="merged JSON array to write")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--threads", type=int, default=2, help="CPU threads per worker (N_THREADS)")
    parser.add_argument("--work-dir", default=str(HERE / "work"), help="where shards and JSONL outputs live")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N rows (testing)")
    parser.add_argument("--fresh", action="store_true", help="discard previous shard outputs")
    args = parser.parse_args(argv)

    python = sys.executable
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    rows = _read_rows(Path(args.input))
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    workers = max(1, min(args.workers, total))
    shard_size = -(-total // workers)  # ceiling division
    print(f"{total} rows -> {workers} workers x {args.threads} threads (shards of {shard_size})", flush=True)

    _warm_up_model(python)

    processes: list[tuple[int, subprocess.Popen | None, Path, int]] = []
    for index in range(workers):
        shard_rows = rows[index * shard_size:(index + 1) * shard_size]
        shard_in = work_dir / f"shard_{index:02d}.json"
        shard_out = work_dir / f"shard_{index:02d}.jsonl"
        if args.fresh and shard_out.exists():
            shard_out.unlink()
        done = len(_read_jsonl(shard_out))
        if done >= len(shard_rows):
            processes.append((index, None, shard_out, len(shard_rows)))
            continue
        # Resume: hand the worker only the rows that are not in its output yet.
        remaining = shard_rows[done:]
        with shard_in.open("w", encoding="utf-8") as handle:
            json.dump(remaining, handle, ensure_ascii=False)
        if done:
            # Drop any partial trailing line so --append continues cleanly.
            kept = _read_jsonl(shard_out)
            with shard_out.open("w", encoding="utf-8") as handle:
                for row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        processes.append((index, _start_worker(python, shard_in, shard_out, args.threads, append=bool(done)), shard_out, len(shard_rows)))

    started = time.monotonic()
    initial_done = sum(_count_lines(out) for _, _, out, _ in processes)
    while any(proc is not None and proc.poll() is None for _, proc, _, _ in processes):
        time.sleep(30)
        done = sum(min(_count_lines(out), size) for _, _, out, size in processes)
        elapsed = time.monotonic() - started
        rate = (done - initial_done) / elapsed * 60 if elapsed else 0.0
        eta = (total - done) / rate if rate else float("inf")
        print(f"{done}/{total} rows | {rate:.0f} rows/min | eta {eta:.0f} min", flush=True)

    failures = [index for index, proc, _, _ in processes if proc is not None and proc.returncode != 0]
    if failures:
        print(f"Workers failed: {failures} (see work/shard_XX.log); re-run to resume", file=sys.stderr)
        return 1

    merged: list[dict] = []
    for index, _, shard_out, size in processes:
        shard_rows = _read_jsonl(shard_out)[:size]
        if len(shard_rows) != size:
            print(f"Shard {index} has {len(shard_rows)} rows, expected {size}; re-run to resume", file=sys.stderr)
            return 1
        merged.extend(shard_rows)
    missing = sum(1 for row in merged if not all(key in row for key in NEW_KEYS))
    if missing:
        print(f"{missing} rows lack the llm-generated fields", file=sys.stderr)
        return 1

    output = Path(args.output)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2, ensure_ascii=False)
    print(f"Wrote {len(merged)} rows -> {output} in {(time.monotonic() - started) / 60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
