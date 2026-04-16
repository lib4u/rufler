"""Task status + token resolution — the core logic behind `rufler tasks`.

Pure computation layer: reads log files for boundary markers and token
usage, derives task status from run state. No console output, no side
effects beyond populating TaskEntry token fields in-place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..registry import Registry, RunEntry, TaskEntry
from ..task_markers import scan_task_boundaries, derive_task_status
from ..tokens import parse_log_range


def resolve_tasks_for_entry(
    entry: RunEntry,
) -> list[tuple[TaskEntry, str, int]]:
    """Derive (task, status, total_tokens) for every task in a run.

    Scans logs lazily — only reads the bytes needed for boundary detection
    and token counting. Returns a list parallel to entry.tasks.
    """
    results: list[tuple[TaskEntry, str, int]] = []
    for te in entry.tasks:
        lp = Path(te.log_path) if te.log_path else None
        boundaries = scan_task_boundaries(lp) if lp else {}
        tb = boundaries.get(te.id)
        status = derive_task_status(
            tb, run_status=entry.status, run_rc=entry.exit_code,
        )

        if te.finished_at and te.rc is not None:
            status_from_entry = "done" if te.rc == 0 else "failed"
            if status in ("queued", "skipped", "dead"):
                status = status_from_entry

        if te.started_at and tb and not tb.started:
            if status == "queued":
                status = "running" if entry.status == "running" else "done"

        tok = 0
        if lp and lp.exists():
            start_off = tb.start_offset if tb and tb.start_offset is not None else 0
            end_off = tb.end_offset if tb and tb.end_offset is not None else None
            usage = parse_log_range(lp, start_offset=start_off, end_offset=end_off)
            tok = usage.total
            te.input_tokens = usage.input_tokens
            te.output_tokens = usage.output_tokens
            te.cache_read = usage.cache_read
            te.cache_creation = usage.cache_creation

        results.append((te, status, tok))
    return results


def find_resumable_run(
    registry: Registry,
    base_dir: Path,
    flow_file: Path,
) -> Optional[RunEntry]:
    """Find the most recent non-running run for the same project directory
    and flow file. Used by `rufler run` to detect previous progress.

    Returns None if no matching finished/stopped/failed run exists.
    """
    resolved_base = base_dir.resolve()
    resolved_flow = flow_file.resolve()
    best: Optional[RunEntry] = None
    for entry in registry.list_refreshed(include_all=True):
        if entry.status == "running":
            continue
        if not entry.tasks:
            continue
        try:
            if Path(entry.base_dir).resolve() != resolved_base:
                continue
            if Path(entry.flow_file).resolve() != resolved_flow:
                continue
        except (ValueError, OSError):
            continue
        if best is None or entry.started_at > best.started_at:
            best = entry
    return best


def completed_task_names(entry: RunEntry) -> dict[str, TaskEntry]:
    """Return a mapping of {name: TaskEntry} for tasks that finished
    successfully (rc == 0) in the given run.

    Used by the resume logic to decide which tasks to skip.
    """
    out: dict[str, TaskEntry] = {}
    for te in entry.tasks:
        if te.rc is not None and te.rc == 0 and te.finished_at is not None:
            out[te.name] = te
    return out
