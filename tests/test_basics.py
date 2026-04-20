"""Smoke tests covering the bits the senior review flagged.

Targeted, fast, no network: _fmt_age edge cases, _resolve_log_path priority,
registry v1->v2 migration, the 5-status model, and remove_many batching.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from rufler.process import fmt_age as _fmt_age, resolve_log_path as _resolve_log_path, DEFAULT_LOG_REL
from rufler.registry import (
    Registry,
    RunEntry,
    TaskEntry,
    new_entry,
    _pid_starttime,
)


# ---------- _fmt_age ----------

def test_fmt_age_none_returns_dash():
    assert _fmt_age(None) == "-"


def test_fmt_age_zero_returns_dash():
    # Regression: previously `if not ts:` returned "-" which collapsed ts=0
    # into "no value". Now ts=0 is also a sentinel for "unknown".
    assert _fmt_age(0) == "-"
    assert _fmt_age(0.0) == "-"


def test_fmt_age_seconds():
    assert _fmt_age(time.time() - 5).endswith("s")


def test_fmt_age_minutes():
    assert "m" in _fmt_age(time.time() - 125)


def test_fmt_age_future_clamped():
    # Future timestamps must not produce negatives.
    assert _fmt_age(time.time() + 60) == "0s"


# ---------- _resolve_log_path ----------

def _entry(log_path: str = "") -> RunEntry:
    return RunEntry(
        id="deadbeef",
        project="p",
        flow_file="/x.yml",
        base_dir="/tmp",
        mode="foreground",
        run_mode="sequential",
        started_at=time.time(),
        log_path=log_path,
    )


def test_resolve_log_path_cli_wins(tmp_path: Path):
    cli = Path("custom.log")
    out = _resolve_log_path(_entry("/from/entry.log"), cli, tmp_path, Path("/from/yml.log"))
    assert out == (tmp_path / "custom.log").resolve()


def test_resolve_log_path_entry_beats_yml(tmp_path: Path):
    out = _resolve_log_path(_entry("/from/entry.log"), None, tmp_path, Path("/from/yml.log"))
    assert out == Path("/from/entry.log")


def test_resolve_log_path_yml_beats_default(tmp_path: Path):
    out = _resolve_log_path(_entry(""), None, tmp_path, Path("/from/yml.log"))
    assert out == Path("/from/yml.log")


def test_resolve_log_path_default(tmp_path: Path):
    out = _resolve_log_path(None, None, tmp_path, None)
    assert out == (tmp_path / DEFAULT_LOG_REL).resolve()


# ---------- Registry v1 -> v2 migration ----------

def test_registry_reads_legacy_v1_list_format(tmp_path: Path):
    legacy = [{
        "id": "11111111",
        "project": "old",
        "flow_file": "/x.yml",
        "base_dir": str(tmp_path),
        "mode": "foreground",
        "run_mode": "sequential",
        "started_at": time.time(),
        "pids": [],
        "log_path": "",
        "tasks": [],
    }]
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    reg = Registry(path=p)
    entries = reg.list_all()
    assert len(entries) == 1
    assert entries[0].id == "11111111"


def test_registry_v2_roundtrip(tmp_path: Path):
    p = tmp_path / "registry.json"
    reg = Registry(path=p)
    e = new_entry(
        project="proj",
        flow_file=tmp_path / "flow.yml",
        base_dir=tmp_path,
        mode="foreground",
        run_mode="sequential",
        log_path=tmp_path / "run.log",
    )
    reg.add(e)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw.get("version") == 2
    assert "runs" in raw and "projects" in raw
    assert raw["projects"]["proj"]["total_runs"] == 1


# ---------- 5-status model ----------

def _persist(reg: Registry, entry: RunEntry):
    reg.add(entry)


def test_status_running_when_pid_alive(tmp_path: Path):
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="p", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.pids = [os.getpid()]
    e.pid_starttimes = [_pid_starttime(os.getpid()) or 0]
    reg.refresh_status(e)
    assert e.status == "running"


def test_status_exited_when_log_rc_zero(tmp_path: Path):
    log = tmp_path / "x.log"
    log.write_text(
        json.dumps({"src": "rufler", "text": "log ended rc=0"}) + "\n",
        encoding="utf-8",
    )
    e = new_entry(
        project="p", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="background", run_mode="sequential", log_path=log,
    )
    e.pids = [99999999]  # not alive
    Registry(path=tmp_path / "r.json").refresh_status(e)
    assert e.status == "done"
    assert e.exit_code == 0


def test_status_failed_when_log_rc_nonzero(tmp_path: Path):
    log = tmp_path / "x.log"
    log.write_text(
        json.dumps({"src": "rufler", "text": "log ended rc=42"}) + "\n",
        encoding="utf-8",
    )
    e = new_entry(
        project="p", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="background", run_mode="sequential", log_path=log,
    )
    e.pids = [99999999]
    Registry(path=tmp_path / "r.json").refresh_status(e)
    assert e.status == "failed"
    assert e.exit_code == 42


def test_status_stopped_when_finished_at_set_no_rc(tmp_path: Path):
    e = new_entry(
        project="p", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.pids = [99999999]
    e.finished_at = time.time()
    Registry(path=tmp_path / "r.json").refresh_status(e)
    assert e.status == "stopped"


def test_status_dead_when_no_marker_no_finished_at(tmp_path: Path):
    e = new_entry(
        project="p", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.pids = [99999999]
    Registry(path=tmp_path / "r.json").refresh_status(e)
    assert e.status == "dead"


# ---------- remove_many ----------

def test_remove_many_batches(tmp_path: Path):
    p = tmp_path / "r.json"
    reg = Registry(path=p)
    ids = []
    for i in range(5):
        e = new_entry(
            project=f"p{i}", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
            mode="foreground", run_mode="sequential",
            log_path=tmp_path / f"x{i}.log",
        )
        reg.add(e)
        ids.append(e.id)
    removed = reg.remove_many(ids[:3])
    assert removed == 3
    remaining = reg.list_all()
    assert len(remaining) == 2
    assert {e.id for e in remaining} == set(ids[3:])


def test_remove_many_empty():
    assert Registry(path=Path("/nonexistent")).remove_many([]) == 0


# ---------- token accounting ----------

def _write_log(path: Path, assistant_usages: list[dict]):
    lines = []
    for i, u in enumerate(assistant_usages):
        lines.append(json.dumps({
            "src": "claude",
            "type": "assistant",
            "message": {"id": f"msg_{i}", "usage": u},
        }))
    lines.append(json.dumps({"src": "rufler", "text": "log started"}))
    lines.append("not json")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_log_sums_assistant_usage(tmp_path: Path):
    from rufler.tokens import parse_log
    log = tmp_path / "x.log"
    # input/output are per-turn deltas (summed),
    # cache fields are session-cumulative (max wins).
    _write_log(log, [
        {"input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
        {"input_tokens": 3, "output_tokens": 2,
         "cache_read_input_tokens": 150, "cache_creation_input_tokens": 7},
    ])
    u = parse_log(log)
    assert u.input_tokens == 13
    assert u.output_tokens == 7
    assert u.cache_read == 150   # max(100, 150)
    assert u.cache_creation == 7  # max(7, 7)
    assert u.total == 13 + 7 + 150 + 7


def test_parse_log_missing_file_returns_zero(tmp_path: Path):
    from rufler.tokens import parse_log
    u = parse_log(tmp_path / "missing.log")
    assert u.total == 0


def test_parse_logs_dedupes_paths(tmp_path: Path):
    from rufler.tokens import parse_logs
    log = tmp_path / "x.log"
    _write_log(log, [{"input_tokens": 4, "output_tokens": 1}])
    u = parse_logs([log, log])  # same file twice
    assert u.input_tokens == 4
    assert u.output_tokens == 1


def test_fmt_tokens():
    from rufler.tokens import fmt_tokens
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1500).endswith("K")
    assert fmt_tokens(2_500_000).endswith("M")


def test_recompute_tokens_persists_and_rolls_up(tmp_path: Path):
    log = tmp_path / "x.log"
    _write_log(log, [
        {"input_tokens": 100, "output_tokens": 50,
         "cache_read_input_tokens": 200, "cache_creation_input_tokens": 10},
    ])
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="proj-x", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=log,
    )
    reg.add(e)
    reg.recompute_tokens(e)
    assert e.input_tokens == 100
    assert e.output_tokens == 50
    assert e.cache_read == 200
    assert e.cache_creation == 10
    assert e.total_tokens == 360

    projs = {p.name: p for p in reg.list_projects()}
    p = projs["proj-x"]
    assert p.total_input_tokens == 100
    assert p.total_output_tokens == 50
    assert p.total_cache_read == 200
    assert p.total_cache_creation == 10


def test_recompute_tokens_idempotent_no_double_count(tmp_path: Path):
    """Calling recompute twice on the same log must not double the rollup."""
    log = tmp_path / "x.log"
    _write_log(log, [{"input_tokens": 7, "output_tokens": 3}])
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="proj-y", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=log,
    )
    reg.add(e)
    reg.recompute_tokens(e)
    reg.recompute_tokens(e)
    reg.recompute_tokens(e)
    p = {pp.name: pp for pp in reg.list_projects()}["proj-y"]
    assert p.total_input_tokens == 7
    assert p.total_output_tokens == 3


def _write_flow_yml(tmp_path: Path, agents_yaml: str) -> Path:
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: dep-test\n"
        "memory:\n  namespace: deptest\n"
        "task:\n  main: 'do the thing'\n"
        f"agents:\n{agents_yaml}\n",
        encoding="utf-8",
    )
    return p


def test_depends_on_validates_unknown_agent(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: [ghost]}\n"
    )
    with pytest.raises(ValueError, match="ghost"):
        FlowConfig.load(p)


def test_depends_on_rejects_self_dependency(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: [a]}\n"
    )
    with pytest.raises(ValueError, match="itself"):
        FlowConfig.load(p)


def test_depends_on_detects_cycle(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: [b]}\n"
        "  - {name: b, type: coder, role: worker, seniority: junior, "
        "prompt: 'y', depends_on: [a]}\n"
    )
    with pytest.raises(ValueError, match="cycle"):
        FlowConfig.load(p)


def test_depends_on_injects_gate_and_handoff(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: architect, type: system-architect, role: specialist, "
        "seniority: lead, prompt: 'design'}\n"
        "  - {name: coder, type: coder, role: worker, seniority: senior, "
        "prompt: 'build', depends_on: [architect]}\n"
    )
    cfg = FlowConfig.load(p)
    obj = cfg.build_objective()
    # Coder has a gate referencing architect, scoped to 'main'
    assert "GATE — coder MUST NOT start work" in obj
    assert "instructions:main:architect->coder" in obj
    assert "approval:main:architect->coder" in obj
    # Architect has a handoff block referencing coder
    assert "HANDOFF — downstream agents are blocked until architect" in obj
    # depends_on shown in agent header
    assert "depends_on=['architect']" in obj
    # Multi-task scope isolates keys per task name
    obj_t1 = cfg.build_objective(task_body="t1 body", task_name="t1")
    obj_t2 = cfg.build_objective(task_body="t2 body", task_name="t2")
    assert "instructions:t1:architect->coder" in obj_t1
    assert "instructions:t2:architect->coder" in obj_t2
    assert "instructions:t1:architect->coder" not in obj_t2


def test_project_summary_in_objective(tmp_path: Path):
    """project_summary is injected above the deep_think analysis so
    every subtask sees the project's north star at the top."""
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: 'x'}\n"
    )
    cfg = FlowConfig.load(p)
    cfg.task.project_summary = (
        "# The idea\nBuild a REST API in FastAPI.\nStack: Python 3.11, "
        "Postgres, SQLAlchemy.\nSuccess: /users endpoints pass the smoke "
        "suite."
    )
    obj = cfg.build_objective(
        task_body="implement POST /users",
        task_name="task_1",
        analysis="## 1. Project Overview\nFresh repo, no code yet.",
    )
    vision_idx = obj.find("# PROJECT VISION")
    analysis_idx = obj.find("# PROJECT ANALYSIS")
    task_idx = obj.find("# TASK: task_1")
    # Vision comes before analysis and task body
    assert 0 < vision_idx < analysis_idx < task_idx
    assert "Build a REST API in FastAPI" in obj
    assert "Success: /users endpoints" in obj


