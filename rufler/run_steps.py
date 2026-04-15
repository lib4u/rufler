"""Step helpers for `run_cmd` / `build_cmd` — extracted from cli.py.

Each function does ONE thing and is called from the big command handlers.
None of them reach into the Typer app — they take explicit parameters +
a Console so unit tests can drive them directly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config import FlowConfig
from .registry import Registry, RunEntry
from .tokens import fmt_tokens


@dataclass
class ExecOverrides:
    """Effective execution flags after merging CLI args over yml defaults.

    `background` forces `non_interactive=True` and `yolo=True` because
    there's no terminal attached to answer prompts or grant permissions.
    """
    background: bool
    non_interactive: bool
    yolo: bool
    log_file: Path


def resolve_exec_overrides(
    cfg: FlowConfig,
    background: Optional[bool],
    non_interactive: Optional[bool],
    yolo: Optional[bool],
    log_file: Optional[Path],
) -> ExecOverrides:
    """CLI flag wins, else yml, else default. Must run BEFORE registry entry
    creation so the entry gets the real mode and effective log path."""
    eff_background = background if background is not None else cfg.execution.background
    eff_non_interactive = (
        non_interactive if non_interactive is not None else cfg.execution.non_interactive
    )
    eff_yolo = yolo if yolo is not None else cfg.execution.yolo
    eff_log_file = log_file if log_file is not None else Path(cfg.execution.log_file)
    if eff_background:
        eff_non_interactive = True
        eff_yolo = True
    return ExecOverrides(
        background=eff_background,
        non_interactive=eff_non_interactive,
        yolo=eff_yolo,
        log_file=eff_log_file,
    )


def decompose_task_group(cfg: FlowConfig, console: Console) -> None:
    """Decompose `task.main` into `task.decompose_count` subtasks via a
    claude-powered decomposer, then mutate `cfg.task.group` in place.

    No-op unless `cfg.task.multi` AND `cfg.task.decompose` AND the
    group is still empty. Exits the CLI on any failure — upstream already
    knows it's a fatal misconfiguration at this point.
    """
    if not (cfg.task.multi and cfg.task.decompose and not cfg.task.group):
        return

    try:
        main_body = cfg.task.resolved_main(cfg.base_dir)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if not main_body:
        console.print(
            "[red]decompose multi: task.main (or main_path) is required[/red]"
        )
        raise typer.Exit(1)

    out_dir = (cfg.base_dir / cfg.task.decompose_dir).resolve()
    yml_out = (cfg.base_dir / cfg.task.decompose_file).resolve()
    console.rule(
        f"[bold]0. decompose main → {cfg.task.decompose_count} subtasks[/bold]"
    )
    console.print(f"[dim]claude -p decomposer → {yml_out}[/dim]")

    prompt_template: Optional[str] = None
    if cfg.task.decompose_prompt:
        prompt_template = cfg.task.decompose_prompt
    elif cfg.task.decompose_prompt_path:
        pt_path = (cfg.base_dir / cfg.task.decompose_prompt_path).expanduser().resolve()
        if not pt_path.exists():
            console.print(
                f"[red]task.decompose_prompt_path not found:[/red] {pt_path}"
            )
            raise typer.Exit(1)
        prompt_template = pt_path.read_text(encoding="utf-8")
    if prompt_template:
        console.print("[dim]using custom decomposer prompt from yml[/dim]")

    try:
        from .decomposer import decompose
        written = decompose(
            main_body,
            cfg.task.decompose_count,
            out_dir,
            yml_out,
            prompt_template=prompt_template,
        )
    except Exception as e:
        console.print(f"[red]decomposer failed:[/red] {e}")
        raise typer.Exit(1)

    # `w["file_path"]` is relative to the companion yml's directory; resolve
    # to absolute so TaskItem.resolved() (which joins against cfg.base_dir)
    # picks up the file regardless of where decompose_dir sits.
    from .config import TaskItem
    cfg.task.group = [
        TaskItem(
            name=w["name"],
            file_path=str((yml_out.parent / w["file_path"]).resolve()),
        )
        for w in written
    ]
    console.print(
        f"[green]generated {len(written)} subtasks[/green] → "
        + ", ".join(w["name"] for w in written)
    )


def print_run_plan(
    cfg: FlowConfig,
    tasks: list[tuple[str, str]],
    console: Console,
) -> None:
    """Render the pre-execution plan banner: project, mode, agents, swarm,
    memory, exec flags, and every task name + first-line preview."""
    mode_label = (
        f"multi ({cfg.task.run_mode}, {len(tasks)} tasks)"
        if cfg.task.multi
        else "mono (1 task)"
    )
    console.rule("[bold]Plan[/bold]")
    console.print(f"Project : [cyan]{cfg.project.name}[/cyan]")
    console.print(f"Mode    : [cyan]{mode_label}[/cyan]")
    console.print(
        f"Agents  : [cyan]{len(cfg.agents)}[/cyan]  "
        f"({', '.join(a.name for a in cfg.agents)})"
    )
    console.print(
        f"Swarm   : topology={cfg.swarm.topology} "
        f"max_agents={cfg.swarm.max_agents} strategy={cfg.swarm.strategy} "
        f"consensus={cfg.swarm.consensus}"
    )
    console.print(
        f"Memory  : backend={cfg.memory.backend} namespace={cfg.memory.namespace} "
        f"init={cfg.memory.init}"
    )
    console.print(
        f"Exec    : background={cfg.execution.background} "
        f"non_interactive={cfg.execution.non_interactive} "
        f"yolo={cfg.execution.yolo}"
    )
    console.rule("[bold]Tasks[/bold]")
    for i, (tname, tbody) in enumerate(tasks, 1):
        preview = tbody.splitlines()[0][:90] if tbody else "(empty)"
        console.print(f"[cyan]{i}.[/cyan] [bold]{tname}[/bold] — [dim]{preview}[/dim]")
    console.rule()


def finalize_run(
    reg_entry: RunEntry,
    registry: Registry,
    base_task_id: str,
    console: Console,
) -> None:
    """Mark the registry entry finished, recompute tokens, print the
    completion banner. Swallows registry errors — we're in cleanup."""
    reg_entry.finished_at = time.time()
    try:
        registry.update(reg_entry)
    except Exception:
        pass
    try:
        registry.recompute_tokens(reg_entry)
    except Exception as e:
        console.print(f"[dim]token accounting skipped: {e}[/dim]")
    console.print(
        f"\n[green bold]rufler run complete[/green bold]  "
        f"[dim]id={reg_entry.id} task_id={base_task_id} "
        f"tokens={fmt_tokens(reg_entry.total_tokens)}[/dim]"
    )
