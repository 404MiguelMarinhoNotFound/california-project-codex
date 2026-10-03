"""
Summarise recorded bench runs and judge spikes against a baseline.

    uv run python tools/bench_report.py bench/results/*.jsonl
    uv run python tools/bench_report.py bench/results/*.jsonl --baseline baseline

Each line of a results file is one run written by `bench_tv_power.py --json`.
Per scenario and spike it prints the median, min and max of every phase, and a
verdict per spike on the `total` phase.

The win rule is deliberately strict: the candidate's median has to beat the
baseline's median by more than the baseline's own min-max spread. The deep wake
has ranged 34-42s over identical runs, so anything inside that band is noise.
"""

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path


def load(paths: list[Path]) -> list[dict]:
    records = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def summarize(records: list[dict]) -> dict[tuple[str, str], dict[str, tuple[float, float, float, int]]]:
    """(scenario, spike) -> phase -> (median, min, max, n). A phase a run lacks is skipped, never 0."""
    values: dict[tuple[str, str], dict[str, list[float]]] = {}
    for rec in records:
        phases = values.setdefault((rec["scenario"], rec["spike"]), {})
        for name, ms in (rec.get("phases") or {}).items():
            if ms is not None:
                phases.setdefault(name, []).append(float(ms))
        if rec.get("total_ms") is not None:
            phases.setdefault("total", []).append(float(rec["total_ms"]))
    return {
        key: {name: (statistics.median(v), min(v), max(v), len(v)) for name, v in phases.items()}
        for key, phases in values.items()
    }


def is_win(baseline: list[float], candidate: list[float]) -> bool:
    if len(baseline) < 2 or len(candidate) < 2:
        return False
    spread = max(baseline) - min(baseline)
    return statistics.median(candidate) < statistics.median(baseline) - spread


def _totals(records: list[dict], scenario: str, spike: str) -> list[float]:
    return [float(r["total_ms"]) for r in records
            if r["scenario"] == scenario and r["spike"] == spike and r.get("total_ms") is not None]


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise bench_tv_power.py --json results.")
    parser.add_argument("files", nargs="+", help="JSONL result files (globs are expanded)")
    parser.add_argument("--baseline", default="baseline", help="spike label to judge the others against")
    args = parser.parse_args()

    paths = [Path(p) for pattern in args.files for p in (glob.glob(pattern) or [pattern])]
    records = load(paths)
    summary = summarize(records)

    for scenario in sorted({s for s, _ in summary}):
        print(f"\n== {scenario}")
        for (sc, spike), phases in sorted(summary.items()):
            if sc != scenario:
                continue
            print(f"  [{spike}]")
            for name, (med, lo, hi, n) in sorted(phases.items(), key=lambda kv: kv[1][0]):
                print(f"    {name:<22} median {med / 1000:6.1f}s  min {lo / 1000:6.1f}s  "
                      f"max {hi / 1000:6.1f}s  n={n}")
        base = _totals(records, scenario, args.baseline)
        for spike in sorted({sp for sc, sp in summary if sc == scenario} - {args.baseline}):
            cand = _totals(records, scenario, spike)
            verdict = "WIN" if is_win(base, cand) else "no measurable gain"
            print(f"  verdict {spike} vs {args.baseline}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
