"""``rufler tasks`` and ``rufler tokens`` commands."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..process import DEFAULT_FLOW_FILE, resolve_entry_or_cwd
from ..registry import Registry, RunEntry, TaskEntry
from ..tasks import (
    render_task_detail,
    render_tasks_table,
    render_tokens_by_task,
    resolve_tasks_for_entry,
)
from ..tokens import fmt_tokens
from ..process import fmt_age


def register(app: typer.Typer, console: Console) -> None:

    def _tasks_delete(
        reg: Registry,
        id_or_task: Optional[str],
        *,
        rm_all: bool,
        rm_files: bool,
        config: Path,
    ) -> None:
        """Implement --rm / --rm-all for `rufler tasks`."""

        def _delete_files(entry: RunEntry, task_entries: list[TaskEntry]) -> int:
            removed = 0
            for te in task_entries:
                if te.file_path:
                    fp = Path(te.file_path)
                    if fp.exists():
                        fp.unlink()
                        removed += 1
                if te.log_path:
                    lp = Path(te.log_path)
                    if lp.exists():
                        lp.unlink()
                        removed += 1
            return removed

        if rm_all:
            cwd_resolved = Path.cwd().resolve()
            all_entries = reg.list_refreshed(include_all=True)
            candidates = [
                e for e in all_entries
                if Path(e.base_dir).resolve() == cwd_resolved
            ]
            if not candidates:
                console.print(
                    "[yellow]no runs found for this directory[/yellow]"
                )
                raise typer.Exit(0)
            total_removed = 0
            files_removed = 0
            for entry in candidates:
                if rm_files:
                    files_removed += _delete_files(entry, entry.tasks)
                n = reg.remove_tasks(entry.id)
                total_removed += n
            console.print(
                f"[green]removed {total_removed} task(s)[/green] "
                f"from {len(candidates)} run(s)"
            )
            if files_removed:
                console.print(
                    f"[dim]deleted {files_removed} file(s) from disk[/dim]"
                )
            return

        if not id_or_task:
            console.print("[red]pass a task sub-id or run id with --rm[/red]")
            raise typer.Exit(1)

        if "." in id_or_task:
            run_prefix = id_or_task.split(".")[0]
            matches = reg.find_ambiguous(run_prefix)
            if not matches:
                console.print(
                    f"[red]no run matching[/red] [bold]{run_prefix}[/bold]"
                )
                raise typer.Exit(1)
            if len(matches) > 1:
                console.print(
                    f"[red]ambiguous prefix[/red] [bold]{run_prefix}[/bold] — "
                    f"{len(matches)} matches"
                )
                raise typer.Exit(1)
            entry = matches[0]
            targets = [te for te in entry.tasks if te.id == id_or_task]
            if not targets:
                console.print(
                    f"[red]task not found:[/red] [bold]{id_or_task}[/bold]"
                )
                raise typer.Exit(1)
            if rm_files:
                n_files = _delete_files(entry, targets)
                if n_files:
                    console.print(
                        f"[dim]deleted {n_files} file(s) from disk[/dim]"
                    )
            removed = reg.remove_tasks(entry.id, [id_or_task])
            console.print(
                f"[green]removed[/green] [cyan]{id_or_task}[/cyan] "
                f"({targets[0].name}) from run {entry.id}"
            )
        else:
            matches = reg.find_ambiguous(id_or_task)
            if not matches:
                console.print(
                    f"[red]no run matching[/red] [bold]{id_or_task}[/bold]"
                )
                raise typer.Exit(1)
            if len(matches) > 1:
                console.print(
                    f"[red]ambiguous prefix[/red] [bold]{id_or_task}[/bold] — "
                    f"{len(matches)} matches"
                )
                raise typer.Exit(1)
            entry = matches[0]
            if rm_files:
                n_files = _delete_files(entry, entry.tasks)
                if n_files:
                    console.print(
                        f"[dim]deleted {n_files} file(s) from disk[/dim]"
                    )
            removed = reg.remove_tasks(entry.id)
            console.print(
                f"[green]removed {removed} task(s)[/green] from run "
                f"[cyan]{entry.id}[/cyan] ({entry.project})"
            )

    @app.command("tasks")
    def tasks_cmd(
        id_or_task: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id or task sub-id (e.g. a1b2c3d4 or a1b2c3d4.01). "
                 "Omit to show tasks of the latest run in cwd.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        all_runs: bool = typer.Option(
            False, "--all", "-a",
            help="Show tasks across ALL runs, not just the latest",
        ),
        status_filter: Optional[str] = typer.Option(
            None, "--status", "-s",
            help="Filter by status: queued, running, exited, failed, stopped, skipped",
        ),
        verbose: bool = typer.Option(
            False, "--verbose", "-v",
            help="Show detailed card for a single task",
        ),
        rm: bool = typer.Option(
            False, "--rm",
            help="Delete the task(s) identified by [ID] from the registry.",
        ),
        rm_all: bool = typer.Option(
            False, "--rm-all",
            help="Remove ALL tasks across all runs for the current project directory.",
        ),
        rm_files: bool = typer.Option(
            False, "--rm-files",
            help="When used with --rm or --rm-all, also delete on-disk task files and logs.",
        ),
    ):
        r"""List tasks for a rufler run with status, tokens, and timing.

        \b
        By default shows the latest run's tasks for the current directory.
        Pass a run id prefix to inspect a specific run, or a full task sub-id
        (e.g. a1b2c3d4.01) plus -v for a detailed card.
        """
        reg = Registry()

        if rm or rm_all:
            _tasks_delete(
                reg, id_or_task, rm_all=rm_all, rm_files=rm_files,
                config=config,
            )
            return

        target_task_id: Optional[str] = None
        run_id_prefix: Optional[str] = None
        if id_or_task and "." in id_or_task:
            target_task_id = id_or_task
            run_id_prefix = id_or_task.split(".")[0]
            verbose = True
        elif id_or_task:
            run_id_prefix = id_or_task

        if all_runs:
            entries = reg.list_refreshed(include_all=True)
            if not entries:
                console.print("[yellow]no runs recorded[/yellow]")
                raise typer.Exit(0)
        elif run_id_prefix:
            entry, _, _ = resolve_entry_or_cwd(
                run_id_prefix, config, console, require_existing_dir=False,
            )
            if entry is None:
                console.print(
                    f"[red]no run matching[/red] [bold]{run_id_prefix}[/bold]"
                )
                raise typer.Exit(1)
            entries = [entry]
        else:
            cwd_resolved = Path.cwd().resolve()
            all_entries = reg.list_refreshed(include_all=True)
            candidates = [
                e for e in all_entries
                if Path(e.base_dir).resolve() == cwd_resolved
            ]
            if not candidates:
                console.print(
                    "[yellow]no rufler runs found for this directory[/yellow]\n"
                    "[dim]run [bold]rufler run[/bold] first, or pass a run id: "
                    "[bold]rufler tasks <id>[/bold][/dim]"
                )
                raise typer.Exit(0)
            entries = [candidates[0]]

        if target_task_id and verbose:
            for entry in entries:
                resolved = resolve_tasks_for_entry(entry)
                for te, st, tok in resolved:
                    if te.id == target_task_id or te.id.startswith(
                        target_task_id
                    ):
                        render_task_detail(entry, te, st, tok, console)
                        return
            console.print(
                f"[red]task not found:[/red] [bold]{target_task_id}[/bold]"
            )
            raise typer.Exit(1)

        all_rows: list[tuple[RunEntry, TaskEntry, str, int]] = []
        for entry in entries:
            resolved = resolve_tasks_for_entry(entry)
            for te, st, tok in resolved:
                if te.source in ("task_report", "final_report"):
                    continue
                if status_filter and st != status_filter:
                    continue
                all_rows.append((entry, te, st, tok))

        if not all_rows:
            if status_filter:
                console.print(
                    f"[yellow]no tasks with status '{status_filter}'[/yellow]"
                )
            else:
                console.print("[yellow]no tasks found[/yellow]")
            raise typer.Exit(0)

        title = "rufler tasks"
        if len(entries) == 1:
            e = entries[0]
            title += f"  {e.project}  {e.id}  ({e.status})"

        render_tasks_table(
            all_rows, console=console, title=title,
            show_run_column=all_runs,
        )

    @app.command("tokens")
    def tokens_cmd(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id (prefix) to inspect. "
                 "Omit for the per-project + grand totals.",
        ),
        rescan: bool = typer.Option(
            False, "--rescan",
            help="Re-parse run logs before reporting (slower but accurate).",
        ),
        by_task: bool = typer.Option(
            False, "--by-task",
            help="Show per-task token breakdown instead of run/project totals.",
        ),
    ):
        """Show token usage — per run, per project, and grand total."""
        reg = Registry()

        if by_task:
            entry: Optional[RunEntry] = None
            if id_prefix:
                entry, _, _ = resolve_entry_or_cwd(
                    id_prefix, Path(DEFAULT_FLOW_FILE), console,
                    require_existing_dir=False,
                )
            else:
                cwd_resolved = Path.cwd().resolve()
                all_entries = reg.list_refreshed(include_all=True)
                candidates = [
                    e for e in all_entries
                    if Path(e.base_dir).resolve() == cwd_resolved
                ]
                if candidates:
                    entry = candidates[0]
            if entry is None:
                console.print(
                    "[red]no run found[/red] — pass a run id or run from "
                    "a project dir"
                )
                raise typer.Exit(1)
            resolved = resolve_tasks_for_entry(entry)
            if not resolved:
                console.print(
                    f"[yellow]no tasks in run[/yellow] [cyan]{entry.id}[/cyan]"
                )
                raise typer.Exit(0)

            render_tokens_by_task(entry, resolved, console)
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
            if rescan:
                try:
                    reg.recompute_tokens(entry)
                except Exception as e:
                    console.print(f"[yellow]rescan failed:[/yellow] {e}")

            console.rule(
                f"[bold]tokens[/bold] [cyan]{entry.id}[/cyan] "
                f"[dim]{entry.project}[/dim]"
            )
            console.print(
                f"input          : [bold]{entry.input_tokens:>12,}[/bold]"
            )
            console.print(
                f"output         : [bold]{entry.output_tokens:>12,}[/bold]"
            )
            console.print(
                f"cache_read     : [bold]{entry.cache_read:>12,}[/bold]"
            )
            console.print(
                f"cache_creation : [bold]{entry.cache_creation:>12,}[/bold]"
            )
            console.rule()
            console.print(
                f"[magenta bold]TOTAL          : "
                f"{entry.total_tokens:>12,}[/magenta bold]  "
                f"[dim]({fmt_tokens(entry.total_tokens)})[/dim]"
            )
            return

        if rescan:
            console.print("[dim]rescanning every run's logs...[/dim]")
            for e in reg.list_all():
                try:
                    reg.recompute_tokens(e)
                except Exception:
                    pass

        projs = reg.list_projects()
        if not projs:
            console.print("[yellow]no projects recorded[/yellow]")
            return

        col_defs = [
            ("PROJECT", {"no_wrap": True}),
            ("INPUT", {"justify": "right"}),
            ("OUTPUT", {"justify": "right"}),
            ("CACHE READ", {"justify": "right"}),
            ("CACHE CREATION", {"justify": "right"}),
            ("TOTAL", {"justify": "right"}),
        ]

        rows: list[list[str]] = []
        grand_in = grand_out = grand_cr = grand_cc = 0
        for p in projs:
            total = (
                p.total_input_tokens + p.total_output_tokens
                + p.total_cache_read + p.total_cache_creation
            )
            grand_in += p.total_input_tokens
            grand_out += p.total_output_tokens
            grand_cr += p.total_cache_read
            grand_cc += p.total_cache_creation
            rows.append([
                p.name,
                f"{p.total_input_tokens:,}",
                f"{p.total_output_tokens:,}",
                f"{p.total_cache_read:,}",
                f"{p.total_cache_creation:,}",
                f"{total:,}",
            ])

        grand_total = grand_in + grand_out + grand_cr + grand_cc
        footer = (
            f"grand total: {grand_total:,} ({fmt_tokens(grand_total)}) — "
            f"in={fmt_tokens(grand_in)} out={fmt_tokens(grand_out)} "
            f"cache_read={fmt_tokens(grand_cr)} "
            f"cache_creation={fmt_tokens(grand_cc)}"
        )

        table = Table(title="token usage by project", show_lines=False)
        table.add_column("PROJECT", style="cyan", no_wrap=True)
        table.add_column("INPUT", justify="right")
        table.add_column("OUTPUT", justify="right")
        table.add_column("CACHE READ", justify="right")
        table.add_column("CACHE CREATION", justify="right")
        table.add_column("TOTAL", justify="right", style="magenta bold")
        for row in rows:
            table.add_row(*row)
        console.print(table)
        console.print(
            f"[bold]grand total:[/bold] [magenta]{grand_total:,}[/magenta] "
            f"[dim]({fmt_tokens(grand_total)}) — "
            f"in={fmt_tokens(grand_in)} out={fmt_tokens(grand_out)} "
            f"cache_read={fmt_tokens(grand_cr)} "
            f"cache_creation={fmt_tokens(grand_cc)}[/dim]"
        )