def test_original_main_injected_in_multi_mode(tmp_path: Path):
    """In multi-task mode every subtask objective carries the raw
    `task.main` so the agent can see the user's original intent, not
    just the decomposer's distilled slice."""
    from rufler.config import FlowConfig, TaskItem

    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: multi-proj\n"
        "task:\n"
        "  main: |\n"
        "    Build a REST API for users.\n"
        "    Must support JWT auth.\n"
        "    Postgres backend.\n"
        "  multi: true\n"
        "agents:\n"
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: x}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    cfg.task.group = [TaskItem(name="task_1", file_path="x.md")]

    obj = cfg.build_objective(task_body="scaffold app", task_name="task_1")
    assert "# ORIGINAL MAIN TASK" in obj
    assert "Build a REST API for users." in obj
    assert "Must support JWT auth." in obj
    # Subtask body still rendered under TASK block
    assert "# TASK: task_1" in obj
    assert "scaffold app" in obj


def test_original_main_skipped_in_mono_mode(tmp_path: Path):
    """Mono-mode objectives already render `task.main` as the TASK body;
    injecting it a second time via ORIGINAL MAIN TASK would duplicate."""
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: mono-proj\n"
        "task:\n  main: 'single task body'\n"
        "agents:\n"
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: x}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    obj = cfg.build_objective()
    assert "ORIGINAL MAIN TASK" not in obj


def test_original_main_truncated_at_cap(tmp_path: Path):
    """Oversized task.main is capped to 1000 lines with a truncation
    marker so every subtask prompt doesn't balloon."""
    from rufler.config import FlowConfig, TaskItem

    main_body = "\n".join(f"line {i}" for i in range(1500))
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: big\n"
        "task:\n  main: |\n"
        + "\n".join(f"    line {i}" for i in range(1500)) + "\n"
        + "  multi: true\n"
        "agents:\n"
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: x}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    cfg.task.group = [TaskItem(name="task_1", file_path="x.md")]
    obj = cfg.build_objective(task_body="slice", task_name="task_1")
    assert "line 0" in obj
    assert "line 999" in obj
    # Line 1000 must be truncated out
    assert "line 1000" not in obj
    assert "line(s) truncated" in obj


def test_decompose_timeout_default_and_plumbing(tmp_path: Path):
    """Decompose timeout defaults to 600s (was 300s, caused production
    hangs on 5 × 80-200 line outputs) and is passed through from yml."""
    from unittest.mock import patch, MagicMock
    from rufler.config import TaskSpec
    from rufler.decomposer import decompose

    assert TaskSpec().decompose_timeout == 600

    stdout = """\
project_summary: |
  ok
tasks:
  - name: task_1
    title: "x"
    content: |
      Do thing.
"""
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = stdout

    captured: dict = {}
    def _capture(cmd, *, log_path=None, timeout=None, phase=None):
        captured["timeout"] = timeout
        return mock_res

    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", side_effect=_capture):
        decompose("x", 1, tmp_path / "t", tmp_path / "d.yml", timeout=900)

    assert captured["timeout"] == 900


