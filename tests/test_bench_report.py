"""
The bench report and the win rule.

A spike only counts when its median beats the baseline median by more than the
baseline's own spread: the deep wake has ranged 34-42s on identical runs, so a
single faster run is noise until proven otherwise.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools.bench_report import is_win, load, summarize


def _rec(scenario="S1", spike="baseline", total=40000.0, **phases):
    return {"scenario": scenario, "spike": spike, "git_sha": "abc", "run": 1,
            "phases": phases, "total_ms": total, "outcome": "ok",
            "settings_before": {}, "settings_restored": True}


class SummarizeTests(unittest.TestCase):
    def test_summarize_groups_by_scenario_and_spike(self):
        out = summarize([_rec(total=40000), _rec(total=42000),
                         _rec(spike="H6", total=30000), _rec(scenario="S2-deep", total=90000)])
        self.assertEqual(set(out), {("S1", "baseline"), ("S1", "H6"), ("S2-deep", "baseline")})
        self.assertEqual(out[("S1", "baseline")]["total"], (41000.0, 40000.0, 42000.0, 2))

    def test_absent_phase_is_not_zero(self):
        out = summarize([_rec(box_ready=38000.0), _rec(box_ready=40000.0), _rec()])
        self.assertEqual(out[("S1", "baseline")]["box_ready"], (39000.0, 38000.0, 40000.0, 2))

    def test_a_run_with_no_total_does_not_count_toward_total(self):
        out = summarize([_rec(total=None), _rec(total=40000)])
        self.assertEqual(out[("S1", "baseline")]["total"][3], 1)

    def test_load_reads_every_line_of_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text("\n".join(json.dumps(_rec()) for _ in range(3)) + "\n", encoding="utf-8")
            self.assertEqual(len(load([path])), 3)


class WinRuleTests(unittest.TestCase):
    def test_is_win_needs_more_than_the_baseline_spread(self):
        baseline = [40000, 42000, 44000]  # median 42000, spread 4000 -> must be < 38000
        self.assertTrue(is_win(baseline, [36000, 37000, 37900]))
        self.assertFalse(is_win(baseline, [38000, 38500, 39000]))

    def test_is_win_refuses_single_runs(self):
        self.assertFalse(is_win([40000], [10000, 10000]))
        self.assertFalse(is_win([40000, 41000], [10000]))


if __name__ == "__main__":
    unittest.main()
