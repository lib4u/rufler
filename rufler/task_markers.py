"""Task boundary markers inside the NDJSON run log.

Rufler writes `task_start` / `task_end` markers directly into the run log so
`rufler tasks` can derive per-task status, byte ranges, and tokens *lazily*
from the log itself — there is no separate mutable task-state file to race
against. This mirrors the existing `log ended rc=N` convention the logwriter
already emits.

Record shape (one NDJSON line per marker, src="rufler"):

    {"ts": 1776200000.1, "src": "rufler", "type": "task_start",
     "task_id": "a1b2c3d4.01", "slot": 1, "name": "build_backend",
     "offset": 12345}

    {"ts": 1776200500.9, "src": "rufler", "type": "task_end",
     "task_id": "a1b2c3d4.01", "slot": 1, "rc": 0, "offset": 67890}

`offset` is the file position at marker-write time — used by
`rufler.tokens.parse_log_range` to slice out just the byte range that
belongs to each task for per-task token accounting. `task_start.offset`
points to where the task's output will start; `task_end.offset` is the
position right after the marker line itself (so the next task_start
slots in cleanly).

Status derivation (`derive_task_status`):
  - has task_start, no task_end, log still growing / pid alive → "running"
  - has task_start + task_end rc==0 → "exited"
  - has task_start + task_end rc!=0 → "failed"
  - task_end missing but log tail shows `log ended rc=N`
    (parallel mode fallback — orchestrator detached before writing end) → "exited"/"failed"
  - no task_start, run still queued → "queued"
  - no task_start, run finished → "skipped"
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def emit_task_marker(
    log_path: Path,
    evt: str,
    *,
    task_id: str,
    slot: int,
    name: Optional[str] = None,
    rc: Optional[int] = None,
) -> None:
    """Append a single `task_start` / `task_end` NDJSON record to `log_path`.

    Tolerates a missing parent directory (the log may not have been opened
    yet for the very first task in parallel-background mode). Silently
    swallows IO errors — markers are best-effort observability, never
    load-bearing.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        with open(log_path, "ab") as f:
            f.flush()
            offset = f.tell()
            rec: dict = {
                "ts": time.time(),
                "src": "rufler",
                "type": evt,
                "task_id": task_id,
                "slot": int(slot),
                "offset": int(offset),
            }
            if name is not None:
                rec["name"] = name
            if rc is not None:
                rec["rc"] = int(rc)
            f.write((json.dumps(rec) + "\n").encode("utf-8"))
    except OSError:
        return


@dataclass
class TaskBoundary:
    """Parsed task_start/task_end pair from an NDJSON log."""
    task_id: str
    slot: int
    name: str = ""
    start_offset: Optional[int] = None
    end_offset: Optional[int] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    rc: Optional[int] = None
    started: bool = False
    ended: bool = False


def scan_task_boundaries(log_path: Path) -> dict[str, TaskBoundary]:
    """Walk `log_path` and collect one `TaskBoundary` per unique task_id.

    Handles both sequential-multi (one shared log with interleaved markers)
    and parallel-multi / mono (one log per task, single pair). Unknown or
    malformed records are skipped silently. Key = task_id.
    """
    out: dict[str, TaskBoundary] = {}
    if not log_path or not log_path.exists():
        return out
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or not ln.startswith("{"):
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("src") != "rufler":
                    continue
                t = rec.get("type")
                if t not in ("task_start", "task_end"):
                    continue
                tid = str(rec.get("task_id") or "")
                if not tid:
                    continue
                tb = out.get(tid)
                if tb is None:
                    tb = TaskBoundary(
                        task_id=tid,
                        slot=int(rec.get("slot") or 0),
                        name=str(rec.get("name") or ""),
                    )
                    out[tid] = tb
                ts = rec.get("ts")
                off = rec.get("offset")
                if t == "task_start":
                    tb.started = True
                    tb.start_offset = int(off) if isinstance(off, (int, float)) else None
                    tb.started_at = float(ts) if isinstance(ts, (int, float)) else None
                    if not tb.name and rec.get("name"):
                        tb.name = str(rec.get("name"))
                else:
                    tb.ended = True
                    tb.end_offset = int(off) if isinstance(off, (int, float)) else None
                    tb.finished_at = float(ts) if isinstance(ts, (int, float)) else None
                    rc = rec.get("rc")
                    if isinstance(rc, (int, float)):
                        tb.rc = int(rc)
    except OSError:
        return out
    return out


def derive_task_status(
    tb: Optional[TaskBoundary],
    *,
    run_status: str,
    run_rc: Optional[int],
) -> str:
    """Classify a task given its boundary record + the owning run's status.

    Status values mirror `Registry.refresh_status` where meaningful:
        queued   — known to the run, no task_start seen yet, run still running
        running  — task_start seen, no task_end, run still running
        exited   — task_end rc=0, or run exited cleanly and task had task_start
        failed   — task_end rc != 0, or run failed and this was the active task
        stopped  — run stopped/dead before task_end
        skipped  — run finished but task never started
    """
    if tb is None or not tb.started:
        if run_status == "running":
            return "queued"
        return "skipped"
    if tb.ended:
        if tb.rc is None:
            return "exited" if run_status == "exited" else "failed"
        return "exited" if tb.rc == 0 else "failed"
    # Started but no explicit end.
    if run_status == "running":
        return "running"
    if run_status in ("exited", "failed"):
        # Parallel-mode fallback: orchestrator detached before task_end,
        # inherit the run's verdict.
        if run_status == "exited" and (run_rc is None or run_rc == 0):
            return "exited"
        return "failed"
    if run_status == "stopped":
        return "stopped"
    return "dead"
