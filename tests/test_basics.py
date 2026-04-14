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

from rufler.cli import _fmt_age, _resolve_log_path, DEFAULT_LOG_REL
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
    assert e.status == "exited"
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
    for u in assistant_usages:
        lines.append(json.dumps({
            "src": "claude",
            "type": "assistant",
            "message": {"usage": u},
        }))
    # Also throw in some noise the parser must ignore.
    lines.append(json.dumps({"src": "rufler", "text": "log started"}))
    lines.append("not json")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_log_sums_assistant_usage(tmp_path: Path):
    from rufler.tokens import parse_log
    log = tmp_path / "x.log"
    _write_log(log, [
        {"input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
        {"input_tokens": 3, "output_tokens": 2,
         "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0},
    ])
    u = parse_log(log)
    assert u.input_tokens == 13
    assert u.output_tokens == 7
    assert u.cache_read == 150
    assert u.cache_creation == 7
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
