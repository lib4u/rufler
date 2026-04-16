"""YAML parser and the top-level :class:`FlowConfig` dataclass.

Everything that touches ``yaml.safe_load`` or builds a ``FlowConfig``
from raw dicts lives here. Data-only models live in :mod:`.models`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import (
    AgentSpec,
    ExecutionSpec,
    McpServerSpec,
    McpSpec,
    MemorySpec,
    ProjectSpec,
    ReportSpec,
    SkillsShEntry,
    SkillsSpec,
    SwarmSpec,
    TaskItem,
    TaskSpec,
    VALID_MCP_SERVER_FIELDS,
    VALID_RUN_MODES,
)
from ..templates import DENY_RULES_PROMPT


# --------------- Parse helpers ---------------

def _task_item_from_dict(name: str, item: dict) -> TaskItem:
    """Build a TaskItem, tolerating unknown yml keys by filtering to known fields."""
    allowed = {f for f in TaskItem.__dataclass_fields__ if f != "name"}
    kwargs = {k: v for k, v in item.items() if k in allowed}
    return TaskItem(name=name, **kwargs)


def _parse_task(raw: dict) -> TaskSpec:
    """Parse a task: section. Supports mono + multi (group | decompose)."""
    group_raw = raw.get("group")
    group: list[TaskItem] = []
    if isinstance(group_raw, dict):
        for name, item in group_raw.items():
            if isinstance(item, dict):
                group.append(_task_item_from_dict(str(name), item))
            elif isinstance(item, str):
                group.append(TaskItem(name=str(name), file_path=item))
    elif isinstance(group_raw, list):
        for i, item in enumerate(group_raw):
            if isinstance(item, dict):
                name = str(item.get("name") or f"task_{i + 1}")
                group.append(_task_item_from_dict(name, item))
            elif isinstance(item, str):
                group.append(TaskItem(name=f"task_{i + 1}", file_path=item))
    elif group_raw is not None:
        raise ValueError(
            f"task.group must be dict or list, got {type(group_raw).__name__}"
        )

    on_task_raw = raw.pop("on_task_complete", None)
    on_complete_raw = raw.pop("on_complete", None)

    allowed = set(TaskSpec.__dataclass_fields__) - {"on_task_complete", "on_complete"}
    kwargs = {k: v for k, v in raw.items() if k in allowed and k != "group"}
    spec = TaskSpec(**kwargs)
    spec.group = group

    if isinstance(on_task_raw, dict):
        report_fields = set(ReportSpec.__dataclass_fields__)
        filtered = {k: v for k, v in on_task_raw.items() if k in report_fields}
        spec.on_task_complete = ReportSpec(**filtered)
        if not spec.on_task_complete.report_path:
            spec.on_task_complete.report_path = ".rufler/reports/{task}.md"
    elif on_task_raw is False:
        spec.on_task_complete = ReportSpec(report=False)

    if isinstance(on_complete_raw, dict):
        report_fields = set(ReportSpec.__dataclass_fields__)
        filtered = {k: v for k, v in on_complete_raw.items() if k in report_fields}
        spec.on_complete = ReportSpec(**filtered)
        if not spec.on_complete.report_path:
            spec.on_complete.report_path = ".rufler/report.md"
    elif on_complete_raw is False:
        spec.on_complete = ReportSpec(report=False)

    if spec.run_mode not in VALID_RUN_MODES:
        raise ValueError(
            f"task.run_mode '{spec.run_mode}' invalid — must be one of {sorted(VALID_RUN_MODES)}"
        )
    return spec


def _parse_skills_sh_command(raw: str) -> SkillsShEntry:
    """Parse a full ``npx skills add …`` (or ``skills add …``) command line
    into a :class:`SkillsShEntry`.

    Recognised flags (skills.sh CLI): ``-s``/``--skill``, ``-a``/``--agent``,
    ``--copy``/``--no-copy``, ``-y``/``--yes`` (ignored).
    Unknown flags / ``-g`` / ``--global`` cause a load-time error.
    """
    import shlex

    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        raise ValueError(f"skills.skills_sh: cannot parse command '{raw}': {e}")

    if tokens and tokens[0] == "npx":
        tokens = tokens[1:]
        while tokens and tokens[0] in ("-y", "--yes"):
            tokens = tokens[1:]
    if len(tokens) >= 2 and tokens[0] == "skills" and tokens[1] == "add":
        tokens = tokens[2:]
    else:
        raise ValueError(
            f"skills.skills_sh: command must start with `npx skills add …` "
            f"or `skills add …`, got: {raw!r}"
        )

    source: Optional[str] = None
    skill: Optional[str] = None
    agent: str = "claude-code"
    copy: bool = True

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-s", "--skill"):
            if i + 1 >= len(tokens):
                raise ValueError(f"skills.skills_sh: {tok} needs a value in: {raw!r}")
            skill = tokens[i + 1]
            i += 2
        elif tok.startswith("--skill="):
            skill = tok.split("=", 1)[1]
            i += 1
        elif tok in ("-a", "--agent"):
            if i + 1 >= len(tokens):
                raise ValueError(f"skills.skills_sh: {tok} needs a value in: {raw!r}")
            agent = tokens[i + 1]
            i += 2
        elif tok.startswith("--agent="):
            agent = tok.split("=", 1)[1]
            i += 1
        elif tok == "--copy":
            copy = True
            i += 1
        elif tok == "--no-copy":
            copy = False
            i += 1
        elif tok in ("-y", "--yes"):
            i += 1
        elif tok in ("-g", "--global"):
            raise ValueError(
                f"skills.skills_sh: `{tok}` (global install) is not supported — "
                f"rufler installs skills into the project's .claude/skills/ "
                f"so they're part of the flow. Drop the flag."
            )
        elif tok.startswith("-"):
            raise ValueError(
                f"skills.skills_sh: unsupported flag `{tok}` in command: {raw!r}"
            )
        else:
            if source is not None:
                raise ValueError(
                    f"skills.skills_sh: more than one positional source in "
                    f"command ({source!r} and {tok!r}): {raw!r}"
                )
            source = tok
            i += 1

    if not source:
        raise ValueError(f"skills.skills_sh: no source in command: {raw!r}")
    return SkillsShEntry(source=source, skill=skill, agent=agent, copy=copy)


# --------------- FlowConfig ---------------

@dataclass
class FlowConfig:
    project: ProjectSpec = field(default_factory=ProjectSpec)
    memory: MemorySpec = field(default_factory=MemorySpec)
    swarm: SwarmSpec = field(default_factory=SwarmSpec)
    task: TaskSpec = field(default_factory=TaskSpec)
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)
    skills: SkillsSpec = field(default_factory=SkillsSpec)
    mcp: McpSpec = field(default_factory=McpSpec)
    agents: list[AgentSpec] = field(default_factory=list)
    base_dir: Path = field(default_factory=Path.cwd)

    @classmethod
    def load(cls, path: Path) -> "FlowConfig":
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = cls(base_dir=path.parent.resolve())
        if isinstance(data.get("project"), dict):
            cfg.project = ProjectSpec(**data["project"])
        if isinstance(data.get("memory"), dict):
            cfg.memory = MemorySpec(**data["memory"])
        if isinstance(data.get("swarm"), dict):
            cfg.swarm = SwarmSpec(**data["swarm"])
        if isinstance(data.get("task"), dict):
            cfg.task = _parse_task(data["task"])
        if isinstance(data.get("execution"), dict):
            cfg.execution = ExecutionSpec(**data["execution"])
        if isinstance(data.get("skills"), dict):
            raw_skills = dict(data["skills"])
            legacy_sh = raw_skills.pop("skills_sh", None)
            if legacy_sh:
                if not isinstance(legacy_sh, list):
                    raise ValueError(
                        f"skills.skills_sh (deprecated, moved to 'custom'): "
                        f"must be a list, got {type(legacy_sh).__name__}"
                    )
                raw_skills["custom"] = list(raw_skills.get("custom") or []) + legacy_sh
            cfg.skills = SkillsSpec(**raw_skills)
            cfg.skills.validate()
        if isinstance(data.get("mcp"), dict):
            raw_mcp = data["mcp"]
            servers_raw = raw_mcp.get("servers") or []
            if not isinstance(servers_raw, list):
                raise ValueError(
                    f"mcp.servers must be a list, got {type(servers_raw).__name__}"
                )
            mcp_servers: list[McpServerSpec] = []
            for i, s in enumerate(servers_raw):
                if not isinstance(s, dict):
                    raise ValueError(
                        f"mcp.servers[{i}] must be a dict, got {type(s).__name__}"
                    )
                unknown = set(s.keys()) - VALID_MCP_SERVER_FIELDS
                if unknown:
                    raise ValueError(
                        f"mcp.servers[{i}]: unknown field(s) {sorted(unknown)} "
                        f"(allowed: {sorted(VALID_MCP_SERVER_FIELDS)})"
                    )
                mcp_servers.append(McpServerSpec(**s))
            cfg.mcp = McpSpec(servers=mcp_servers)
            cfg.mcp.validate()
        for a in data.get("agents") or []:
            spec = AgentSpec(**a)
            spec.validate()
            cfg.agents.append(spec)
        cfg._validate_dependencies()
        return cfg

    def _validate_dependencies(self) -> None:
        """Check depends_on references resolve to known agents and that the
        dependency graph has no cycles."""
        names = {a.name for a in self.agents}
        for a in self.agents:
            for dep in a.depends_on:
                if dep == a.name:
                    raise ValueError(f"agent '{a.name}': depends_on cannot reference itself")
                if dep not in names:
                    raise ValueError(
                        f"agent '{a.name}': depends_on '{dep}' is not a known agent "
                        f"(known: {sorted(names)})"
                    )
        graph = {a.name: list(a.depends_on) for a in self.agents}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}

        def visit(n: str, path: list[str]) -> None:
            if color[n] == GRAY:
                cycle = " -> ".join(path + [n])
                raise ValueError(f"agent dependency cycle: {cycle}")
            if color[n] == BLACK:
                return
            color[n] = GRAY
            for m in graph[n]:
                visit(m, path + [n])
            color[n] = BLACK

        for n in graph:
            visit(n, [])

    def build_objective(
        self,
        task_body: Optional[str] = None,
        task_name: str = "main",
        previous_tasks: Optional[list] = None,
        analysis: Optional[str] = None,
    ) -> str:
        from ..tasks.chain import build_retrospective

        lines: list[str] = []
        # DENY_RULES_PROMPT is always first — takes precedence over every
        # downstream instruction (agent prompts, task body, chain history).
        lines.append(DENY_RULES_PROMPT.rstrip())
        lines.append("")
        lines.append(f"# PROJECT: {self.project.name}")
        if self.project.description:
            lines.append(self.project.description.strip())
        lines.append("")

        if analysis:
            lines.append("# PROJECT ANALYSIS (from deep_think phase — read-only context)")
            lines.append(analysis.strip())
            lines.append("")
            lines.append("---")
            lines.append("")

        if previous_tasks:
            lines.append(build_retrospective(previous_tasks))

        body = task_body if task_body is not None else self.task.resolved_main(self.base_dir)
        header = "MAIN TASK" if task_name == "main" else f"TASK: {task_name}"
        lines.append(f"# {header}")
        lines.append(body)
        lines.append("")
        lines.append(
            f"# AGENT TEAM ({len(self.agents)} agents)  "
            f"— coordinate via shared memory namespace '{self.memory.namespace}'"
        )
        lines.append("")

        order = {"lead": 0, "senior": 1, "junior": 2}
        sorted_agents = sorted(self.agents, key=lambda a: order.get(a.seniority, 99))

        ns = self.memory.namespace
        task_scope = task_name or "main"
        downstream: dict[str, list[str]] = {a.name: [] for a in self.agents}
        for a in self.agents:
            for dep in a.depends_on:
                if dep in downstream:
                    downstream[dep].append(a.name)

        for a in sorted_agents:
            header_extra = (
                f", depends_on={a.depends_on}" if a.depends_on else ""
            )
            lines.append(
                f"## {a.name}  [type={a.type}, role={a.role}, "
                f"seniority={a.seniority}{header_extra}]"
            )
            body = a.resolved_prompt(self.base_dir)
            lines.append(body if body else "(no prompt)")

            if a.depends_on:
                lines.append("")
                lines.append(f"### GATE — {a.name} MUST NOT start work until upstream agents approve")
                lines.append(
                    f"Before doing ANY work, read these keys from shared memory "
                    f"namespace '{ns}'. If any are missing, poll memory_search "
                    f"every ~30s and DO NOT write code, files, or decisions until "
                    f"all gates are open:"
                )
                for dep in a.depends_on:
                    lines.append(
                        f"  - memory_retrieve namespace='{ns}' "
                        f"key='instructions:{task_scope}:{dep}->{a.name}'  "
                        f"(your work brief from {dep}; required)"
                    )
                    lines.append(
                        f"  - memory_retrieve namespace='{ns}' "
                        f"key='approval:{task_scope}:{dep}->{a.name}'  "
                        f"(must equal 'approved' before you may proceed)"
                    )
                lines.append(
                    f"If a gate stays closed >10 minutes, write "
                    f"key='blockers' value='waiting on <upstream>' and keep polling. "
                    f"NEVER bypass a gate, even if you 'know what to do' — the "
                    f"upstream agent owns the contract."
                )

            waiters = downstream.get(a.name, [])
            if waiters:
                lines.append("")
                lines.append(f"### HANDOFF — downstream agents are blocked until {a.name} signs off")
                lines.append(
                    f"The following agents will not start until you publish their "
                    f"brief AND approval to shared memory namespace '{ns}': "
                    f"{', '.join(waiters)}."
                )
                for w in waiters:
                    lines.append(
                        f"  - memory_store namespace='{ns}' "
                        f"key='instructions:{task_scope}:{a.name}->{w}' "
                        f"value='<concrete work brief for {w}: scope, files, "
                        f"interfaces, acceptance criteria>'"
                    )
                    lines.append(
                        f"  - memory_store namespace='{ns}' "
                        f"key='approval:{task_scope}:{a.name}->{w}' value='approved'  "
                        f"(write this LAST, only after the brief is in place "
                        f"and you are confident {w} can proceed)"
                    )
                lines.append(
                    "If you must reject a downstream agent's request for approval, "
                    f"write value='rejected: <reason>' instead — they will halt "
                    f"and surface the blocker."
                )
            lines.append("")

        if self.task.autonomous:
            lines.append("# EXECUTION MODE: AUTONOMOUS")
            lines.append(
                "Work autonomously without asking for confirmation. "
                "Create all files, run builds, write tests, iterate until done. "
                "Use shared memory namespace above for cross-agent state."
            )
            lines.append("")

        interval = self.memory.checkpoint_interval_minutes
        lines.append("# RESUME AWARENESS")
        lines.append(
            f"Before starting, search the shared memory namespace '{ns}' for "
            f"prior progress on this task. Look for keys like 'progress', "
            f"'checkpoint:latest', 'last_step', 'completed', 'decisions', "
            f"'blockers'. A previous run may have been interrupted — if you "
            f"find relevant state, continue from there instead of redoing work."
        )
        lines.append("")
        lines.append("# CHECKPOINT DISCIPLINE (do not skip — this is how we avoid losing work)")
        if interval > 0:
            lines.append(
                f"Write a checkpoint to shared memory namespace '{ns}' "
                f"EVERY {interval} MINUTES of wall-clock work, AND immediately "
                f"after completing any of: a file write, a test run, a build, "
                f"a sub-task, or a decision. A run can be killed at any moment "
                f"(terminal closed, power loss, SIGKILL) — anything not in "
                f"memory at the moment of the kill is lost forever."
            )
        else:
            lines.append(
                f"Write a checkpoint to shared memory namespace '{ns}' "
                f"immediately after completing any of: a file write, a test "
                f"run, a build, a sub-task, or a decision. A run can be killed "
                f"at any moment — anything not in memory is lost forever."
            )
        lines.append(
            "Use these exact keys so recovery is deterministic:"
        )
        lines.append(
            f"  - memory_store namespace='{ns}' key='checkpoint:latest' "
            f"value='<compact JSON: current_step, done_steps[], next_step, "
            f"open_questions[], last_ts>'"
        )
        lines.append(
            f"  - memory_store namespace='{ns}' key='checkpoint:<unix_ts>' "
            f"value='<same compact JSON>'  (timestamped snapshot for history)"
        )
        lines.append(
            f"  - memory_store namespace='{ns}' key='progress' "
            f"value='<one-line human summary: what is done, what is next>'"
        )
        lines.append(
            f"  - memory_store namespace='{ns}' key='decisions' "
            f"value='<list of design decisions made so far>'  (append as you go)"
        )
        lines.append(
            f"  - memory_store namespace='{ns}' key='blockers' "
            f"value='<anything stuck on, with context>'  (clear when unstuck)"
        )
        lines.append(
            "Rule of thumb: if you just spent >2 minutes on something you "
            "could not reconstruct from the repo alone, it MUST go into memory "
            "before your next tool call."
        )
        return "\n".join(lines)
