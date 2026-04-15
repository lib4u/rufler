"""Tasks subpackage — resolve, display, and inspect per-task state.

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
]
