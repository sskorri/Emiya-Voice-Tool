"""The bench runs end to end on a toy dataset (R3): tests\\reliability\\bench.py in a child process, one repeat of its
core part and of its page renders and UI operations, with bench.json and bench.md written where --out points.

It checks the pipeline, not the speed: every metric the report promises is there, the toy run keeps every budget,
and nothing lands in the install folder. The full bench is `python tests\\reliability\\bench.py --out DIR`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.reliability import bench

APP_ROOT = Path(__file__).resolve().parents[2]
PAGES = ("speak", "voices", "forge", "takes", "models", "settings")
NEVER_IN_THE_INSTALL_FOLDER = ("ui_settings.json", "logs", ".nicegui", ".nicegui-dev", ".dev")


def test_the_bench_runs_on_a_toy_dataset_and_writes_its_report(tmp_path: Path) -> None:
    out = tmp_path / "out"
    before = {name: (APP_ROOT / name).exists() for name in NEVER_IN_THE_INSTALL_FOLDER}
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, str(APP_ROOT / "tests" / "reliability" / "bench.py"), "--toy", "--repeat", "1",
         "--out", str(out), "--select", "bench_core or page_renders or ui_operations"],
        cwd=APP_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    detail = f"exit {done.returncode}\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    assert done.returncode == 0, detail

    report = json.loads((out / "bench.json").read_text(encoding="utf-8"))
    assert report["ok"] and report["failed"] == [], detail
    assert report["scale"]["name"] == "toy" and report["repeat"] == 1
    assert report["run"]["exit_code"] == 0, report["run"]
    assert report["run"]["summary"].startswith("3 passed"), report["run"]["summary"]
    metrics = report["metrics"]
    promised = ["core.start", "core.start_loop_block", "core.takes_index_load", "core.history_load",
                "core.library_refresh", "core.check_files", "search.voice", "search.take_text", "search.no_match",
                "takes.tab.all", "takes.query", "takes.voice_filter", "takes.star", "takes.unstar",
                "takes.purge_all_unstarred", "refresh.speak", "tick.generating", "memory.rss_after_start",
                "page.shell", "ui.open_root", "ui.search.voice", "ui.takes.tab.starred", "ui.voices.select",
                *(f"page.first.{page}" for page in PAGES), *(f"page.rerender.{page}" for page in PAGES)]
    assert [name for name in promised if name not in metrics] == []
    for name in promised:
        assert metrics[name]["n"] >= 1 and metrics[name]["ok"], (name, metrics[name])
    assert metrics["memory.rss_after_start"]["median"] > 0, "the working set reading failed"
    counts = report["counts"]
    toy = bench.SCALES["toy"]
    assert (counts["data.voices"], counts["data.takes"], counts["data.session_takes"]) == \
        (toy.voices, toy.takes, toy.session)
    assert counts["takes.purged"] == counts["data.takes"] - counts["data.starred"] + 1   # + the bench's own take
    assert counts["rows.speak.session"] == toy.session and counts["rows.speak.voices"] == toy.voices

    markdown = (out / "bench.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Emiya GUI · scale bench (R3)") and "**Verdict: all budgets hold**" in markdown
    assert "| pages | page.first.speak |" in markdown
    assert {name: (APP_ROOT / name).exists() for name in NEVER_IN_THE_INSTALL_FOLDER} == before
