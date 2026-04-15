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
