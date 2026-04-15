"""Task display helpers — tables, detail cards, log tail rendering.

Pure presentation layer: every function takes an explicit `console` so
tests can capture output. No side effects beyond printing.
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from ..process import fmt_age
from ..registry import RunEntry, TaskEntry, _pid_alive
from ..tokens import fmt_tokens


TASK_STATUS_COLORS = {
    "queued": "dim",
    "running": "green",
    "exited": "blue",
    "failed": "red",
    "stopped": "yellow",
    "skipped": "bright_black",
    "dead": "bright_black",
}


def fmt_ts(ts: Optional[float]) -> str:
    """Format a unix timestamp as `YYYY-MM-DD HH:MM:SS`, or '-' if None."""
    if ts is None:
        return "-"
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(started: Optional[float], finished: Optional[float],
                 running: bool = False) -> str:
    """Human-readable duration between two timestamps."""
    if started is None:
        return ""
    end = finished if finished else (time.time() if running else None)
    if end is None:
        return ""
    dur = end - started
    suffix = "…" if running and not finished else ""
    if dur < 60:
        return f"{dur:.0f}s{suffix}"
    if dur < 3600:
        return f"{dur / 60:.1f}m{suffix}"
    return f"{dur / 3600:.1f}h{suffix}"


def render_tasks_table(
    rows: list[tuple[RunEntry, TaskEntry, str, int]],
    *,
    console: Console,
    title: str = "rufler tasks",
    show_run_column: bool = False,
) -> None:
    """Render the task list table + summary footer."""
    table = Table(title=title, show_lines=False)
    table.add_column("TASK ID", style="cyan", no_wrap=True)
    table.add_column("SLOT", justify="right", style="dim")
    table.add_column("NAME", style="bold")
    table.add_column("STATUS")
    table.add_column("SOURCE", style="dim")
    if show_run_column:
        table.add_column("RUN", style="dim", no_wrap=True)
    table.add_column("STARTED", style="dim")
    table.add_column("DURATION", justify="right")
    table.add_column("TOKENS", justify="right", style="magenta")
    table.add_column("LOG", overflow="fold", style="dim")

    for entry, te, st, tok in rows:
        color = TASK_STATUS_COLORS.get(st, "white")
        status_cell = f"[{color}]{st}[/{color}]"
        started = fmt_age(te.started_at) if te.started_at else ""
        duration = fmt_duration(te.started_at, te.finished_at,
                                running=(st == "running"))
        tokens_cell = fmt_tokens(tok) if tok else "-"
        log_short = Path(te.log_path).name if te.log_path else "-"

        row = [te.id, str(te.slot), te.name, status_cell, te.source]
        if show_run_column:
            row.append(entry.id)
        row.extend([started, duration, tokens_cell, log_short])
        table.add_row(*row)

    console.print(table)

    total_tokens = sum(tok for _, _, _, tok in rows)
    n_done = sum(1 for _, _, st, _ in rows if st == "exited")
    n_running = sum(1 for _, _, st, _ in rows if st == "running")
    n_queued = sum(1 for _, _, st, _ in rows if st == "queued")
    n_failed = sum(1 for _, _, st, _ in rows if st == "failed")

    parts = [f"{len(rows)} task(s)"]
    if n_done:
        parts.append(f"[blue]{n_done} done[/blue]")
    if n_running:
        parts.append(f"[green]{n_running} running[/green]")
    if n_queued:
        parts.append(f"[dim]{n_queued} queued[/dim]")
    if n_failed:
        parts.append(f"[red]{n_failed} failed[/red]")
    parts.append(f"tokens=[magenta]{fmt_tokens(total_tokens)}[/magenta]")
    console.print("[dim]" + " · ".join(parts) + "[/dim]")


def render_task_detail(
    entry: RunEntry,
    te: TaskEntry,
    status: str,
    total_tok: int,
    console: Console,
) -> None:
    """Render a detailed card for a single task."""
    color = TASK_STATUS_COLORS.get(status, "white")
    console.rule(f"[bold]task[/bold] [cyan]{te.id}[/cyan]  [{color}]{status}[/{color}]")

    console.print(f"  name        : [bold]{te.name}[/bold]")
    console.print(f"  run         : [cyan]{entry.id}[/cyan]  {entry.project}")
    console.print(f"  slot        : {te.slot}")
    console.print(f"  source      : {te.source}")
    if te.file_path:
        console.print(f"  file_path   : {te.file_path}")
    console.print(f"  log_path    : {te.log_path}")
    if te.pid:
        alive = _pid_alive(te.pid)
        pid_style = "green" if alive else "dim"
        console.print(f"  pid         : [{pid_style}]{te.pid}[/{pid_style}]"
                       + (" (alive)" if alive else " (dead)"))

    console.print()
    console.print(f"  started_at  : {fmt_ts(te.started_at)}")
    console.print(f"  finished_at : {fmt_ts(te.finished_at)}")
    if te.started_at and te.finished_at:
        dur = te.finished_at - te.started_at
        console.print(f"  duration    : {dur:.1f}s")
    if te.rc is not None:
        rc_style = "green" if te.rc == 0 else "red"
        console.print(f"  exit code   : [{rc_style}]{te.rc}[/{rc_style}]")

    console.print()
    console.rule("[dim]tokens[/dim]")
    console.print(f"  input          : {te.input_tokens:>10,}")
    console.print(f"  output         : {te.output_tokens:>10,}")
    console.print(f"  cache_read     : {te.cache_read:>10,}")
    console.print(f"  cache_creation : {te.cache_creation:>10,}")
    console.print(f"  [magenta bold]TOTAL          : {total_tok:>10,}[/magenta bold]"
                   f"  [dim]({fmt_tokens(total_tok)})[/dim]")

    if te.log_path and Path(te.log_path).exists():
        console.print()
        console.rule("[dim]recent log events[/dim]")
        render_task_log_tail(Path(te.log_path), console=console, limit=10)


def render_task_log_tail(
    log_path: Path,
    *,
    console: Console,
    limit: int = 10,
) -> None:
    """Print the last N meaningful events from a task's log file."""
    events: list[str] = []
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
                src = rec.get("src", "")
                text = rec.get("text", "")
                rtype = rec.get("type", "")
                ts = rec.get("ts")
                ts_str = ""
                if ts:
                    ts_str = datetime.datetime.fromtimestamp(
                        float(ts)
                    ).strftime("%H:%M:%S")
                if src == "rufler" and rtype in ("task_start", "task_end"):
                    events.append(
                        f"  [dim]{ts_str}[/dim]  [cyan]{rtype}[/cyan]"
                        f"  {rec.get('task_id', '')}"
                    )
                elif src == "claude" and rtype == "assistant":
                    msg = rec.get("message", {})
                    content = msg.get("content", [])
                    summary = ""
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict):
                                if c.get("type") == "tool_use":
                                    summary = f"tool_use: {c.get('name', '?')}"
                                    break
                                elif c.get("type") == "text":
                                    t = (c.get("text") or "")[:80].replace("\n", " ")
                                    if t:
                                        summary = t
                    if summary:
                        events.append(
                            f"  [dim]{ts_str}[/dim]  [bold]assistant[/bold]"
                            f"  {summary}"
                        )
                elif src == "rufler" and text:
                    events.append(
                        f"  [dim]{ts_str}[/dim]  [dim]{text[:100]}[/dim]"
                    )
    except OSError:
        console.print("  [yellow]could not read log[/yellow]")
        return
    for ev in events[-limit:]:
        console.print(ev)


def render_tokens_by_task(
    entry: RunEntry,
    resolved: list[tuple[TaskEntry, str, int]],
    console: Console,
) -> None:
    """Render a per-task token breakdown table for `rufler tokens --by-task`."""
    table = Table(
        title=f"tokens by task  {entry.project}  {entry.id}",
        show_lines=False,
    )
    table.add_column("TASK ID", style="cyan", no_wrap=True)
    table.add_column("NAME", style="bold")
    table.add_column("INPUT", justify="right")
    table.add_column("OUTPUT", justify="right")
    table.add_column("CACHE READ", justify="right")
    table.add_column("CACHE CREAT", justify="right")
    table.add_column("TOTAL", justify="right", style="magenta bold")

    grand = 0
    for te, _st, tok in resolved:
        table.add_row(
            te.id, te.name,
            f"{te.input_tokens:,}", f"{te.output_tokens:,}",
            f"{te.cache_read:,}", f"{te.cache_creation:,}",
            f"{tok:,}",
        )
        grand += tok
    console.print(table)
    console.print(
        f"[bold]run total:[/bold] [magenta]{grand:,}[/magenta]  "
        f"[dim]({fmt_tokens(grand)})[/dim]"
    )