def test_deep_think_allowed_tools_default_unrestricted():
    """Deep_think leaves allowed_tools unrestricted by default.

    An earlier attempt restricted this to 'Read,Glob,Grep,Bash' to
    force read-only, but narrow allowlists cause claude -p to exit
    with empty stdout (session init / hooks need tools outside the
    list). Enforcement now lives in the prompt + a side-file fallback
    in tasks/deep_think.py."""
    from rufler.config import TaskSpec
    assert TaskSpec().deep_think_allowed_tools is None


def test_deep_think_prompt_output_contract_at_top():
    """The no-file-writes instruction lives at the very top of the
    prompt (before the persona / rules), not buried in a later Rules
    block — that's where models pay most attention."""
    from rufler.tasks.deep_think import build_deep_think_prompt

    out = build_deep_think_prompt("t")
    # OUTPUT CONTRACT appears before the persona intro
    contract_idx = out.find("OUTPUT CONTRACT")
    persona_idx = out.find("senior software architect")
    rules_idx = out.find("## Rules")
    assert contract_idx != -1 and persona_idx != -1 and rules_idx != -1
    assert contract_idx < persona_idx < rules_idx
    # The contract body explicitly forbids file writes
    contract = out[contract_idx:persona_idx]
    for forbidden in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert forbidden in contract
    assert "directly in your response" in contract.lower()


def test_project_summary_absent_omits_section(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: 'x'}\n"
    )
    cfg = FlowConfig.load(p)
    # Default project_summary is "" — must not emit an empty vision block
    obj = cfg.build_objective(task_body="do thing", task_name="t")
    assert "PROJECT VISION" not in obj


def test_decomposer_parses_project_summary(tmp_path: Path):
    """End-to-end: claude returns YAML with project_summary + tasks;
    decompose() writes it to companion yml and returns it."""
    from unittest.mock import patch, MagicMock
    from rufler.decomposer import decompose

    stdout = """\
project_summary: |
  # North star
  Build a REST API. Stack: FastAPI, Postgres.
  Success: /users + /auth live.
tasks:
  - name: task_1
    title: "Scaffold app"
    content: |
      ## Scope
      Create FastAPI skeleton.
      ## Files
      app/main.py — entry point.
      ## Acceptance
      - `uvicorn app.main:app` boots.
  - name: task_2
    title: "Add DB"
    content: |
      ## Scope
      Wire Postgres via SQLAlchemy.
      ## Files
      app/db.py — engine + session.
      ## Acceptance
      - Connection health check passes.
"""
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = stdout
    out_dir = tmp_path / "tasks"
    yml_out = tmp_path / "decomposed_tasks.yml"

    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", return_value=mock_res):
        result = decompose("big task", 2, out_dir, yml_out)

    assert set(result.keys()) == {"tasks", "project_summary"}
    assert "Build a REST API" in result["project_summary"]
    assert len(result["tasks"]) == 2

    import yaml as _yaml
    saved = _yaml.safe_load(yml_out.read_text(encoding="utf-8"))
    assert "Build a REST API" in saved["task"]["project_summary"]
    assert len(saved["task"]["group"]) == 2


def test_decomposer_anchors_beat_nested_fences(tmp_path: Path):
    """Regression: claude's YAML output can contain fenced code blocks
    (directory trees, code samples) inside `content: |` values. Naive
    `re.search(r'```...```')` grabs the INNER fence and returns a
    non-YAML payload. _extract_yaml must prefer the top-level
    project_summary/tasks anchor over any nested fence."""
    from unittest.mock import patch, MagicMock
    from rufler.decomposer import decompose

    # Mock a realistic claude reply: YAML with a nested tree fence
    # inside project_summary (real failure mode observed in prod).
    stdout = '''\
project_summary: |
  A greenfield project.
  Structure:
  ```
  my-app/
  ├── src/
  └── tests/
  ```
  Stack: Next.js + TypeScript.
tasks:
  - name: task_1
    title: "Scaffold"
    content: |
      ## Scope
      Initial scaffold.
      ## Files
      src/index.ts — entry point.
  - name: task_2
    title: "Implement"
    content: |
      ## Scope
      Implement core.
'''
    mock_res = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", return_value=mock_res):
        result = decompose("x", 2, tmp_path / "t", tmp_path / "d.yml")

    assert len(result["tasks"]) == 2
    assert "A greenfield project" in result["project_summary"]
    # The tree fence inside project_summary survived as-is in the value
    assert "└── tests/" in result["project_summary"]


def test_decomposer_bare_fences_sanitized(tmp_path: Path):
    """Regression: bare triple-backtick lines inside content: | blocks
    cause yaml.safe_load to fail with "found character '`' that cannot
    start any token". _sanitize_fences must convert them to YAML
    comments so parsing succeeds."""
    from unittest.mock import patch, MagicMock
    from rufler.decomposer import decompose

    stdout = '''\
tasks:
  - name: task_1
    title: "Write code"
    content: |
      ## Scope
      Add a function.
      ```rust
      fn main() {
          println!("hello");
      }
      ```
      See above.
'''
    mock_res = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", return_value=mock_res):
        result = decompose("x", 1, tmp_path / "t", tmp_path / "d.yml")

    assert len(result["tasks"]) == 1
    content = (tmp_path / "t" / "task_1.md").read_text()
    assert "fn main()" in content
    assert "println!" in content


def test_decomposer_missing_project_summary_tolerated(tmp_path: Path):
    """If the model omits project_summary, decompose() still returns the
    tasks and sets project_summary to empty string — the caller warns
    but doesn't fail."""
    from unittest.mock import patch, MagicMock
    from rufler.decomposer import decompose

    stdout = """\
tasks:
  - name: task_1
    title: "x"
    content: |
      Do thing.
"""
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = stdout

    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", return_value=mock_res):
        result = decompose("x", 1, tmp_path / "tasks", tmp_path / "dec.yml")

    assert result["project_summary"] == ""
    assert len(result["tasks"]) == 1


def test_depends_on_null_normalizes_to_empty(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: a, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: null}\n"
    )
    cfg = FlowConfig.load(p)  # must not raise
    assert cfg.agents[0].depends_on == []


def test_depends_on_dedupes(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: arch, type: system-architect, role: specialist, "
        "seniority: lead, prompt: 'd'}\n"
        "  - {name: coder, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: [arch, arch, arch]}\n"
    )
    cfg = FlowConfig.load(p)
    assert cfg.agents[1].depends_on == ["arch"]


def test_depends_on_rejects_string_value(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_yml(tmp_path,
        "  - {name: arch, type: system-architect, role: specialist, "
        "seniority: lead, prompt: 'd'}\n"
        "  - {name: coder, type: coder, role: worker, seniority: junior, "
        "prompt: 'x', depends_on: 'arch'}\n"
    )
    with pytest.raises(ValueError, match="must be a list"):
        FlowConfig.load(p)


def _write_flow_with_skills(tmp_path: Path, skills_yaml: str) -> Path:
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: skills-test\n"
        "memory:\n  namespace: st\n"
        "task:\n  main: 'x'\n"
        f"skills:\n{skills_yaml}\n"
        "agents:\n"
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: 'p'}\n",
        encoding="utf-8",
    )
    return p


