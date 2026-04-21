"""Tasks subpackage — resolve, display, report, and inspect per-task state.

Public API re-exported here so cli.py can do a single
`from .tasks import resolve_tasks_for_entry, ...` instead of reaching
into submodules.
"""
from .resolve import resolve_tasks_for_entry, find_resumable_run, completed_task_names
from .display import (
    TASK_STATUS_COLORS,
    fmt_duration,
    fmt_ts,
    render_task_detail,
    render_task_log_tail,
    render_tasks_table,
    render_tokens_by_task,
)
from .report import run_report
from .chain import (
    ChainedTask,
    build_retrospective,
    collect_chain_entry,
    compress_task_context,
    resolve_chain_flag,
)
from .deep_think import deep_think, build_deep_think_prompt
from .judge import JudgeResult, build_judge_prompt, judge_iteration

__all__ = [
    "TASK_STATUS_COLORS",
    "completed_task_names",
    "find_resumable_run",
    "fmt_duration",
    "fmt_ts",
    "render_task_detail",
    "render_task_log_tail",
    "render_tasks_table",
    "render_tokens_by_task",
    "resolve_tasks_for_entry",
    "run_report",
    "ChainedTask",
    "build_retrospective",
    "collect_chain_entry",
    "compress_task_context",
    "resolve_chain_flag",
    "deep_think",
    "build_deep_think_prompt",
    "JudgeResult",
    "build_judge_prompt",
    "judge_iteration",
]
