from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


VALID_ROLES = {"queen", "specialist", "worker", "scout"}
VALID_SENIORITY = {"lead", "senior", "junior"}
VALID_RUN_MODES = {"sequential", "parallel"}

# Skill pack taxonomy — mirrors SKILLS_MAP in ruflo's
# v3/@claude-flow/cli/src/init/executor.ts. Split by install mechanism:
#   CLI_FLAG_PACKS      → ruflo exposes a `--<pack>` flag on `init skills`
#   MANUAL_COPY_PACKS   → no CLI flag, rufler copies directories itself
# Keep in sync manually; an unknown pack is a load-time error so users catch
# typos early.
CLI_FLAG_PACKS = {"core", "agentdb", "github", "v3"}
MANUAL_COPY_PACKS = {"flowNexus", "browser", "dualMode"}
VALID_SKILL_PACKS = CLI_FLAG_PACKS | MANUAL_COPY_PACKS

# Heuristic directory-name prefixes used when copying MANUAL_COPY_PACKS from
# ruflo's bundled source. Best-effort: if a prefix matches 0 dirs the user
# gets a warning rather than silent zero-copy.
MANUAL_PACK_PREFIXES: dict[str, tuple[str, ...]] = {
    "flowNexus": ("flow-nexus", "nexus"),
    "browser": ("browser",),
    "dualMode": ("dual-mode", "dual", "codex"),
}


def _task_item_from_dict(name: str, item: dict) -> "TaskItem":
    """Build a TaskItem, tolerating unknown yml keys by filtering to known fields."""
    allowed = {f for f in TaskItem.__dataclass_fields__ if f != "name"}
    kwargs = {k: v for k, v in item.items() if k in allowed}
    return TaskItem(name=name, **kwargs)


def _parse_task(raw: dict) -> "TaskSpec":
    """Parse a task: section. Supports mono + multi (group | decompose)."""
    group_raw = raw.get("group")
    group: list[TaskItem] = []
    if isinstance(group_raw, dict):
        # group: { task_1: {file_path: ...}, task_2: {...} }
        for name, item in group_raw.items():
            if isinstance(item, dict):
                group.append(_task_item_from_dict(str(name), item))
            elif isinstance(item, str):
                group.append(TaskItem(name=str(name), file_path=item))
    elif isinstance(group_raw, list):
        # group: [ {name: task_1, file_path: ...}, ... ]
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

    allowed = set(TaskSpec.__dataclass_fields__)
    kwargs = {k: v for k, v in raw.items() if k in allowed and k != "group"}
    spec = TaskSpec(**kwargs)
    spec.group = group
    if spec.run_mode not in VALID_RUN_MODES:
        raise ValueError(
            f"task.run_mode '{spec.run_mode}' invalid — must be one of {sorted(VALID_RUN_MODES)}"
        )
    return spec


@dataclass
class AgentSpec:
    name: str
    type: str = "worker"
    role: str = "worker"
    seniority: str = "junior"
    prompt: Optional[str] = None
    prompt_path: Optional[str] = None
    # Soft DAG: this agent must wait for each listed agent's approval in
    # shared memory before starting work. Enforced via prompt-injected GATE
    # block, not OS-level scheduling — see FlowConfig.build_objective.
    depends_on: list[str] = field(default_factory=list)

    def validate(self):
        if self.role not in VALID_ROLES:
            raise ValueError(f"agent '{self.name}': invalid role '{self.role}' (must be one of {sorted(VALID_ROLES)})")
        if self.seniority not in VALID_SENIORITY:
            raise ValueError(f"agent '{self.name}': invalid seniority '{self.seniority}'")
        if not self.prompt and not self.prompt_path:
            raise ValueError(f"agent '{self.name}': must have 'prompt' or 'prompt_path'")
        # Normalize depends_on: yaml `null` → [], strings → reject explicitly,
        # non-string list items → reject. Also dedupe while preserving order.
        if self.depends_on is None:
            self.depends_on = []
        elif not isinstance(self.depends_on, list):
            raise ValueError(
                f"agent '{self.name}': depends_on must be a list of agent names, "
                f"got {type(self.depends_on).__name__}"
            )
        else:
            seen: set[str] = set()
            cleaned: list[str] = []
            for d in self.depends_on:
                if not isinstance(d, str):
                    raise ValueError(
                        f"agent '{self.name}': depends_on entries must be strings, "
                        f"got {type(d).__name__}"
                    )
                if d not in seen:
                    seen.add(d)
                    cleaned.append(d)
            self.depends_on = cleaned

    def resolved_prompt(self, base: Path) -> str:
        if self.prompt:
            return self.prompt.strip()
        if self.prompt_path:
            p = (base / self.prompt_path).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(
                    f"agent '{self.name}': prompt_path not found: {p}"
                )
            return p.read_text(encoding="utf-8").strip()
        return ""