def test_skills_rejects_unknown_pack(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(tmp_path, "  packs: [nope]\n")
    with pytest.raises(ValueError, match="unknown pack"):
        FlowConfig.load(p)


def test_skills_accepts_known_packs_and_dedupes(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(tmp_path, "  packs: [core, core, github]\n")
    cfg = FlowConfig.load(p)
    assert cfg.skills.packs == ["core", "github"]


def test_skills_rejects_non_list_packs(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(tmp_path, "  packs: core\n")
    with pytest.raises(ValueError, match="must be a list"):
        FlowConfig.load(p)


def test_skills_extra_dedupes_and_strips_blanks(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(tmp_path, "  extra: [foo, foo, '', bar]\n")
    cfg = FlowConfig.load(p)
    assert cfg.skills.extra == ["foo", "bar"]


def test_skills_sh_accepts_string_shorthand(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    # Bare strings that aren't real filesystem dirs fall through to skills.sh
    # only at install time — at parse time they stay as plain strings under
    # `custom`. Use the dict form to get an eager SkillsShEntry.
    p = _write_flow_with_skills(
        tmp_path, "  custom:\n    - source: anthropics/skills\n"
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    assert len(sh) == 1
    e = sh[0]
    assert e.source == "anthropics/skills"
    assert e.skill is None
    assert e.agent == "claude-code"
    assert e.copy is True


def test_skills_sh_accepts_dict_form(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    p = _write_flow_with_skills(
        tmp_path,
        "  custom:\n"
        "    - source: vercel-labs/skills\n"
        "      skill: azure-ai\n"
        "      agent: claude-code\n"
        "      copy: false\n",
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    e = sh[0]
    assert e.source == "vercel-labs/skills"
    assert e.skill == "azure-ai"
    assert e.copy is False


def test_skills_sh_dedupes_by_source_and_skill(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    p = _write_flow_with_skills(
        tmp_path,
        "  custom:\n"
        "    - source: anthropics/skills\n"
        "    - source: anthropics/skills\n"
        "    - source: anthropics/skills\n"
        "      skill: xyz\n",
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    assert len(sh) == 2
    assert sh[0].skill is None
    assert sh[1].skill == "xyz"


def test_skills_sh_parses_pasted_npx_command(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    p = _write_flow_with_skills(
        tmp_path,
        "  custom:\n"
        "    - npx skills add https://github.com/samber/cc-skills-golang "
        "--skill golang-error-handling\n",
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    assert len(sh) == 1
    e = sh[0]
    assert e.source == "https://github.com/samber/cc-skills-golang"
    assert e.skill == "golang-error-handling"
    assert e.agent == "claude-code"
    assert e.copy is True


def test_skills_sh_parses_short_skills_add_command(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    p = _write_flow_with_skills(
        tmp_path,
        "  custom:\n    - skills add owner/repo -s foo -a claude-code --no-copy\n",
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    e = sh[0]
    assert e.source == "owner/repo"
    assert e.skill == "foo"
    assert e.agent == "claude-code"
    assert e.copy is False


def test_skills_sh_rejects_global_flag_in_command(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(
        tmp_path, "  custom:\n    - npx skills add owner/repo -g\n"
    )
    with pytest.raises(ValueError, match="global install"):
        FlowConfig.load(p)


def test_skills_sh_rejects_empty_source(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(tmp_path, "  custom:\n    - source: ''\n")
    with pytest.raises(ValueError, match="non-empty 'source'"):
        FlowConfig.load(p)


def test_skills_sh_rejects_unknown_field(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_skills(
        tmp_path,
        "  custom:\n    - source: a/b\n      wrong: 1\n",
    )
    with pytest.raises(ValueError, match="unknown field"):
        FlowConfig.load(p)


def test_skills_sh_legacy_section_migrates_to_custom(tmp_path: Path):
    from rufler.config import FlowConfig, SkillsShEntry
    p = _write_flow_with_skills(
        tmp_path,
        "  skills_sh:\n    - source: anthropics/skills\n",
    )
    cfg = FlowConfig.load(p)
    sh = [e for e in cfg.skills.custom if isinstance(e, SkillsShEntry)]
    assert len(sh) == 1
    assert sh[0].source == "anthropics/skills"


def test_install_skills_sh_skips_when_npx_missing(tmp_path: Path, monkeypatch):
    from rufler.skills import install_skills_sh
    from rufler.config import SkillsShEntry

    monkeypatch.setattr("rufler.skills.skills_sh.shutil.which", lambda name: None)

    msgs: list[str] = []

    class StubConsole:
        def rule(self, *a, **kw): pass
        def print(self, *a, **kw):
            if a:
                msgs.append(str(a[0]))

    install_skills_sh(
        tmp_path,
        [SkillsShEntry(source="anthropics/skills")],
        StubConsole(),
    )
    assert any("npx" in m for m in msgs)


def test_skills_defaults_enabled_empty(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: s\ntask:\n  main: x\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.skills.enabled is True
    assert cfg.skills.packs == []
    assert cfg.skills.all is False


def test_read_skill_description_yaml_frontmatter(tmp_path: Path):
    from rufler.skills import read_skill_description as _read_skill_description
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: demo\ndescription: \"Hello from yaml\"\n---\n# body\n",
        encoding="utf-8",
    )
    assert _read_skill_description(p) == "Hello from yaml"


def test_read_skill_description_multiline_folded(tmp_path: Path):
    from rufler.skills import read_skill_description as _read_skill_description
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: demo\ndescription: |\n  line one\n  line two\n---\nbody\n",
        encoding="utf-8",
    )
    assert _read_skill_description(p) == "line one line two"


def test_read_skill_description_fallback_first_line(tmp_path: Path):
    from rufler.skills import read_skill_description as _read_skill_description
    p = tmp_path / "SKILL.md"
    p.write_text("# Header\n\nFirst prose line here.\n", encoding="utf-8")
    assert _read_skill_description(p) == "First prose line here."


def test_read_skill_description_missing_file(tmp_path: Path):
    from rufler.skills import read_skill_description as _read_skill_description
    assert _read_skill_description(tmp_path / "nope.md") == "-"


def test_skills_cmd_lists_installed(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner
    from rufler.cli import app
    # Build a project with a flow file + one installed skill.
    (tmp_path / "rufler_flow.yml").write_text(
        "project:\n  name: skills-smoke\n"
        "task:\n  main: x\n"
        "skills:\n  enabled: true\n  packs: [core]\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    skill = tmp_path / ".claude" / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: smoke test skill\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["skills"])
    assert result.exit_code == 0, result.output
    assert "demo-skill" in result.output
    assert "smoke test skill" in result.output
    assert "packs=['core']" in result.output


def test_install_skills_noop_when_disabled(tmp_path: Path):
    from rufler.skills import install_skills as _install_skills
    from rufler.config import FlowConfig, SkillsSpec
    calls: list[tuple] = []

    class StubRunner:
        def init_skills(self, **kwargs):
            calls.append(("init_skills", kwargs))
            return 0

    class StubConsole:
        def rule(self, *a, **kw): pass
        def print(self, *a, **kw): pass

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.skills = SkillsSpec(enabled=False, all=True, packs=["core"])
    _install_skills(StubRunner(), cfg, StubConsole())
    assert calls == []  # disabled → nothing ran


def test_grand_total_tokens_sums_projects(tmp_path: Path):
    reg = Registry(path=tmp_path / "r.json")
    for i, project in enumerate(("a", "b")):
        log = tmp_path / f"{project}.log"
        _write_log(log, [{"input_tokens": (i + 1) * 10, "output_tokens": (i + 1) * 5}])
        e = new_entry(
            project=project, flow_file=tmp_path / "f.yml", base_dir=tmp_path,
            mode="foreground", run_mode="sequential", log_path=log,
        )
        reg.add(e)
        reg.recompute_tokens(e)
    g = reg.grand_total_tokens()
    assert g["input"] == 30  # 10 + 20
    assert g["output"] == 15  # 5 + 10


# ---------- task_markers: emit + scan + derive ----------

def test_emit_and_scan_task_boundaries(tmp_path: Path):
    from rufler.task_markers import emit_task_marker, scan_task_boundaries
    log = tmp_path / "tasks.log"
    emit_task_marker(log, "task_start", task_id="abc12345.01", slot=1, name="schema")
    # Simulate some claude output between markers.
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"src": "claude", "type": "assistant", "message": {}}) + "\n")
    emit_task_marker(log, "task_end", task_id="abc12345.01", slot=1, rc=0)
    boundaries = scan_task_boundaries(log)
    assert "abc12345.01" in boundaries
    tb = boundaries["abc12345.01"]
    assert tb.started is True
    assert tb.ended is True
    assert tb.rc == 0
    assert tb.slot == 1
    assert tb.name == "schema"
    assert tb.start_offset is not None
    assert tb.end_offset is not None
    assert tb.start_offset < tb.end_offset


def test_scan_task_boundaries_missing_file(tmp_path: Path):
    from rufler.task_markers import scan_task_boundaries
    assert scan_task_boundaries(tmp_path / "nope.log") == {}


def test_derive_task_status_queued():
    from rufler.task_markers import derive_task_status
    assert derive_task_status(None, run_status="running", run_rc=None) == "queued"


def test_derive_task_status_running():
    from rufler.task_markers import derive_task_status, TaskBoundary
    tb = TaskBoundary(task_id="x.01", slot=1, started=True, ended=False)
    assert derive_task_status(tb, run_status="running", run_rc=None) == "running"


def test_derive_task_status_exited():
    from rufler.task_markers import derive_task_status, TaskBoundary
    tb = TaskBoundary(task_id="x.01", slot=1, started=True, ended=True, rc=0)
    assert derive_task_status(tb, run_status="done", run_rc=0) == "done"


def test_derive_task_status_failed():
    from rufler.task_markers import derive_task_status, TaskBoundary
    tb = TaskBoundary(task_id="x.01", slot=1, started=True, ended=True, rc=1)
    assert derive_task_status(tb, run_status="failed", run_rc=1) == "failed"


def test_derive_task_status_stopped():
    from rufler.task_markers import derive_task_status, TaskBoundary
    tb = TaskBoundary(task_id="x.01", slot=1, started=True, ended=False)
    assert derive_task_status(tb, run_status="stopped", run_rc=None) == "stopped"


def test_derive_task_status_skipped():
    from rufler.task_markers import derive_task_status
    assert derive_task_status(None, run_status="done", run_rc=0) == "skipped"


# ---------- per-task token accounting ----------

def test_parse_log_range_slices_by_offsets(tmp_path: Path):
    from rufler.tokens import parse_log_range
    log = tmp_path / "multi.log"
    line1 = json.dumps({
        "src": "claude", "type": "assistant",
        "message": {"id": "m1", "usage": {"input_tokens": 100, "output_tokens": 50}},
    }) + "\n"
    line2 = json.dumps({"src": "rufler", "type": "task_end", "task_id": "x.01"}) + "\n"
    line3 = json.dumps({
        "src": "claude", "type": "assistant",
        "message": {"id": "m2", "usage": {"input_tokens": 200, "output_tokens": 80}},
    }) + "\n"
    log.write_bytes(line1.encode() + line2.encode() + line3.encode())

    # parse_log_range skips one line after seeking (to handle landing
    # mid-record), so split_offset should point to the START of the marker
    # line. readline() then skips the marker, and the loop picks up line3.
    split_offset = len(line1.encode())

    # Full scan gets everything
    full = parse_log_range(log)
    assert full.input_tokens == 300
    assert full.output_tokens == 130

    # Slice only task 2 (from split_offset onward)
    t2 = parse_log_range(log, start_offset=split_offset)
    assert t2.input_tokens == 200
    assert t2.output_tokens == 80

    # Slice only task 1 (up to split_offset)
    t1 = parse_log_range(log, end_offset=split_offset)
    assert t1.input_tokens == 100
    assert t1.output_tokens == 50


# ---------- TaskEntry in RunEntry roundtrip ----------

def test_task_entry_roundtrip_in_registry(tmp_path: Path):
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="task-rt", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.tasks = [
        TaskEntry(
            name="build", log_path=str(tmp_path / "build.log"),
            id="abc.01", slot=1, source="group",
            started_at=1000.0, finished_at=1060.0, rc=0,
            input_tokens=42, output_tokens=10,
        ),
        TaskEntry(
            name="test", log_path=str(tmp_path / "test.log"),
            id="abc.02", slot=2, source="group",
        ),
    ]
    reg.add(e)
    loaded = reg.list_all()
    assert len(loaded) == 1
    assert len(loaded[0].tasks) == 2
    t1, t2 = loaded[0].tasks
    assert t1.id == "abc.01"
    assert t1.name == "build"
    assert t1.slot == 1
    assert t1.source == "group"
    assert t1.started_at == 1000.0
    assert t1.finished_at == 1060.0
    assert t1.rc == 0
    assert t1.input_tokens == 42
    assert t2.id == "abc.02"
    assert t2.name == "test"
    assert t2.started_at is None


def test_remove_tasks_specific(tmp_path: Path):
    """remove_tasks with explicit task_ids removes only those tasks."""
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="rt-del", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.tasks = [
        TaskEntry(name="t1", log_path="t1.log", id=f"{e.id}.01", slot=1),
        TaskEntry(name="t2", log_path="t2.log", id=f"{e.id}.02", slot=2),
        TaskEntry(name="t3", log_path="t3.log", id=f"{e.id}.03", slot=3),
    ]
    reg.add(e)
    removed = reg.remove_tasks(e.id, [f"{e.id}.02"])
    assert removed == 1
    loaded = reg.list_all()[0]
    assert len(loaded.tasks) == 2
    assert [t.name for t in loaded.tasks] == ["t1", "t3"]


def test_remove_tasks_all(tmp_path: Path):
    """remove_tasks with task_ids=None removes ALL tasks from the entry."""
    reg = Registry(path=tmp_path / "r.json")
    e = new_entry(
        project="rt-all", flow_file=tmp_path / "f.yml", base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.tasks = [
        TaskEntry(name="t1", log_path="t1.log", id=f"{e.id}.01", slot=1),
        TaskEntry(name="t2", log_path="t2.log", id=f"{e.id}.02", slot=2),
    ]
    reg.add(e)
    removed = reg.remove_tasks(e.id)
    assert removed == 2
    loaded = reg.list_all()[0]
    assert loaded.tasks == []


def test_remove_tasks_nonexistent_run(tmp_path: Path):
    """remove_tasks on a missing run returns 0."""
    reg = Registry(path=tmp_path / "r.json")
    assert reg.remove_tasks("nonexistent") == 0


# ---------- rufler tasks CLI integration ----------

def _patch_registry(monkeypatch, reg: Registry):
    """Patch Registry() to always use `reg`'s path, across all modules."""
    real_init = Registry.__init__

    def patched_init(self, path=None):
        real_init(self, path=reg.path)

    monkeypatch.setattr(Registry, "__init__", patched_init)


def test_tasks_cmd_renders_table(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner
    from rufler.cli import app
    from rufler.task_markers import emit_task_marker

    log = tmp_path / ".rufler" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    emit_task_marker(log, "task_start", task_id="dead0001.01", slot=1, name="build")
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "src": "claude", "type": "assistant",
            "message": {"usage": {"input_tokens": 500, "output_tokens": 120}},
        }) + "\n")
    emit_task_marker(log, "task_end", task_id="dead0001.01", slot=1, rc=0)

    reg = Registry(path=tmp_path / ".rufler" / "registry.json")
    e = RunEntry(
        id="dead0001",
        project="test-proj",
        flow_file=str(tmp_path / "rufler_flow.yml"),
        base_dir=str(tmp_path),
        mode="foreground",
        run_mode="sequential",
        started_at=time.time() - 60,
        finished_at=time.time(),
        pids=[99999999],
        log_path=str(log),
        tasks=[
            TaskEntry(
                name="build", log_path=str(log),
                id="dead0001.01", slot=1, source="group",
                started_at=time.time() - 60, finished_at=time.time(), rc=0,
            ),
        ],
    )
    reg.add(e)

    monkeypatch.chdir(tmp_path)
    _patch_registry(monkeypatch, reg)

    result = CliRunner().invoke(app, ["tasks", "dead0001"])
    assert result.exit_code == 0, result.output
    assert "dead0001.01" in result.output
    assert "build" in result.output
    assert "1 done" in result.output


def test_tasks_cmd_verbose_detail(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner
    from rufler.cli import app
    from rufler.task_markers import emit_task_marker

    log = tmp_path / ".rufler" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    emit_task_marker(log, "task_start", task_id="beef0002.01", slot=1, name="deploy")
    emit_task_marker(log, "task_end", task_id="beef0002.01", slot=1, rc=0)

    reg = Registry(path=tmp_path / ".rufler" / "registry.json")
    e = RunEntry(
        id="beef0002",
        project="detail-proj",
        flow_file=str(tmp_path / "rufler_flow.yml"),
        base_dir=str(tmp_path),
        mode="foreground",
        run_mode="sequential",
        started_at=time.time() - 30,
        finished_at=time.time(),
        pids=[99999999],
        log_path=str(log),
        tasks=[
            TaskEntry(
                name="deploy", log_path=str(log),
                id="beef0002.01", slot=1, source="main",
                started_at=time.time() - 30, finished_at=time.time(), rc=0,
            ),
        ],
    )
    reg.add(e)

    monkeypatch.chdir(tmp_path)
    _patch_registry(monkeypatch, reg)

    result = CliRunner().invoke(app, ["tasks", "beef0002.01", "-v"])
    assert result.exit_code == 0, result.output
    assert "beef0002.01" in result.output
    assert "deploy" in result.output
    assert "TOTAL" in result.output


# ---------- resume logic ----------

def test_find_resumable_run_returns_latest_finished(tmp_path: Path):
    from rufler.tasks import find_resumable_run
    reg = Registry(path=tmp_path / "r.json")
    flow = tmp_path / "flow.yml"
    flow.touch()

    old = new_entry(
        project="p", flow_file=flow, base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "old.log",
    )
    old.finished_at = time.time() - 100
    old.pids = [99999999]
    old.tasks = [TaskEntry(name="t1", log_path="x", id="old.01", slot=1, rc=0,
                           started_at=1.0, finished_at=2.0)]
    reg.add(old)

    newer = new_entry(
        project="p", flow_file=flow, base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "new.log",
    )
    newer.finished_at = time.time() - 10
    newer.pids = [99999999]
    newer.tasks = [TaskEntry(name="t1", log_path="x", id="new.01", slot=1, rc=0,
                             started_at=3.0, finished_at=4.0)]
    reg.add(newer)

    found = find_resumable_run(reg, tmp_path, flow)
    assert found is not None
    assert found.id == newer.id


def test_find_resumable_run_skips_running(tmp_path: Path):
    from rufler.tasks import find_resumable_run
    reg = Registry(path=tmp_path / "r.json")
    flow = tmp_path / "flow.yml"
    flow.touch()

    e = new_entry(
        project="p", flow_file=flow, base_dir=tmp_path,
        mode="foreground", run_mode="sequential", log_path=tmp_path / "x.log",
    )
    e.pids = [os.getpid()]
    e.pid_starttimes = [_pid_starttime(os.getpid()) or 0]
    e.tasks = [TaskEntry(name="t1", log_path="x", id="e.01", slot=1)]
    reg.add(e)

    found = find_resumable_run(reg, tmp_path, flow)
    assert found is None


def test_find_resumable_run_no_match_different_dir(tmp_path: Path):
    from rufler.tasks import find_resumable_run
    reg = Registry(path=tmp_path / "r.json")
    flow = tmp_path / "flow.yml"
    flow.touch()
    other = tmp_path / "other"
    other.mkdir()

    e = new_entry(
        project="p", flow_file=flow, base_dir=other,
        mode="foreground", run_mode="sequential", log_path=other / "x.log",
    )
    e.finished_at = time.time()
    e.pids = [99999999]
    e.tasks = [TaskEntry(name="t1", log_path="x", id="e.01", slot=1, rc=0,
                         started_at=1.0, finished_at=2.0)]
    reg.add(e)

    found = find_resumable_run(reg, tmp_path, flow)
    assert found is None


def test_log_has_agent_output(tmp_path: Path):
    from rufler.tasks.resolve import log_has_agent_output

    # Missing path / None
    assert log_has_agent_output(None) is False
    assert log_has_agent_output(tmp_path / "nope.log") is False

    # Empty log
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert log_has_agent_output(empty) is False

    # Only rufler/ruflo lines, no claude assistant message
    ruflo_only = tmp_path / "ruflo.log"
    ruflo_only.write_text(
        json.dumps({"src": "rufler", "text": "log started"}) + "\n"
        + json.dumps({"src": "ruflo", "level": "info", "text": "init ok"}) + "\n"
        + json.dumps({"src": "rufler", "text": "log ended rc=0"}) + "\n",
        encoding="utf-8",
    )
    assert log_has_agent_output(ruflo_only) is False

    # With a claude assistant message
    with_asst = tmp_path / "asst.log"
    with_asst.write_text(
        json.dumps({"src": "rufler", "text": "log started"}) + "\n"
        + json.dumps({
            "src": "claude", "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }) + "\n",
        encoding="utf-8",
    )
    assert log_has_agent_output(with_asst) is True


def test_completed_task_names_filters_rc_zero(tmp_path: Path):
    """Only tasks with rc=0, finished_at set, AND real claude output in
    the log count as completed. The output check guards against the
    bug where ruflo returns rc=0 without claude actually engaging —
    without it, resume would skip past unfinished work."""
    from rufler.tasks import completed_task_names

    def _write_log(name: str, with_output: bool) -> Path:
        p = tmp_path / f"{name}.log"
        lines = [json.dumps({"ts": 1.0, "src": "rufler", "text": "log started"})]
        if with_output:
            lines.append(json.dumps({
                "ts": 2.0, "src": "claude", "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    done1_log = _write_log("done1", with_output=True)
    done2_log = _write_log("done2", with_output=True)
    phantom_log = _write_log("phantom", with_output=False)

    e = RunEntry(
        id="abc", project="p", flow_file="f", base_dir="/tmp",
        mode="fg", run_mode="seq", started_at=1.0,
        tasks=[
            TaskEntry(name="done1", log_path=str(done1_log), id="a.01",
                      slot=1, rc=0, started_at=1.0, finished_at=2.0),
            TaskEntry(name="failed", log_path=str(done1_log), id="a.02",
                      slot=2, rc=1, started_at=3.0, finished_at=4.0),
            TaskEntry(name="pending", log_path=str(done1_log), id="a.03",
                      slot=3),
            TaskEntry(name="done2", log_path=str(done2_log), id="a.04",
                      slot=4, rc=0, started_at=5.0, finished_at=6.0),
            # rc=0 + finished_at set, but log has NO assistant output —
            # must not be counted as done (fast-fail / phantom completion).
            TaskEntry(name="phantom", log_path=str(phantom_log), id="a.05",
                      slot=5, rc=0, started_at=7.0, finished_at=8.0),
        ],
    )
    done = completed_task_names(e)
    assert set(done.keys()) == {"done1", "done2"}
    assert done["done1"].slot == 1
    assert done["done2"].slot == 4


def test_decompose_reuses_existing_files(tmp_path: Path):
    import yaml as _yaml
    from rufler.config import FlowConfig, TaskSpec

    tasks_dir = tmp_path / ".rufler" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task_1.md").write_text("# Task 1\nDo thing one\n", encoding="utf-8")
    (tasks_dir / "task_2.md").write_text("# Task 2\nDo thing two\n", encoding="utf-8")

    yml_out = tasks_dir / "decomposed_tasks.yml"
    companion = {
        "task": {
            "multi": True,
            "run_mode": "sequential",
            "group": [
                {"name": "task_1", "file_path": "task_1.md"},
                {"name": "task_2", "file_path": "task_2.md"},
            ],
        }
    }
    yml_out.write_text(_yaml.safe_dump(companion), encoding="utf-8")

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.task = TaskSpec(
        main="big task",
        multi=True,
        decompose=True,
        decompose_file=".rufler/tasks/decomposed_tasks.yml",
        decompose_dir=".rufler/tasks",
    )

    class StubConsole:
        def rule(self, *a, **kw): pass
        def print(self, *a, **kw): pass

    from rufler.run_steps import decompose_task_group
    decompose_task_group(cfg, StubConsole(), force_new=False)

    assert len(cfg.task.group) == 2
    assert cfg.task.group[0].name == "task_1"
    assert cfg.task.group[1].name == "task_2"


def test_wait_for_log_end_detects_dead_supervisor(tmp_path: Path):
    """Regression: claude/logwriter can die via SIGKILL (OOM, terminal
    close) without writing the 'log ended rc=N' marker. If the
    supervisor pid is dead AND the log stopped growing, bail out of the
    wait — otherwise rufler sits on a corpse for the full task
    timeout (up to 4h by default)."""
    from rufler.process.daemon import wait_for_log_end

    log = tmp_path / "task.log"
    log.write_text("some content\nbut no 'log ended' marker\n", encoding="utf-8")

    class StubConsole:
        def __init__(self): self.messages = []
        def print(self, *a, **kw): self.messages.append(a)

    stub = StubConsole()
    # PID 1 (init) is alive on any POSIX system, but we want a DEAD pid.
    # 0 is reserved + _pid_alive treats non-positive as dead.
    dead_pid = 0

    import time as _time
    start = _time.time()
    found, rc = wait_for_log_end(
        log, timeout_sec=60, console=stub,
        start_offset=0,
        supervisor_pid=dead_pid,
        stale_threshold_sec=1,  # bail quickly for test
    )
    elapsed = _time.time() - start

    assert found is False
    assert rc is None
    # Must have bailed quickly — well under the 60s timeout
    assert elapsed < 10, f"waited {elapsed:.1f}s — early-bail didn't fire"
    joined = " ".join(str(m) for m in stub.messages)
    assert "supervisor" in joined and "gone" in joined


def test_decompose_cache_partial_files_regenerates(tmp_path: Path):
    """Cache validation: if yml lists N subtasks but only M (< N) files
    exist on disk, the cache is considered invalid and rufler triggers
    regeneration instead of silently proceeding with a truncated list."""
    import yaml as _yaml
    from unittest.mock import patch
    from rufler.config import FlowConfig, TaskSpec

    tasks_dir = tmp_path / ".rufler" / "tasks"
    tasks_dir.mkdir(parents=True)
    # Only task_1 exists; task_2 / task_3 are listed in yml but missing
    (tasks_dir / "task_1.md").write_text("# task_1\n", encoding="utf-8")

    yml_out = tasks_dir / "decomposed_tasks.yml"
    companion = {"task": {"multi": True, "run_mode": "sequential",
                          "group": [
                              {"name": "task_1", "file_path": "task_1.md"},
                              {"name": "task_2", "file_path": "task_2.md"},
                              {"name": "task_3", "file_path": "task_3.md"},
                          ]}}
    yml_out.write_text(_yaml.safe_dump(companion), encoding="utf-8")

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.task = TaskSpec(
        main="big task",
        multi=True,
        decompose=True,
        decompose_file=".rufler/tasks/decomposed_tasks.yml",
        decompose_dir=".rufler/tasks",
    )

    class StubConsole:
        def __init__(self):
            self.messages: list = []
        def rule(self, *a, **kw): self.messages.append(("rule", a, kw))
        def print(self, *a, **kw): self.messages.append(("print", a, kw))

    stub = StubConsole()
    from rufler.run_steps import decompose_task_group
    from click.exceptions import Exit
    # `_claude_bin` stubbed so the fresh decompose attempt fails
    # deterministically — we only care that regeneration was TRIGGERED,
    # not that it succeeded.
    with patch("rufler.decomposer._claude_bin", return_value=None):
        with pytest.raises(Exit):
            decompose_task_group(cfg, stub, force_new=False)

    # Message about cache-invalidation must have been printed
    joined = " ".join(str(m) for m in stub.messages)
    assert "cache invalid" in joined or "regenerating" in joined
    assert "missing" in joined.lower() or "subtask" in joined.lower()


def test_decompose_retries_on_parse_failure(tmp_path: Path):
    """On decomposer parse failure the CLI retries once before failing."""
    from unittest.mock import patch, MagicMock
    from rufler.config import FlowConfig, TaskSpec

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.task = TaskSpec(
        main="x",
        multi=True,
        decompose=True,
        decompose_count=1,
        decompose_file=".rufler/tasks/decomposed_tasks.yml",
        decompose_dir=".rufler/tasks",
    )

    class StubConsole:
        def __init__(self):
            self.messages: list = []
        def rule(self, *a, **kw): self.messages.append(a)
        def print(self, *a, **kw): self.messages.append(a)

    stub = StubConsole()
    from rufler.run_steps import decompose_task_group
    from click.exceptions import Exit

    # First call raises, second call returns valid result.
    good_stdout = '''\
project_summary: |
  ok
tasks:
  - name: task_1
    title: "x"
    content: |
      Do it.
'''
    mock_good = MagicMock(returncode=0, stdout=good_stdout, stderr="")
    call_count = {"n": 0}
    def flaky(cmd, *, log_path=None, timeout=None, phase=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate invalid YAML -> decompose raises RuntimeError
            return MagicMock(returncode=0, stdout="not yaml at all", stderr="")
        return mock_good

    with patch("rufler.decomposer._claude_bin", return_value="/usr/bin/claude"), \
         patch("rufler.stream_log.stream_claude", side_effect=flaky):
        decompose_task_group(cfg, stub, force_new=False)

    assert call_count["n"] == 2, f"expected 2 attempts, got {call_count['n']}"
    assert len(cfg.task.group) == 1
    joined = " ".join(str(m) for m in stub.messages)
    assert "retry" in joined.lower()


def test_decompose_ignores_existing_when_force_new(tmp_path: Path):
    """With force_new=True, decompose should NOT reuse files and should
    invoke the decomposer instead. We mock out `claude` so the call is
    deterministically absent regardless of test-host PATH."""
    import yaml as _yaml
    from unittest.mock import patch
    from rufler.config import FlowConfig, TaskSpec

    tasks_dir = tmp_path / ".rufler" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task_1.md").write_text("# old\n", encoding="utf-8")

    yml_out = tasks_dir / "decomposed_tasks.yml"
    companion = {"task": {"multi": True, "run_mode": "sequential",
                          "group": [{"name": "task_1", "file_path": "task_1.md"}]}}
    yml_out.write_text(_yaml.safe_dump(companion), encoding="utf-8")

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.task = TaskSpec(
        main="big task",
        multi=True,
        decompose=True,
        decompose_file=".rufler/tasks/decomposed_tasks.yml",
        decompose_dir=".rufler/tasks",
    )

    class StubConsole:
        def rule(self, *a, **kw): pass
        def print(self, *a, **kw): pass

    from rufler.run_steps import decompose_task_group
    from click.exceptions import Exit
    # Force `claude` to appear missing inside the decomposer so it raises
    # and decompose_task_group escalates to Exit(1) — proving it tried to
    # call the decomposer instead of silently reusing the stale yml.
    with patch("rufler.decomposer._claude_bin", return_value=None):
        with pytest.raises(Exit):
            decompose_task_group(cfg, StubConsole(), force_new=True)


# ---------- MCP config parsing ----------

def _write_flow_with_mcp(tmp_path: Path, mcp_yaml: str) -> Path:
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: mcp-test\n"
        "task:\n  main: x\n"
        f"mcp:\n{mcp_yaml}\n"
        "agents:\n"
        "  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    return p


def test_mcp_parses_stdio_server(tmp_path: Path):
    from rufler.config import FlowConfig, McpServerSpec
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: my-db\n"
        "      command: npx\n"
        "      args: ['-y', '@anthropic/mcp-postgres']\n"
        "      env:\n"
        "        DATABASE_URL: 'postgresql://localhost/mydb'\n"
    )
    cfg = FlowConfig.load(p)
    assert len(cfg.mcp.servers) == 1
    s = cfg.mcp.servers[0]
    assert s.name == "my-db"
    assert s.command == "npx"
    assert s.args == ["-y", "@anthropic/mcp-postgres"]
    assert s.env == {"DATABASE_URL": "postgresql://localhost/mydb"}
    assert s.transport == "stdio"


def test_mcp_parses_http_server(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: sentry\n"
        "      transport: http\n"
        "      url: 'https://mcp.sentry.dev/mcp'\n"
    )
    cfg = FlowConfig.load(p)
    s = cfg.mcp.servers[0]
    assert s.transport == "http"
    assert s.url == "https://mcp.sentry.dev/mcp"
    assert s.command == ""


def test_mcp_parses_http_with_headers(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: corridor\n"
        "      transport: http\n"
        "      url: 'https://app.corridor.dev/api/mcp'\n"
        "      headers:\n"
        "        Authorization: 'Bearer token123'\n"
    )
    cfg = FlowConfig.load(p)
    s = cfg.mcp.servers[0]
    assert s.headers == {"Authorization": "Bearer token123"}


def test_mcp_rejects_duplicate_names(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: dup\n"
        "      command: echo\n"
        "    - name: dup\n"
        "      command: echo\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        FlowConfig.load(p)


def test_mcp_rejects_stdio_without_command(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: broken\n"
    )
    with pytest.raises(ValueError, match="command"):
        FlowConfig.load(p)


def test_mcp_rejects_http_without_url(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: broken\n"
        "      transport: http\n"
    )
    with pytest.raises(ValueError, match="url"):
        FlowConfig.load(p)


def test_mcp_rejects_unknown_fields(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path,
        "  servers:\n"
        "    - name: bad\n"
        "      command: echo\n"
        "      bogus: true\n"
    )
    with pytest.raises(ValueError, match="unknown"):
        FlowConfig.load(p)


def test_mcp_empty_servers_is_valid(tmp_path: Path):
    from rufler.config import FlowConfig
    p = _write_flow_with_mcp(tmp_path, "  servers: []\n")
    cfg = FlowConfig.load(p)
    assert cfg.mcp.servers == []


def test_mcp_no_section_defaults_empty(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: no-mcp\ntask:\n  main: x\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.mcp.servers == []


# ---------- report config parsing ----------

def test_report_defaults_enabled(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: rpt\ntask:\n  main: x\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.task.on_task_complete.report is True
    assert cfg.task.on_complete.report is True
    assert cfg.task.on_task_complete.report_path == ".rufler/reports/{task}.md"
    assert cfg.task.on_complete.report_path == ".rufler/report.md"


def test_report_custom_prompt(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: rpt\ntask:\n  main: x\n"
        "  on_complete:\n"
        "    report: true\n"
        "    report_prompt: 'Custom final summary.'\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.task.on_complete.report_prompt == "Custom final summary."
    assert cfg.task.on_complete.report is True


def test_report_disabled(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: rpt\ntask:\n  main: x\n"
        "  on_task_complete:\n    report: false\n"
        "  on_complete: false\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.task.on_task_complete.report is False
    assert cfg.task.on_complete.report is False


def test_report_custom_path(tmp_path: Path):
    from rufler.config import FlowConfig
    p = tmp_path / "rufler_flow.yml"
    p.write_text(
        "project:\n  name: rpt\ntask:\n  main: x\n"
        "  on_task_complete:\n    report_path: docs/reports/{task}.md\n"
        "  on_complete:\n    report_path: docs/FINAL.md\n"
        "agents:\n  - {name: a, type: coder, role: worker, seniority: junior, prompt: p}\n",
        encoding="utf-8",
    )
    cfg = FlowConfig.load(p)
    assert cfg.task.on_task_complete.report_path == "docs/reports/{task}.md"
    assert cfg.task.on_complete.report_path == "docs/FINAL.md"


def test_report_prompt_resolution():
    from rufler.config import ReportSpec
    from rufler.tasks.report import _resolve_prompt
    from pathlib import Path

    spec = ReportSpec(report_prompt="Custom: {project} done.")
    result = _resolve_prompt(
        spec, Path("/tmp"), is_final=True,
        task_name="final", project="myapp", ns="default",
        report_path=".rufler/report.md",
    )
    assert result == "Custom: myapp done."

    spec_default = ReportSpec()
    result_default = _resolve_prompt(
        spec_default, Path("/tmp"), is_final=True,
        task_name="final", project="myapp", ns="default",
        report_path=".rufler/report.md",
    )
    assert "myapp" in result_default
    assert "report" in result_default.lower()


def test_report_default_prompt_demands_write_tool():
    """Report prompts must explicitly tell claude to call the Write
    tool at the absolute path — otherwise claude sometimes returns the
    markdown inline, leaving no file on disk."""
    from rufler.tasks.report import (
        DEFAULT_TASK_REPORT_PROMPT, DEFAULT_FINAL_REPORT_PROMPT,
    )
    for tpl in (DEFAULT_TASK_REPORT_PROMPT, DEFAULT_FINAL_REPORT_PROMPT):
        assert "Write" in tpl
        assert "absolute path" in tpl
        # No ambiguous "respond with" / "return the report" instruction
        assert "return the report inline" in tpl.lower() or "not return" in tpl.lower()


def test_run_report_marks_failed_when_file_missing(tmp_path: Path):
    """Regression: rc=0 from the claude subprocess does NOT prove the
    report was written. Without the file-exists check, run_report
    used to print '[green] written' while the report file was missing,
    causing chain retrospectives and final reports to silently skip."""
    from unittest.mock import MagicMock
    from rufler.tasks.report import run_report
    from rufler.config import FlowConfig, ReportSpec

    cfg = FlowConfig()
    cfg.base_dir = tmp_path
    cfg.project.name = "p"

    class StubRunner:
        def hive_spawn_claude(self, **_):
            # Simulate claude exiting cleanly WITHOUT writing the file
            return 0

    from rufler.registry import RunEntry
    reg_entry = RunEntry(
        id="abc", project="p", flow_file="f", base_dir=str(tmp_path),
        mode="fg", run_mode="seq", started_at=1.0,
    )

    class StubRegistry:
        def __init__(self): self.updates = 0
        def update(self, e): self.updates += 1

    class StubConsole:
        def __init__(self): self.messages = []
        def print(self, *a, **kw): self.messages.append(str(a))

    class StubEff:
        from pathlib import Path as _P
        log_file = _P(".rufler/run.log")

    spec = ReportSpec(report=True, report_path=".rufler/reports/{task}.md")
    stub_con = StubConsole()
    run_report(
        cfg, StubRunner(), reg_entry, StubRegistry(), StubEff(),
        stub_con, spec=spec, task_name="task_1", source="task_report",
    )
    # The last appended task entry is the report — rc must be 1
    te = reg_entry.tasks[-1]
    assert te.rc == 1, f"expected rc=1 when report file missing, got {te.rc}"
    # Message must have called out the missing file
    joined = " ".join(stub_con.messages)
    assert "missing" in joined.lower()
