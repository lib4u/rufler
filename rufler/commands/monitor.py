"""``rufler status``, ``rufler watch``, ``rufler logs``, ``rufler follow``,
``rufler progress`` commands."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from ..process import (
    DEFAULT_FLOW_FILE,
    resolve_entry_or_cwd,
    resolve_log_path,
)
from ..registry import Registry
from ..runner import Runner
from ..task_markers import scan_task_boundaries


def register(app: typer.Typer, console: Console) -> None:

    @app.command()
    def status(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id from `rufler ps`. Omit to use current dir.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
    ):
        """Show full ruflo status: system + swarm + hive-mind + autopilot."""
        _, cwd, _ = resolve_entry_or_cwd(id_prefix, config, console)
        r = Runner(cwd=cwd)
        console.rule("[bold]system[/bold]")
        r.system_status()
        console.rule("[bold]swarm[/bold]")
        r.swarm_status()
        console.rule("[bold]hive-mind[/bold]")
        r.hive_status()
        console.rule("[bold]autopilot[/bold]")
        r.autopilot_status()

    @app.command()
    def watch(
        id_prefix: Optional[str] = typer.Argument(None, metavar="[ID]"),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        interval: int = typer.Option(
            10, "--interval", "-i", help="Seconds between refreshes",
        ),
    ):
        """Poll `rufler status` on a loop until Ctrl-C."""
        _, cwd, _ = resolve_entry_or_cwd(id_prefix, config, console)
        r = Runner(cwd=cwd)
        try:
            while True:
                console.clear()
                console.rule(
                    f"[bold]rufler watch[/bold]  "
                    f"[dim]every {interval}s — Ctrl-C to stop[/dim]"
                )
                console.rule("[bold]system[/bold]")
                r.system_status()
                console.rule("[bold]hive-mind[/bold]")
                r.hive_status()
                console.rule("[bold]autopilot[/bold]")
                r.autopilot_status()
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[dim]watch stopped[/dim]")

    @app.command()
    def logs(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id or task sub-id (a1b2c3d4.01). "
                 "When a task sub-id is given, only that task's log slice is shown.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        last: int = typer.Option(
            50, "--last", "-n", help="How many events to show",
        ),
        raw: bool = typer.Option(
            False, "--raw",
            help="Print raw NDJSON run log instead of autopilot events",
        ),
        follow: bool = typer.Option(
            False, "--follow", "-f",
            help="Stream new lines as they are appended "
                 "(like `docker logs -f` / `tail -f`).",
        ),
    ):
        """Tail recent autopilot events, or the raw NDJSON run log with --raw.

        With `-f` / `--follow` the run log is streamed live (Ctrl+C to stop).

        Pass a task sub-id (e.g. a1b2c3d4.01) to show only that task's log slice.
        """
        if id_prefix and "." in id_prefix:
            run_prefix = id_prefix.split(".")[0]
            entry, cwd, yml_log = resolve_entry_or_cwd(
                run_prefix, config, console, require_existing_dir=False,
            )
            if entry is None:
                console.print(f"[red]no run matching[/red] [bold]{run_prefix}[/bold]")
                raise typer.Exit(1)
            te_match = None
            for te in entry.tasks:
                if te.id == id_prefix or te.id.startswith(id_prefix):
                    te_match = te
                    break
            if te_match is None:
                console.print(f"[red]task not found:[/red] [bold]{id_prefix}[/bold]")
                raise typer.Exit(1)
            lp = Path(te_match.log_path) if te_match.log_path else None
            if not lp or not lp.exists():
                console.print(f"[yellow]log not found:[/yellow] {lp}")
                raise typer.Exit(1)
            boundaries = scan_task_boundaries(lp)
            tb = boundaries.get(te_match.id)
            start_off = tb.start_offset if tb and tb.start_offset is not None else 0
            end_off = tb.end_offset if tb and tb.end_offset is not None else None
            console.rule(
                f"[bold]task log[/bold] [cyan]{te_match.id}[/cyan] "
                f"[dim]{te_match.name}[/dim]  [dim]{lp}[/dim]"
            )
            try:
                with open(lp, "rb") as f:
                    if start_off:
                        f.seek(start_off)
                        f.readline()
                    while True:
                        pos = f.tell()
                        if end_off is not None and pos >= end_off:
                            break
                        raw_line = f.readline()
                        if not raw_line:
                            break
                        console.print(
                            raw_line.decode("utf-8", errors="replace").rstrip(),
                            highlight=False, markup=False,
                        )
            except OSError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            return

        entry, cwd, yml_log = resolve_entry_or_cwd(id_prefix, config, console)
        if follow or raw:
            lp = resolve_log_path(entry, None, cwd, yml_log)
            if not lp.exists() and not follow:
                console.print(f"[yellow]log not found:[/yellow] {lp}")
                raise typer.Exit(1)
            console.rule(f"[bold]run log[/bold] [dim]{lp}[/dim]")
            try:
                if lp.exists():
                    with open(lp, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    for ln in lines[-last:]:
                        console.print(ln.rstrip())
            except OSError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            if not follow:
                return
            try:
                offset = lp.stat().st_size if lp.exists() else 0
                while True:
                    if not lp.exists():
                        time.sleep(0.5)
                        continue
                    try:
                        size = lp.stat().st_size
                    except OSError:
                        time.sleep(0.5)
                        continue
                    if size < offset:
                        offset = 0
                    if size > offset:
                        try:
                            with open(lp, "r", encoding="utf-8",
                                      errors="replace") as f:
                                f.seek(offset)
                                chunk = f.read(size - offset)
                        except OSError:
                            time.sleep(0.5)
                            continue
                        if chunk:
                            if chunk.endswith("\n"):
                                chunk = chunk[:-1]
                            console.print(chunk, highlight=False, markup=False)
                        offset = size
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                console.print("\n[dim]follow stopped[/dim]")
                return
        r = Runner(cwd=cwd)
        console.rule(f"[bold]autopilot log[/bold] [dim](last {last})[/dim]")
        r.autopilot_log(last=last)

    @app.command("follow")
    def follow_cmd(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id from `rufler ps`. Omit to use current dir.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        classic: bool = typer.Option(
            False, "--classic",
            help="Use the Rich Live-based follow instead of Textual TUI.",
        ),
    ):
        """Live TUI dashboard with task progress, AI conversation, and system log.

        Tails all per-task NDJSON logs and renders a dashboard with four panels:
        tasks list, session stats, AI conversation stream, and system events.
        Replays existing log content for context, then streams live. Press q to quit.
        """
        entry, cwd, yml_log = resolve_entry_or_cwd(id_prefix, config, console)

        if entry is None and not id_prefix:
            cwd_resolved = cwd.resolve()
            all_entries = Registry().list_refreshed(include_all=True)
            candidates = [
                e for e in all_entries
                if Path(e.base_dir).resolve() == cwd_resolved
            ]
            if candidates:
                running = [e for e in candidates if e.status == "running"]
                entry = running[0] if running else candidates[0]

        lp = resolve_log_path(entry, None, cwd, yml_log)

        task_logs: list[tuple[str, Path]] | None = None
        task_defs = None
        _report_sources = ("task_report", "final_report")
        if entry and entry.tasks:
            task_logs = [
                (te.name, Path(te.log_path))
                for te in entry.tasks
                if te.log_path and te.source not in _report_sources
            ]
            from ..follow import TaskSeed
            task_defs = [
                TaskSeed(
                    task_id=te.id,
                    name=te.name,
                    started_at=te.started_at,
                    finished_at=te.finished_at,
                    rc=te.rc,
                )
                for te in entry.tasks
                if te.source not in _report_sources
            ]

        if classic:
            n_logs = len(task_logs) if task_logs else 1
            run_label = f"  [cyan]{entry.id}[/cyan]" if entry else ""
            console.print(
                f"[dim]following{run_label}  {n_logs} log(s) — Ctrl+C to stop[/dim]"
            )
            from ..follow import follow as _follow_rich
            _follow_rich(lp, task_logs=task_logs, task_defs=task_defs)
            return

        try:
            from ..tui.follow import run_follow
        except ImportError:
            console.print(
                "[yellow]textual not installed, falling back to classic mode[/yellow]"
            )
            from ..follow import follow as _follow_rich
            _follow_rich(lp, task_logs=task_logs, task_defs=task_defs)
            return
        run_follow(
            lp, task_logs=task_logs, task_defs=task_defs,
            run_id=entry.id if entry else None,
        )

    @app.command()
    def progress(
        id_prefix: Optional[str] = typer.Argument(None, metavar="[ID]"),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        last: int = typer.Option(
            20, "--last", "-n", help="How many recent log events to show",
        ),
        query: Optional[str] = typer.Option(
            None, "--query", "-q",
            help="Optional semantic query for autopilot history (past episodes)",
        ),
    ):
        """Show autopilot task progress + recent iteration log."""
        _, cwd, _ = resolve_entry_or_cwd(id_prefix, config, console)
        r = Runner(cwd=cwd)
        console.rule("[bold]autopilot status[/bold]")
        r.autopilot_status()
        console.rule(f"[bold]autopilot log[/bold] [dim](last {last})[/dim]")
        r.autopilot_log(last=last)
        if query:
            console.rule(
                f"[bold]autopilot history[/bold] [dim]query={query!r}[/dim]"
            )
            r.autopilot_history(query=query, limit=last)