@dataclass
class SwarmSpec:
    topology: str = "hierarchical"
    max_agents: int = 8
    strategy: str = "specialized"
    consensus: str = "raft"


def _parse_skills_sh_command(raw: str) -> "SkillsShEntry":
    """Parse a full `npx skills add …` (or `skills add …`) command line into
    a SkillsShEntry. Lets users paste the exact command from skills.sh into
    the yml verbatim:

        skills_sh:
          - npx skills add https://github.com/foo/bar --skill baz
          - skills add owner/repo -s name -a claude-code

    Recognised flags (skills.sh CLI): `-s`/`--skill`, `-a`/`--agent`,
    `--copy`/`--no-copy`, `-y`/`--yes` (ignored — rufler always passes it).
    Unknown flags / `-g` / `--global` cause a load-time error.
    """
    import shlex

    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        raise ValueError(f"skills.skills_sh: cannot parse command '{raw}': {e}")

    # Strip a leading `npx` (+ its own flags like `-y`) then `skills add`.
    if tokens and tokens[0] == "npx":
        tokens = tokens[1:]
        # drop bare npx flags: `-y`, `--yes`, and ignore any `--package`/`-p`
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
            # rufler always passes --yes; silently tolerate it in pasted commands
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


@dataclass
class SkillsShEntry:
    """A single entry under `skills.skills_sh` — installed via the skills.sh
    CLI (`npx skills add <source>`) from https://skills.sh. The source can be
    any repo reference the `skills` CLI accepts: `owner/repo`, a full
    GitHub/GitLab URL, or a `.../tree/<branch>/skills/<name>` subpath.

    Rufler runs `npx skills add <source> --yes --copy` from `<project>` so the
    resulting files land in `<project>/.claude/skills/<skill-name>/SKILL.md`.
    `--copy` (not symlinks) is the default so the install is self-contained
    and safe to commit / diff.

    - `source`: required. `owner/repo`, URL, or repo subpath.
    - `skill`: optional single skill name to install from a multi-skill repo
      (equivalent to the CLI's `-s`/`--skill` flag). `"*"` = all.
    - `agent`: target agent identifier. Default `claude-code` so installs
      land in `.claude/skills/` where ruflo/rufler can see them.
    - `copy`: pass `--copy` to skills CLI. Default true — symlinks to a
      global cache break when the repo is cloned elsewhere.
    """
    source: str = ""
    skill: Optional[str] = None
    agent: str = "claude-code"
    copy: bool = True

    def validate(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError(
                "skills.skills_sh: each entry must have a non-empty 'source'"
            )
        self.source = self.source.strip()
        if self.skill is not None:
            if not isinstance(self.skill, str) or not self.skill.strip():
                raise ValueError(
                    f"skills.skills_sh '{self.source}': 'skill' must be a "
                    f"non-empty string or omitted"
                )
            self.skill = self.skill.strip()
        if not isinstance(self.agent, str) or not self.agent.strip():
            raise ValueError(
                f"skills.skills_sh '{self.source}': 'agent' must be a "
                f"non-empty string (default: claude-code)"
            )
        self.agent = self.agent.strip()


@dataclass
class SkillsSpec:
    """Global Claude Code skills installed into `.claude/skills/` before the
    swarm runs. Resolved to `ruflo init skills --<pack>` flags. Per-agent
    binding is a future feature — today all installed skills are visible to
    every agent in the hive-mind session.

    - `enabled`: master toggle. False → skip the skills install step entirely.
    - `all`: convenience flag equivalent to every pack at once.
    - `packs`: subset of VALID_SKILL_PACKS to install.
    - `extra`: individual skill directory names to copy from ruflo's source
      on top of the packs (e.g. a single standalone skill without a pack).
    - `custom`: unified list of user-defined skill sources. Each entry is
      one of:
        * **Local path** — absolute, `~`, or relative to the yml file. Copied
          into `<project>/.claude/skills/<basename>`.
        * **skills.sh shorthand** — `owner/repo` or a full GitHub/GitLab URL.
          Installed via `npx skills add <source>` from https://skills.sh.
        * **Pasted skills.sh command** — a literal `npx skills add …` or
          `skills add …` line, parsed for flags (`-s`, `-a`, `--copy`, …).
        * **Dict form** — `{source, skill?, agent?, copy?}` for explicit
          skills.sh installs (equivalent to `SkillsShEntry`).
      rufler resolves each string at install time: it tries the filesystem
      first (local path) and falls back to skills.sh if the path does not
      exist. Strings starting with `npx `/`skills ` are always treated as
      skills.sh commands (never as paths).
    - `clean`: wipe every non-symlinked dir under `<project>/.claude/skills/`
      AFTER `ruflo init` (which bundles ~30 default skills) and BEFORE rufler
      installs packs/extra/custom from yml. Default false — keeps ruflo's
      init defaults on top of whatever yml declares. Set true to make the
      yml the single source of truth.
    """
    enabled: bool = True
    all: bool = False
    packs: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    # After validate() each entry is EITHER a plain string (local path OR
    # skills.sh shorthand, resolved at install time) OR a SkillsShEntry.
    custom: list = field(default_factory=list)
    clean: bool = False

    def validate(self) -> None:
        if not isinstance(self.packs, list):
            raise ValueError(f"skills.packs must be a list, got {type(self.packs).__name__}")
        if not isinstance(self.extra, list):
            raise ValueError(f"skills.extra must be a list, got {type(self.extra).__name__}")
        if not isinstance(self.custom, list):
            raise ValueError(f"skills.custom must be a list, got {type(self.custom).__name__}")
        # Dedupe while preserving order + validate pack names.
        seen: set[str] = set()
        cleaned: list[str] = []
        for p in self.packs:
            if not isinstance(p, str):
                raise ValueError(f"skills.packs entries must be strings, got {type(p).__name__}")
            if p not in VALID_SKILL_PACKS:
                raise ValueError(
                    f"skills.packs: unknown pack '{p}' "
                    f"(known: {sorted(VALID_SKILL_PACKS)})"
                )
            if p not in seen:
                seen.add(p)
                cleaned.append(p)
        self.packs = cleaned
        # Dedupe extra; no hard whitelist because rufler can't reliably know
        # every skill shipped by ruflo's bundled source tree at load time.
        seen_extra: set[str] = set()
        cleaned_extra: list[str] = []
        for s in self.extra:
            if not isinstance(s, str):
                raise ValueError(f"skills.extra entries must be strings, got {type(s).__name__}")
            if not s.strip():
                continue
            if s not in seen_extra:
                seen_extra.add(s)
                cleaned_extra.append(s)
        self.extra = cleaned_extra
        # custom: unified list. Each entry becomes EITHER a stripped path
        # string (resolved at install time) OR a SkillsShEntry.
        #   - dict → SkillsShEntry (explicit skills.sh install)
        #   - str starting with `npx `/`skills ` → parsed command → SkillsShEntry
        #   - str with '/', ':', '@', or looking like owner/repo but no '/' is
        #     still kept as str — the installer tries filesystem first, then
        #     falls back to skills.sh shorthand.
        if not isinstance(self.custom, list):
            raise ValueError(
                f"skills.custom must be a list, got {type(self.custom).__name__}"
            )
        seen_path: set[str] = set()
        seen_sh: set[tuple[str, Optional[str]]] = set()
        cleaned_custom: list = []

        def _add_sh(entry: SkillsShEntry) -> None:
            entry.validate()
            key = (entry.source, entry.skill)
            if key in seen_sh:
                return
            seen_sh.add(key)
            cleaned_custom.append(entry)

        for raw in self.custom:
            if isinstance(raw, dict):
                allowed = set(SkillsShEntry.__dataclass_fields__)
                unknown = set(raw.keys()) - allowed
                if unknown:
                    raise ValueError(
                        f"skills.custom: unknown field(s) {sorted(unknown)} "
                        f"in dict entry (allowed: {sorted(allowed)})"
                    )
                _add_sh(SkillsShEntry(**raw))
            elif isinstance(raw, SkillsShEntry):
                _add_sh(raw)
            elif isinstance(raw, str):
                stripped = raw.strip()
                if not stripped:
                    continue
                if stripped.startswith(("npx ", "skills ")):
                    _add_sh(_parse_skills_sh_command(stripped))
                elif stripped not in seen_path:
                    seen_path.add(stripped)
                    cleaned_custom.append(stripped)
            else:
                raise ValueError(
                    f"skills.custom entries must be str or dict, "
                    f"got {type(raw).__name__}"
                )
        self.custom = cleaned_custom


@dataclass
class MemorySpec:
    backend: str = "hybrid"
    namespace: str = "default"
    init: bool = True
    # How often agents should flush state to shared memory during a run.
    # 0 disables the periodic-checkpoint clause in the objective. Default 5m.
    checkpoint_interval_minutes: int = 5


@dataclass
class TaskItem:
    """A single subtask inside a multi-task group."""
    name: str
    file_path: Optional[str] = None
    content: Optional[str] = None  # inline alternative to file_path

    def resolved(self, base: Path) -> str:
        if self.content and self.content.strip():
            return self.content.strip()
        if self.file_path:
            p = (base / self.file_path).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(
                    f"task '{self.name}': file_path not found: {p}"
                )
            return p.read_text(encoding="utf-8").strip()
        raise ValueError(f"task '{self.name}': needs file_path or content")


@dataclass
class TaskSpec:
    main: str = ""
    main_path: Optional[str] = None
    autonomous: bool = True
    max_iterations: int = 100
    timeout_minutes: int = 180

    # Multi-task mode
    multi: bool = False
    run_mode: str = "sequential"        # sequential | parallel
    decompose: bool = False              # let AI decompose `main` into subtasks
    decompose_count: int = 4             # how many subtasks to generate
    decompose_dir: str = ".rufler/tasks" # where to write generated files
    decompose_file: str = ".rufler/tasks/decomposed_tasks.yml"
    decompose_prompt: Optional[str] = None       # inline override for decomposer prompt
    decompose_prompt_path: Optional[str] = None  # md file override for decomposer prompt
    group: list[TaskItem] = field(default_factory=list)

    def resolved_main(self, base: Path) -> str:
        if self.main and self.main.strip():
            return self.main.strip()
        if self.main_path:
            p = (base / self.main_path).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(f"task.main_path not found: {p}")
            return p.read_text(encoding="utf-8").strip()
        return ""

    def iter_tasks(self, base: Path) -> list[tuple[str, str]]:
        """Return [(name, resolved_body), ...].

        - mono mode → single (`main`, <resolved main>)
        - multi with group → one entry per item
        - multi decompose → caller must have populated group already.
        """
        if not self.multi:
            body = self.resolved_main(base)
            return [("main", body)] if body else []
        return [(item.name, item.resolved(base)) for item in self.group]


@dataclass
class ProjectSpec:
    name: str = "rufler-project"
    description: str = ""


@dataclass
class ExecutionSpec:
    """How `rufler start` should launch the Claude Code swarm.
    CLI flags override these values."""
    non_interactive: bool = False
    yolo: bool = False              # --dangerously-skip-permissions
    background: bool = False        # detach from terminal
    log_file: str = ".rufler/run.log"


@dataclass
class FlowConfig:
    project: ProjectSpec = field(default_factory=ProjectSpec)
    memory: MemorySpec = field(default_factory=MemorySpec)
    swarm: SwarmSpec = field(default_factory=SwarmSpec)
    task: TaskSpec = field(default_factory=TaskSpec)
    execution: ExecutionSpec = field(default_factory=ExecutionSpec)
    skills: SkillsSpec = field(default_factory=SkillsSpec)
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
            # Soft-migration: merge legacy `skills_sh:` entries into `custom`.
            # The dedicated field was removed — custom now accepts the same
            # shapes (dict, pasted command, shorthand). We keep reading the
            # old key so existing yml files don't break silently.
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
        for a in data.get("agents") or []:
            spec = AgentSpec(**a)
            spec.validate()
            cfg.agents.append(spec)
        cfg._validate_dependencies()
        return cfg

    def _validate_dependencies(self) -> None:
        """Check depends_on references resolve to known agents and that the
        dependency graph has no cycles. Raises ValueError on either failure."""
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
        # Cycle detection via DFS.
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

    def build_objective(self, task_body: Optional[str] = None, task_name: str = "main") -> str:
        lines: list[str] = []
        lines.append(f"# PROJECT: {self.project.name}")
        if self.project.description:
            lines.append(self.project.description.strip())
        lines.append("")
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

        # Sort lead > senior > junior for clarity
        order = {"lead": 0, "senior": 1, "junior": 2}
        sorted_agents = sorted(self.agents, key=lambda a: order.get(a.seniority, 99))

        ns = self.memory.namespace
        # Scope handoff keys per task so two parallel/sequential tasks sharing
        # the same agent set + memory namespace can't pick up each other's
        # briefs or approvals. `main` for mono mode keeps the keys readable.
        task_scope = task_name or "main"
        # Collect downstream map so we can tell each agent who is waiting on it.
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

        # Resume awareness + periodic checkpointing. A previous rufler run may
        # have been interrupted — agents must probe shared memory for prior
        # progress AND continuously persist their own state so the next run
        # (or another agent) can recover.
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
