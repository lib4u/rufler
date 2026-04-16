"""``rufler ps`` and ``rufler projects`` commands."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..process import (
    DEFAULT_FLOW_FILE,
    find_claude_procs,
    fmt_age,
    resolve_entry_or_cwd,
)
from ..registry import Registry, RunEntry, _pid_alive
from ..tasks import resolve_tasks_for_entry
from ..tokens import fmt_tokens

# Shared status colour map — used by ps, tasks, and any command that shows
# run status.
STATUS_COLORS = {
    "running": "green",
    "done": "blue",
    "failed": "red",
    "stopped": "yellow",
    "dead": "bright_black",
}


def register(app: typer.Typer, console: Console) -> None:

    @app.command("ps")
    def ps_cmd(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id prefix for a detailed single-run view. Omit for the list.",
        ),
        all_runs: bool = typer.Option(
            False, "--all", "-a",
            help="Show all runs, not just currently running",
        ),
        prune: bool = typer.Option(
            False, "--prune",
            help="Drop entries whose base_dir no longer exists on disk",
        ),
        prune_older_than_days: Optional[int] = typer.Option(
            None, "--prune-older-than-days",
            help="Drop finished entries older than N days",
        ),
    ):
        r"""Docker-style list of rufler runs.

        \b
        No args → currently running only (like `docker ps`).
        -a     → everything ever run (like `docker ps -a`).
        <ID>   → detailed view of one run: status, tasks, pids, log tail.
        --prune / --prune-older-than-days → clean up stale entries.
        """
        reg = Registry()

        if prune:
            removed = reg.prune(missing_dirs=True)
            console.print(
                f"[dim]pruned {removed} entries with missing base_dir[/dim]"
            )
            return
        if prune_older_than_days is not None:
            removed = reg.prune(
                missing_dirs=False,
                older_than_sec=prune_older_than_days * 86400,
            )
            console.print(
                f"[dim]pruned {removed} entries older than "
                f"{prune_older_than_days}d[/dim]"
            )
            return

        if id_prefix:
            entry, _, _ = resolve_entry_or_cwd(
                id_prefix, Path(DEFAULT_FLOW_FILE), console,
                require_existing_dir=False,
            )
            if entry is None:
                console.print(
                    f"[red]no run matching[/red] [bold]{id_prefix}[/bold]"
                )
                raise typer.Exit(1)
            _ps_detail(entry, console)
            return

        entries = reg.list_refreshed(include_all=all_runs)
        if not entries:
            if all_runs:
                console.print("[yellow]no runs recorded[/yellow]")
            else:
                console.print(
                    "[yellow]no running rufler runs[/yellow]  "
                    "[dim](use -a to see all)[/dim]"
                )
            return

        # Build data
        col_defs = [
            ("ID", {"no_wrap": True}),
            ("PROJECT", {}),
            ("MODE", {}),
            ("STATUS", {}),
            ("TASKS", {"justify": "right"}),
            ("CREATED", {}),
            ("TOKENS", {"justify": "right"}),
            ("BASE DIR", {}),
        ]
        rows: list[list[str]] = []
        for e in entries:
            status_cell = e.status
            if e.status == "failed" and e.exit_code is not None:
                status_cell = f"{status_cell} ({e.exit_code})"
            created = fmt_age(e.started_at)
            tok = fmt_tokens(e.total_tokens) if e.total_tokens else "-"
            real_tasks = [
                t for t in e.tasks
                if t.source not in ("task_report", "final_report")
            ]
            task_count = str(len(real_tasks)) if real_tasks else "-"
            base = e.base_dir
            rows.append([e.id, e.project, e.mode, status_cell,
                         task_count, created, tok, base])

        title = f"rufler ps {'(all)' if all_runs else '(running)'}"
        footer = (
            f"{len(entries)} run(s)"
            + ("" if all_runs else " — pass -a for all")
        )

        _ps_rich(col_defs, rows, entries, all_runs, title, console)

    def _ps_rich(col_defs, rows, entries, all_runs, title, con):
        table = Table(title=title, show_lines=False)
        rich_styles = {
            "ID": "cyan", "PROJECT": "bold", "MODE": "dim",
            "TOKENS": "magenta", "CREATED": "dim", "BASE DIR": "dim",
        }
        for name, opts in col_defs:
            table.add_column(
                name, style=rich_styles.get(name, ""),
                no_wrap=opts.get("no_wrap", False),
                justify=opts.get("justify", "left"),
                overflow="fold" if name == "BASE DIR" else None,
            )
        for i, row in enumerate(rows):
            e = entries[i]
            styled = list(row)
            color = STATUS_COLORS.get(e.status, "white")
            styled[3] = f"[{color}]{row[3]}[/{color}]"
            if not Path(row[7]).exists():
                styled[7] = f"[dim strike]{row[7]}[/dim strike]"
            table.add_row(*styled)
        con.print(table)
        con.print(
            f"[dim]{len(entries)} run(s)"
            + ("" if all_runs else " — use [bold]-a[/bold] for all")
            + "[/dim]"
        )

    def _ps_detail(entry: RunEntry, con: Console) -> None:
        """Detailed single-run view for `rufler ps <id>`."""
        lines: list[str] = []
        color = STATUS_COLORS.get(entry.status, "white")
        lines.append(f"run {entry.id}  [{entry.status}]")
        lines.append("")
        lines.append(f"  project     : {entry.project}")
        lines.append(f"  flow_file   : {entry.flow_file}")
        lines.append(f"  base_dir    : {entry.base_dir}")
        lines.append(f"  mode        : {entry.mode}  run_mode={entry.run_mode}")
        lines.append(f"  log_path    : {entry.log_path}")
        lines.append(f"  started_at  : {fmt_age(entry.started_at)}")
        if entry.finished_at:
            lines.append(f"  finished_at : {fmt_age(entry.finished_at)}")
        if entry.exit_code is not None:
            lines.append(f"  exit_code   : {entry.exit_code}")

        if entry.pids:
            alive_pids = [p for p in entry.pids if _pid_alive(p)]
            dead_pids = [p for p in entry.pids if not _pid_alive(p)]
            parts = []
            if alive_pids:
                parts.append(",".join(str(p) for p in alive_pids) + " (alive)")
            if dead_pids:
                parts.append(",".join(str(p) for p in dead_pids) + " (dead)")
            lines.append(f"  pids        : {' '.join(parts)}")

        lines.append("")
        lines.append(
            f"  tokens      : {entry.total_tokens:,}  "
            f"(in={entry.input_tokens:,} out={entry.output_tokens:,} "
            f"cache_r={entry.cache_read:,} "
            f"cache_c={entry.cache_creation:,})"
        )

        if entry.tasks:
            lines.append("")
            lines.append("  --- tasks ---")
            resolved = resolve_tasks_for_entry(entry)
            for te, st, tok in resolved:
                tok_str = f"  [{fmt_tokens(tok)}]" if tok else ""
                lines.append(
                    f"  {te.id}  {te.name:<20s}  {st:<8s}{tok_str}"
                )

        try:
            procs = find_claude_procs(Path(entry.base_dir))
            if procs:
                lines.append("")
                lines.append("  --- claude processes ---")
                for p in procs:
                    lines.append(
                        f"  pid={p.get('pid', '?')}  "
                        f"elapsed={p.get('elapsed', '?')}  "
                        f"{(p.get('cmd') or '')[:80]}"
                    )
        except Exception:
            pass

        body = "\n".join(lines)
        con.rule(
            f"[bold]run[/bold] [cyan]{entry.id}[/cyan]  "
            f"[{color}]{entry.status}[/{color}]"
        )
        con.print(body)

    @app.command("projects")
    def projects_cmd():
        """List all projects ever run by rufler with last-run timestamps.

        This is the "project history" rollup — it survives `rufler rm` and
        `rufler ps --prune` because it's stored separately from individual runs.
        """
        reg = Registry()
        projs = reg.list_projects()
        if not projs:
            console.print("[yellow]no projects recorded[/yellow]")
            return

        col_defs = [
            ("PROJECT", {"no_wrap": True}),
            ("LAST RUN", {}),
            ("AGE", {}),
            ("RUNS", {"justify": "right"}),
            ("TOKENS", {"justify": "right"}),
            ("BASE DIR", {}),
        ]

        grand = {"in": 0, "out": 0, "cr": 0, "cc": 0}
        rows: list[list[str]] = []
        for p in projs:
            age = fmt_age(p.last_started_at) if p.last_started_at else "-"
            last_id = p.last_run_id or "-"
            base = p.last_base_dir or "-"
            proj_total = (
                p.total_input_tokens + p.total_output_tokens
                + p.total_cache_read + p.total_cache_creation
            )
            grand["in"] += p.total_input_tokens
            grand["out"] += p.total_output_tokens
            grand["cr"] += p.total_cache_read
            grand["cc"] += p.total_cache_creation
            rows.append([
                p.name, last_id, age, str(p.total_runs),
                fmt_tokens(proj_total), base,
            ])

        grand_total = grand["in"] + grand["out"] + grand["cr"] + grand["cc"]
        footer = (
            f"grand total: {fmt_tokens(grand_total)} "
            f"(in={fmt_tokens(grand['in'])}, out={fmt_tokens(grand['out'])}, "
            f"cache_read={fmt_tokens(grand['cr'])}, "
            f"cache_creation={fmt_tokens(grand['cc'])})"
        )

        table = Table(
            title=f"rufler projects ({len(projs)})", show_lines=False,
        )
        table.add_column("PROJECT", style="cyan", no_wrap=True)
        table.add_column("LAST RUN", style="bold")
        table.add_column("AGE", style="dim")
        table.add_column("RUNS", justify="right")
        table.add_column("TOKENS", justify="right", style="magenta")
        table.add_column("BASE DIR", overflow="fold")
        for row in rows:
            base_val = row[5]
            if base_val != "-" and not Path(base_val).exists():
                base_val = f"[dim strike]{base_val}[/dim strike]"
            table.add_row(row[0], row[1], row[2], row[3], row[4], base_val)
        console.print(table)
        console.print(f"[dim]{footer}[/dim]")
