"""Tests for iterative refinement: yml parsing, per-iter paths, judge
JSON extraction.

Focused unit tests — no claude invocation, no filesystem side-effects
beyond tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rufler.config import FlowConfig
from rufler.run_steps import (
    collect_prior_reports,
    iteration_paths,
    snapshot_task_paths,
)
from rufler.tasks.judge import (
    JudgeResult,
    _parse_verdict,
    build_judge_prompt,
)


# ---------- YAML parsing ----------

def _write_flow(tmp_path: Path, task_block: dict) -> Path:
    """Write a minimal valid flow yml with the given task: section."""
    doc = {
        "project": {"name": "t"},
        "task": task_block,
        "agents": [
            {"name": "coder", "type": "coder", "role": "worker",
             "seniority": "junior", "prompt": "do things"},
        ],
    }
    p = tmp_path / "rufler_flow.yml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_iterations_default_is_one(tmp_path):
    p = _write_flow(tmp_path, {"main": "do X"})
    cfg = FlowConfig.load(p)
    assert cfg.task.iterations == 1
    assert cfg.task.iteration_scope == "full"
    assert cfg.task.iteration_judge is False


def test_iterations_and_judge_parsed(tmp_path):
    p = _write_flow(tmp_path, {
        "main": "do X",
        "iterations": 5,
        "iteration_scope": "decompose_only",
        "iteration_refine": True,
        "iteration_judge": True,
        "iteration_judge_model": "sonnet",
        "iteration_judge_threshold": 0.75,
    })
    cfg = FlowConfig.load(p)
    assert cfg.task.iterations == 5
    assert cfg.task.iteration_scope == "decompose_only"
    assert cfg.task.iteration_judge is True
    assert cfg.task.iteration_judge_model == "sonnet"
    assert cfg.task.iteration_judge_threshold == 0.75


def test_iterations_invalid_scope_rejected(tmp_path):
    p = _write_flow(tmp_path, {"main": "X", "iteration_scope": "bogus"})
    with pytest.raises(Exception, match="iteration_scope"):
        FlowConfig.load(p)


def test_iterations_zero_rejected(tmp_path):
    p = _write_flow(tmp_path, {"main": "X", "iterations": 0})
    with pytest.raises(Exception, match="positive integer"):
        FlowConfig.load(p)


def test_iteration_judge_threshold_out_of_range(tmp_path):
    p = _write_flow(tmp_path, {"main": "X", "iteration_judge_threshold": 1.5})
    with pytest.raises(Exception, match="0.0 and 1.0"):
        FlowConfig.load(p)


# ---------- iteration_paths ----------

class _FakeTaskPaths:
    """Stand-in for _OriginalTaskPaths with just the fields iteration_paths
    reads. We use snapshot_task_paths on a real FlowConfig below to cover
    the full API contract — this tiny stub covers edge cases without yml."""
    def __init__(self):
        self.deep_think_output = ".rufler/analysis.md"
        self.decompose_dir = ".rufler/tasks"
        self.decompose_file = ".rufler/tasks/decomposed_tasks.yml"
        self.on_task_complete_report_path = ".rufler/reports/{task}.md"
        self.on_complete_report_path = ".rufler/report.md"
        self.group_snapshot = []
        self.project_summary = ""


def test_iteration_paths_single_iter_is_unchanged():
    orig = _FakeTaskPaths()
    p = iteration_paths(orig, 1, 1, "full")
    assert p.deep_think_output == orig.deep_think_output
    assert p.decompose_dir == orig.decompose_dir
    assert p.on_task_complete_report_path == orig.on_task_complete_report_path
    assert p.on_complete_report_path == orig.on_complete_report_path


def test_iteration_paths_full_scope_namespaces_everything():
    orig = _FakeTaskPaths()
    p = iteration_paths(orig, 2, 5, "full")
    assert p.deep_think_output == ".rufler/iter-02/analysis.md"
    assert p.decompose_dir == ".rufler/iter-02/tasks"
    assert p.decompose_file == ".rufler/iter-02/decomposed_tasks.yml"
    assert p.on_task_complete_report_path == ".rufler/iter-02/reports/{task}.md"
    assert p.on_complete_report_path == ".rufler/iter-02/report.md"
    assert p.judge_output == ".rufler/iter-02/judge.md"


def test_iteration_paths_decompose_only_shares_analysis():
    orig = _FakeTaskPaths()
    p = iteration_paths(orig, 3, 5, "decompose_only")
    # Analysis is shared across iters
    assert p.deep_think_output == orig.deep_think_output
    # Decompose + reports per-iter
    assert p.decompose_dir == ".rufler/iter-03/tasks"
    assert p.on_complete_report_path == ".rufler/iter-03/report.md"


def test_iteration_paths_tasks_only_shares_analysis_and_decompose():
    orig = _FakeTaskPaths()
    p = iteration_paths(orig, 2, 5, "tasks_only")
    assert p.deep_think_output == orig.deep_think_output
    assert p.decompose_dir == orig.decompose_dir
    assert p.decompose_file == orig.decompose_file
    # But reports still per-iter
    assert p.on_complete_report_path == ".rufler/iter-02/report.md"


def test_snapshot_task_paths_roundtrip(tmp_path):
    from rufler.run_steps import restore_task_paths
    p = _write_flow(tmp_path, {"main": "X"})
    cfg = FlowConfig.load(p)
    orig = snapshot_task_paths(cfg)
    # Mutate
    cfg.task.deep_think_output = "mutated/path"
    cfg.task.on_complete.report_path = "mutated/report.md"
    restore_task_paths(cfg, orig)
    assert cfg.task.deep_think_output == orig.deep_think_output
    assert cfg.task.on_complete.report_path == orig.on_complete_report_path


# ---------- collect_prior_reports ----------

def test_collect_prior_reports_empty_for_single_iter(tmp_path):
    orig = _FakeTaskPaths()
    out = collect_prior_reports(tmp_path, orig, total_iters=1, up_to_iter=0)
    assert out == ""


def test_collect_prior_reports_reads_iter_final_report(tmp_path):
    orig = _FakeTaskPaths()
    iter1_final = tmp_path / ".rufler" / "iter-01" / "report.md"
    iter1_final.parent.mkdir(parents=True, exist_ok=True)
    iter1_final.write_text("iter-1 said: all good", encoding="utf-8")
    out = collect_prior_reports(
        tmp_path, orig, total_iters=3, up_to_iter=1, scope="full",
    )
    assert "iter-1 said: all good" in out
    assert "Iteration 01 — final report" in out


def test_collect_prior_reports_reads_per_task_reports(tmp_path):
    orig = _FakeTaskPaths()
    per_task_dir = tmp_path / ".rufler" / "iter-01" / "reports"
    per_task_dir.mkdir(parents=True, exist_ok=True)
    (per_task_dir / "task_1.md").write_text("task 1 done", encoding="utf-8")
    (per_task_dir / "task_2.md").write_text("task 2 done", encoding="utf-8")
    out = collect_prior_reports(
        tmp_path, orig, total_iters=2, up_to_iter=1, scope="full",
    )
    assert "task 1 done" in out
    assert "task 2 done" in out


# ---------- judge verdict parsing ----------

def test_parse_verdict_strict_json_done():
    raw = '{"verdict":"done","score":0.95,"reasoning":"all tests pass","remaining_work":""}'
    r = _parse_verdict(raw, threshold=0.9)
    assert r.verdict == "done"
    assert r.score == 0.95
    assert r.should_stop is True
    assert r.parse_error is None


def test_parse_verdict_strict_json_continue():
    raw = '{"verdict":"continue","score":0.5,"reasoning":"half done","remaining_work":"finish auth"}'
    r = _parse_verdict(raw, threshold=0.9)
    assert r.verdict == "continue"
    assert r.score == 0.5
    assert r.should_stop is False
    assert "finish auth" in r.remaining_work


def test_parse_verdict_wrapped_in_prose():
    raw = 'Sure, here is my verdict:\n\n{"verdict":"done","score":0.92,"reasoning":"ok","remaining_work":""}\n\nThanks!'
    r = _parse_verdict(raw, threshold=0.9)
    assert r.verdict == "done"
    assert r.score == 0.92


def test_parse_verdict_garbage_falls_back_to_continue():
    r = _parse_verdict("total garbage, no JSON here", threshold=0.9)
    assert r.verdict == "continue"
    assert r.parse_error is not None
    assert r.should_stop is False


def test_parse_verdict_empty_stays_continue():
    r = _parse_verdict("", threshold=0.9)
    assert r.verdict == "continue"
    assert r.parse_error == "empty output"


def test_parse_verdict_derives_verdict_from_score_when_missing():
    # No verdict field — derive from score vs threshold.
    raw = '{"score":0.95,"reasoning":"ok"}'
    r = _parse_verdict(raw, threshold=0.9)
    assert r.verdict == "done"
    raw2 = '{"score":0.3,"reasoning":"nope"}'
    r2 = _parse_verdict(raw2, threshold=0.9)
    assert r2.verdict == "continue"


def test_parse_verdict_clamps_score():
    raw = '{"verdict":"done","score":1.5}'
    r = _parse_verdict(raw, threshold=0.9)
    assert r.score == 1.0
    raw2 = '{"verdict":"continue","score":-0.5}'
    r2 = _parse_verdict(raw2, threshold=0.9)
    assert r2.score == 0.0


# ---------- judge prompt ----------

def test_build_judge_prompt_fills_placeholders():
    out = build_judge_prompt(
        main_task="Build X",
        project="demo",
        iter_num=2,
        total_iters=5,
        threshold=0.9,
        accumulated_reports="iter 1 did the backend",
    )
    assert "Build X" in out
    assert "demo" in out
    assert "iteration 2 of 5" in out
    assert "iter 1 did the backend" in out
    # DENY_RULES still prepended
    assert "PATH-ACCESS POLICY" in out


def test_build_judge_prompt_empty_reports_gets_placeholder():
    out = build_judge_prompt(
        main_task="T", project="p", iter_num=1, total_iters=3,
        threshold=0.8, accumulated_reports="",
    )
    assert "no prior reports" in out
