"""Configuration package — models, constants, and YAML loader.

Re-exports every public name so that ``from rufler.config import X``
continues to work after the split from a single ``config.py`` module.
"""
from .loader import FlowConfig, _parse_skills_sh_command, _parse_task, _task_item_from_dict
from .models import (
    VALID_ROLES,
    VALID_SENIORITY,
    VALID_RUN_MODES,
    CLI_FLAG_PACKS,
    MANUAL_COPY_PACKS,
    VALID_SKILL_PACKS,
    MANUAL_PACK_PREFIXES,
    VALID_MCP_SERVER_FIELDS,
    AgentSpec,
    SwarmSpec,
    SkillsShEntry,
    SkillsSpec,
    MemorySpec,
    TaskItem,
    ReportSpec,
    TaskSpec,
    ProjectSpec,
    McpServerSpec,
    McpSpec,
    ExecutionSpec,
)

__all__ = [
    # Constants
    "VALID_ROLES",
    "VALID_SENIORITY",
    "VALID_RUN_MODES",
    "CLI_FLAG_PACKS",
    "MANUAL_COPY_PACKS",
    "VALID_SKILL_PACKS",
    "MANUAL_PACK_PREFIXES",
    "VALID_MCP_SERVER_FIELDS",
    # Dataclasses
    "AgentSpec",
    "SwarmSpec",
    "SkillsShEntry",
    "SkillsSpec",
    "MemorySpec",
    "TaskItem",
    "ReportSpec",
    "TaskSpec",
    "ProjectSpec",
    "McpServerSpec",
    "McpSpec",
    "ExecutionSpec",
    "FlowConfig",
    # Parse helpers (semi-public — used by models.py deferred import)
    "_parse_skills_sh_command",
    "_parse_task",
    "_task_item_from_dict",
]
