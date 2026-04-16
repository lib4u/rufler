"""Data models and constants for rufler_flow.yml configuration.

All @dataclass types live here except :class:`FlowConfig`, which holds
the parser and lives in :mod:`.loader`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------- Constants ---------------

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


# --------------- Agent ---------------

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
            raise ValueError(
                f"agent '{self.name}': invalid role '{self.role}' "
                f"(must be one of {sorted(VALID_ROLES)})"
            )
        if self.seniority not in VALID_SENIORITY:
            raise ValueError(
                f"agent '{self.name}': invalid seniority '{self.seniority}'"
            )
        if not self.prompt and not self.prompt_path:
            raise ValueError(
                f"agent '{self.name}': must have 'prompt' or 'prompt_path'"
            )
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


# --------------- Swarm ---------------

@dataclass
class SwarmSpec:
    topology: str = "hierarchical"
    max_agents: int = 8
    strategy: str = "specialized"
    consensus: str = "raft"


# --------------- Skills ---------------

@dataclass
class SkillsShEntry:
    """A single entry under ``skills.custom`` — installed via the skills.sh
    CLI (``npx skills add <source>``).
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
    """Global Claude Code skills installed into ``.claude/skills/`` before the
    swarm runs."""
    enabled: bool = True
    all: bool = False
    packs: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    custom: list[str | SkillsShEntry] = field(default_factory=list)
    clean: bool = False

    def validate(self) -> None:
        from .loader import _parse_skills_sh_command  # avoid circular

        if not isinstance(self.packs, list):
            raise ValueError(
                f"skills.packs must be a list, got {type(self.packs).__name__}"
            )
        if not isinstance(self.extra, list):
            raise ValueError(
                f"skills.extra must be a list, got {type(self.extra).__name__}"
            )
        if not isinstance(self.custom, list):
            raise ValueError(
                f"skills.custom must be a list, got {type(self.custom).__name__}"
            )
        seen: set[str] = set()
        cleaned: list[str] = []
        for p in self.packs:
            if not isinstance(p, str):
                raise ValueError(
                    f"skills.packs entries must be strings, "
                    f"got {type(p).__name__}"
                )
            if p not in VALID_SKILL_PACKS:
                raise ValueError(
                    f"skills.packs: unknown pack '{p}' "
                    f"(known: {sorted(VALID_SKILL_PACKS)})"
                )
            if p not in seen:
                seen.add(p)
                cleaned.append(p)
        self.packs = cleaned

        seen_extra: set[str] = set()
        cleaned_extra: list[str] = []
        for s in self.extra:
            if not isinstance(s, str):
                raise ValueError(
                    f"skills.extra entries must be strings, "
                    f"got {type(s).__name__}"
                )
            if not s.strip():
                continue
            if s not in seen_extra:
                seen_extra.add(s)
                cleaned_extra.append(s)
        self.extra = cleaned_extra

        if not isinstance(self.custom, list):
            raise ValueError(
                f"skills.custom must be a list, got {type(self.custom).__name__}"
            )
        seen_path: set[str] = set()
        seen_sh: set[tuple[str, Optional[str]]] = set()
        cleaned_custom: list[str | SkillsShEntry] = []

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


# --------------- Memory ---------------

@dataclass
class MemorySpec:
    backend: str = "hybrid"
    namespace: str = "default"
    init: bool = True
    checkpoint_interval_minutes: int = 5


# --------------- Task ---------------

@dataclass
class TaskItem:
    """A single subtask inside a multi-task group."""
    name: str
    file_path: Optional[str] = None
    content: Optional[str] = None
    chain: Optional[bool] = None

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
class ReportSpec:
    """Report generation settings — shared shape for per-task and final reports."""
    report: bool = True
    report_path: str = ""
    report_prompt: Optional[str] = None
    report_prompt_path: Optional[str] = None


@dataclass
class TaskSpec:
    main: str = ""
    main_path: Optional[str] = None
    autonomous: bool = True
    max_iterations: int = 100
    timeout_minutes: int = 180

    multi: bool = False
    run_mode: str = "sequential"
    decompose: bool = False
    decompose_count: int = 4
    decompose_dir: str = ".rufler/tasks"
    decompose_file: str = ".rufler/tasks/decomposed_tasks.yml"
    decompose_prompt: Optional[str] = None
    decompose_prompt_path: Optional[str] = None
    decompose_model: str = "sonnet"
    decompose_effort: str = "high"

    deep_think: bool = False
    deep_think_model: str = "opus"
    deep_think_output: str = ".rufler/analysis.md"
    deep_think_prompt: Optional[str] = None
    deep_think_prompt_path: Optional[str] = None
    deep_think_timeout: int = 600
    deep_think_budget: Optional[float] = None
    deep_think_effort: str = "max"
    deep_think_allowed_tools: Optional[str] = None
    group: list[TaskItem] = field(default_factory=list)

    chain: bool = False
    chain_max_tokens: int = 2000
    chain_include_report: bool = True

    on_task_complete: ReportSpec = field(
        default_factory=lambda: ReportSpec(report_path=".rufler/reports/{task}.md")
    )
    on_complete: ReportSpec = field(
        default_factory=lambda: ReportSpec(report_path=".rufler/report.md")
    )

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
        if not self.multi:
            body = self.resolved_main(base)
            return [("main", body)] if body else []
        return [(item.name, item.resolved(base)) for item in self.group]


# --------------- Project ---------------

@dataclass
class ProjectSpec:
    name: str = "rufler-project"
    description: str = ""


# --------------- MCP ---------------

@dataclass
class McpServerSpec:
    """One MCP server to register with Claude Code via ``claude mcp add``."""
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError(
                "mcp.servers: each entry must have a non-empty 'name'"
            )
        self.name = self.name.strip()
        if self.transport not in ("stdio", "http", "sse"):
            raise ValueError(
                f"mcp.servers '{self.name}': transport must be stdio, http, "
                f"or sse, got '{self.transport}'"
            )
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(
                    f"mcp.servers '{self.name}': stdio transport requires 'command'"
                )
        else:
            if not self.url:
                raise ValueError(
                    f"mcp.servers '{self.name}': {self.transport} transport "
                    f"requires 'url'"
                )


@dataclass
class McpSpec:
    """MCP servers to register with Claude Code before the swarm runs."""
    servers: list[McpServerSpec] = field(default_factory=list)

    def validate(self) -> None:
        seen: set[str] = set()
        for s in self.servers:
            s.validate()
            if s.name in seen:
                raise ValueError(f"mcp.servers: duplicate name '{s.name}'")
            seen.add(s.name)


VALID_MCP_SERVER_FIELDS = set(McpServerSpec.__dataclass_fields__)


# --------------- Execution ---------------

@dataclass
class ExecutionSpec:
    """How ``rufler start`` should launch the Claude Code swarm."""
    non_interactive: bool = False
    yolo: bool = False
    background: bool = False
    log_file: str = ".rufler/run.log"
