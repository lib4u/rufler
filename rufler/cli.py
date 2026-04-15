from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .checks import find_ruflo_skills_dir
from .orchestration import init_swarm_stack, print_checks
from .config import (
    CLI_FLAG_PACKS,
    MANUAL_COPY_PACKS,
    MANUAL_PACK_PREFIXES,
    FlowConfig,
    SkillsShEntry,
)
from .process import (
    DEFAULT_FLOW_FILE,
    DEFAULT_LOG_REL,
    daemonize,
    find_claude_procs,
    fmt_age,
    human_size,
    kill_pid_tree,
    resolve_entry_or_cwd,
    resolve_log_path,
    setup_log_for,
    wait_for_log_end,
)
from .registry import Registry, RunEntry, TaskEntry, new_entry, _pid_alive
from .task_markers import emit_task_marker, scan_task_boundaries
from .tasks import (
    resolve_tasks_for_entry, render_tasks_table, render_task_detail,
    render_tokens_by_task, find_resumable_run, completed_task_names,
)
from .tokens import fmt_tokens
from .run_steps import (
    decompose_task_group,
    finalize_run,
    print_run_plan,
    resolve_exec_overrides,
)
from .runner import Runner, ensure_bypass_permissions
from .skills import (
    delete_project_skills,
    fmt_custom_entry,
    install_skills,
    render_skills_table,
)
from .templates import SAMPLE_FLOW_YML

app = typer.Typer(
    help="rufler — one-command wrapper around ruflo for AI agent orchestration.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Hoisted from `ps` list view — used by any command that renders run status.
STATUS_COLORS = {
    "running": "green",
    "exited": "blue",
    "failed": "red",
    "stopped": "yellow",
    "dead": "bright_black",
}



@app.command("agents")
def agents_cmd(
    id_prefix: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    full: bool = typer.Option(
        False, "--full", help="Print full prompt body instead of a 150-char preview"
    ),
):
    """List agents declared in a rufler flow (name, type, role, prompt preview)."""
    entry, cwd, _ = resolve_entry_or_cwd(id_prefix, config, console, require_existing_dir=False)
    cfg_path = Path(entry.flow_file) if entry else config.resolve()
    if not cfg_path.exists():
        console.print(
            f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first "
            f"or pass a flow file via [bold]--config[/bold]."
        )
        raise typer.Exit(1)
    try:
        cfg = FlowConfig.load(cfg_path)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        raise typer.Exit(1)
    if not cfg.agents:
        console.print(f"[yellow]no agents defined in[/yellow] {cfg_path}")
        raise typer.Exit(0)

    header = f"[bold]agents[/bold] [dim]{cfg.project.name} — {cfg_path}[/dim]"
    console.rule(header)

    def _preview(text: str, limit: int = 150) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    if full:
        for a in cfg.agents:
            try:
                body = a.resolved_prompt(cfg.base_dir)
            except Exception as e:
                body = f"<prompt unavailable: {e}>"
            console.rule(f"[cyan]{a.name}[/cyan]")
            console.print(
                f"[dim]type=[/dim]{a.type}  [dim]role=[/dim]{a.role}  "
                f"[dim]seniority=[/dim]{a.seniority}"
            )
            console.print(body or "[dim](empty)[/dim]")
        return

    table = Table(show_lines=False)
    table.add_column("NAME", style="cyan", no_wrap=True)
    table.add_column("TYPE")
    table.add_column("ROLE")
    table.add_column("SENIORITY")
    table.add_column("DEPENDS ON")
    table.add_column("PROMPT (first 150)", overflow="fold")
    for a in cfg.agents:
        try:
            body = a.resolved_prompt(cfg.base_dir)
        except Exception as e:
            body = f"<prompt unavailable: {e}>"
        deps = ", ".join(a.depends_on) if a.depends_on else "-"
        table.add_row(a.name, a.type, a.role, a.seniority, deps, _preview(body))
    console.print(table)
    console.print(f"[dim]{len(cfg.agents)} agent(s) — use --full to see full prompts[/dim]")


@app.command("skills")
def skills_cmd(
    id_prefix: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    available: bool = typer.Option(
        False, "--available", help="List skills bundled inside ruflo instead of project skills"
    ),
    delete: bool = typer.Option(
        False,
        "--delete",
        help="Delete every non-symlinked skill dir under <project>/.claude/skills/. "
             "Use before `rufler build` to start from a clean slate.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt for --delete."
    ),
):
    """List skills installed under `<project>/.claude/skills/` plus the skills
    configuration declared in rufler_flow.yml. Use --available to see what's
    shipped inside ruflo's source tree (useful for picking `extra:` names).
    Use --delete to wipe installed skills (symlinks are preserved)."""
    entry, _cwd, _ = resolve_entry_or_cwd(id_prefix, config, console, require_existing_dir=False)
    cfg_path = Path(entry.flow_file) if entry else config.resolve()
    # Prefer flow file's parent over cwd so `rufler skills -c ../other/flow.yml`
    # lists the skills actually tied to that project.
    base_dir = Path(entry.base_dir) if entry else cfg_path.parent

    if delete:
        delete_project_skills(base_dir, yes, console)
        return

    if available:
        src = find_ruflo_skills_dir(base_dir)
        if src is None:
            console.print(
                "[yellow]cannot locate ruflo's bundled .claude/skills[/yellow] "
                "(likely running via npx). Install ruflo locally or globally."
            )
            raise typer.Exit(1)
        count = render_skills_table(src, title="available skills", console=console)
        console.print(
            f"[dim]{count} skill dir(s) — add to `skills.extra` in rufler_flow.yml[/dim]"
        )
        return

    # Project skills view — config snapshot first, then installed dirs.
    if cfg_path.exists():
        try:
            cfg = FlowConfig.load(cfg_path)
            s = cfg.skills
            pieces = [f"enabled={s.enabled}", f"clean={s.clean}", f"all={s.all}"]
            if s.packs:
                pieces.append(f"packs={s.packs}")
            if s.extra:
                pieces.append(f"extra={s.extra}")
            if s.custom:
                pieces.append(f"custom={[fmt_custom_entry(e) for e in s.custom]}")
            suffix = " [dim](disabled — `rufler run` will skip install)[/dim]" if not s.enabled else ""
            console.print(f"[dim]config ({cfg_path.name}):[/dim] {'  '.join(pieces)}{suffix}")
        except Exception as e:
            console.print(f"[yellow]flow config unreadable:[/yellow] {e}")
    else:
        console.print(f"[dim]no flow file at {cfg_path} — showing installed dirs only[/dim]")

    skills_dir = base_dir / ".claude" / "skills"
    if not skills_dir.is_dir():
        console.print(
            f"[yellow]no skills installed[/yellow] at {skills_dir}. "
            f"Run [bold]rufler run[/bold] or add skills manually."
        )
        raise typer.Exit(0)

    count = render_skills_table(skills_dir, title=f"skills — {base_dir.name}", console=console)
    if count == 0:
        console.print(f"[yellow]{skills_dir} is empty[/yellow]")
    else:
        console.print(
            f"[dim]{count} skill(s) in {skills_dir} — "
            f"use [bold]rufler skills --available[/bold] to see what ruflo ships[/dim]"
        )


@app.command("mcp")
def mcp_cmd(
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    active: bool = typer.Option(
        False, "--active",
        help="Show MCP servers actually registered in ~/.claude.json for this project",
    ),
):
    """List MCP servers declared in rufler_flow.yml or registered with Claude Code.

    Without flags: shows what the yml declares under `mcp.servers`.
    With --active: reads ~/.claude.json and shows what's actually registered
    for this project's directory.
    """
    if active:
        import json as _json
        claude_json = Path.home() / ".claude.json"
        if not claude_json.exists():
            console.print("[yellow]~/.claude.json not found[/yellow]")
            raise typer.Exit(0)
        try:
            data = _json.loads(claude_json.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[red]failed to read ~/.claude.json:[/red] {e}")
            raise typer.Exit(1)
        cwd_resolved = str(Path.cwd().resolve())
        projects = data.get("projects") or {}
        proj = projects.get(cwd_resolved) or {}
        servers = proj.get("mcpServers") or {}
        if not servers:
            console.print(
                f"[yellow]no MCP servers registered[/yellow] for {cwd_resolved}"
            )
            raise typer.Exit(0)
        table = Table(title=f"active MCP servers — {cwd_resolved}", show_lines=False)
        table.add_column("NAME", style="cyan", no_wrap=True)
        table.add_column("TYPE", style="dim")
        table.add_column("COMMAND / URL")
        table.add_column("ARGS", overflow="fold", style="dim")
        for name, cfg in servers.items():
            transport = cfg.get("type", "stdio")
            if transport == "stdio":
                cmd_or_url = cfg.get("command", "-")
                args = " ".join(str(a) for a in cfg.get("args", []))
            else:
                cmd_or_url = cfg.get("url", "-")
                args = ""
            table.add_row(name, transport, cmd_or_url, args)
        console.print(table)
        console.print(f"[dim]{len(servers)} server(s)[/dim]")
        return

    cfg_path = config.resolve()
    if not cfg_path.exists():
        console.print(
            f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first."
        )
        raise typer.Exit(1)
    try:
        from .config import FlowConfig
        cfg = FlowConfig.load(cfg_path)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        raise typer.Exit(1)

    servers = cfg.mcp.servers
    if not servers:
        console.print(
            "[yellow]no MCP servers declared[/yellow] in "
            f"[cyan]{cfg_path.name}[/cyan]\n"
            "[dim]add an `mcp.servers` section to your flow yml[/dim]"
        )
        raise typer.Exit(0)

    table = Table(title=f"MCP servers — {cfg_path.name}", show_lines=False)
    table.add_column("NAME", style="cyan", no_wrap=True)
    table.add_column("TRANSPORT", style="dim")
    table.add_column("COMMAND / URL")
    table.add_column("ARGS", overflow="fold", style="dim")
    table.add_column("ENV", overflow="fold", style="dim")
    for s in servers:
        if s.transport == "stdio":
            cmd_or_url = s.command
            args = " ".join(s.args)
        else:
            cmd_or_url = s.url
            args = ""
        env_str = " ".join(f"{k}={v}" for k, v in s.env.items()) if s.env else ""
        table.add_row(s.name, s.transport, cmd_or_url, args, env_str)
    console.print(table)
    console.print(
        f"[dim]{len(servers)} server(s) — "
        f"use [bold]--active[/bold] to see what's registered with claude[/dim]"
    )


@app.command()
def check(
    deep: bool = typer.Option(
        False, "--deep", help="Also run `ruflo doctor --fix` for full system diagnostics"
    ),
):
    """Verify node, claude code and ruflo are available."""
    ok = print_checks(console)
    if deep:
        console.rule("[bold]ruflo doctor --fix[/bold]")
        Runner(cwd=Path.cwd()).doctor(fix=True)
    raise typer.Exit(0 if ok else 1)


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing rufler_flow.yml"),
):
    """Create a sample rufler_flow.yml in the current directory."""
    target = Path.cwd() / DEFAULT_FLOW_FILE
    if target.exists() and not force:
        console.print(f"[yellow]{target} already exists[/yellow] (use --force to overwrite)")
        raise typer.Exit(1)
    target.write_text(SAMPLE_FLOW_YML, encoding="utf-8")
    console.print(f"[green]Created {target}[/green]")
    console.print("Edit it, then run [bold cyan]rufler run[/bold cyan]")


def run_cmd(
    flow_file: Optional[Path] = typer.Argument(
        None,
        metavar="[FLOW_FILE]",
        help="Path to flow yml (positional). Overrides --config and default rufler_flow.yml.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan, don't execute ruflo"),
    skip_checks: bool = typer.Option(False, "--skip-checks"),
    skip_init: bool = typer.Option(
        False, "--skip-init", help="Skip ruflo init + daemon + memory init"
    ),
    non_interactive: Optional[bool] = typer.Option(
        None,
        "--non-interactive/--interactive",
        help="Run Claude Code headless (-p stream). Overrides execution.non_interactive from yml.",
    ),
    yolo: Optional[bool] = typer.Option(
        None,
        "--yolo/--no-yolo",
        help="Pass --dangerously-skip-permissions. Overrides execution.yolo from yml.",
    ),
    background: Optional[bool] = typer.Option(
        None,
        "--background/--foreground",
        "-d",
        help="Detach from terminal (implies --non-interactive --yolo). Overrides execution.background from yml.",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Background log file. Overrides execution.log_file from yml.",
    ),
    new: bool = typer.Option(
        False, "--new",
        help="Start all tasks from scratch: re-decompose and ignore previous progress.",
    ),
    from_task: Optional[int] = typer.Option(
        None, "--from",
        help="Resume from task slot N (1-based), skipping tasks before it.",
    ),
):
    """Validate config, init project, start daemons, launch autonomous swarm.

    By default resumes from the last completed task of a previous run in the
    same project directory. Use --new to start fresh, or --from N to start
    from a specific task slot.
    """
    global console
    if not skip_checks:
        if not print_checks(console):
            console.print("[red]Dependency check failed.[/red] Fix above or use --skip-checks.")
            raise typer.Exit(1)

    # Positional arg wins over --config; --config wins over default.
    cfg_path = (flow_file if flow_file is not None else config).resolve()
    if not cfg_path.exists():
        console.print(
            f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first "
            f"or pass a flow file: [bold]rufler run path/to/flow.yml[/bold]."
        )
        raise typer.Exit(1)

    try:
        cfg = FlowConfig.load(cfg_path)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        raise typer.Exit(1)

    if not cfg.agents:
        console.print("[red]No agents defined in rufler_flow.yml[/red]")
        raise typer.Exit(1)

    decompose_task_group(cfg, console, force_new=new)

    try:
        tasks = cfg.task.iter_tasks(cfg.base_dir)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if not tasks:
        console.print(
            "[red]task is empty[/red] — set [bold]task.main[/bold], "
            "[bold]task.main_path[/bold], or [bold]task.group[/bold]"
        )
        raise typer.Exit(1)

    print_run_plan(cfg, tasks, console)

    if dry_run:
        console.print("[yellow]dry-run: stopping before executing ruflo[/yellow]")
        raise typer.Exit(0)

    runner = Runner(cwd=cfg.base_dir)
    base_task_id = f"rufler-{cfg.project.name}-{int(time.time())}"

    eff = resolve_exec_overrides(cfg, background, non_interactive, yolo, log_file)

    # Central registry entry — one per `rufler run` invocation.
    registry = Registry()
    primary_log_path = (cfg.base_dir / eff.log_file).resolve()
    reg_entry = new_entry(
        project=cfg.project.name,
        flow_file=cfg_path,
        base_dir=cfg.base_dir,
        mode="background" if eff.background else "foreground",
        run_mode=cfg.task.run_mode if cfg.task.multi else "sequential",
        log_path=primary_log_path,
    )
    registry.add(reg_entry)

    def _register_self_pid() -> None:
        # Record the current pid as the run's supervisor pid so `rufler ps` /
        # `rufler stop` can find us. MUST be called after daemonize() in the
        # background case — the grandchild has a new pid.
        from .registry import _pid_starttime
        reg_entry.pids = [os.getpid()]
        reg_entry.pid_starttimes = [_pid_starttime(os.getpid()) or 0]
        registry.update(reg_entry)

    # docker-like `-d`: print id+log to the user's terminal, then fully detach.
    # Parent exits immediately; grandchild becomes the supervisor and runs the
    # rest of run_cmd with stdout/stderr appended to the run log.
    if eff.background:
        setup_log_path = setup_log_for(primary_log_path)
        console.print(
            f"[bold green]rufler started in background[/bold green]  "
            f"[cyan]{reg_entry.id}[/cyan]  [dim]log={primary_log_path}[/dim]"
        )
        console.print(
            f"[dim]setup log: {setup_log_path}[/dim]"
        )
        console.print(
            f"[dim]Monitor with [bold]rufler ps[/bold] / "
            f"[bold]rufler follow {reg_entry.id}[/bold] / "
            f"[bold]rufler logs {reg_entry.id}[/bold].[/dim]"
        )
        daemonize(setup_log_path)
        _register_self_pid()
        # Rebind module-level console so any rich output goes to the log file
        # (the existing global Console captured the old stdout fd at import).
        import sys as _sys
        console = Console(file=_sys.stdout, force_terminal=False, soft_wrap=True)
    else:
        _register_self_pid()
        console.print(
            f"[bold green]rufler id:[/bold green] [cyan]{reg_entry.id}[/cyan]  "
            f"[dim](use: rufler logs {reg_entry.id} | rufler follow {reg_entry.id} | "
            f"rufler stop {reg_entry.id})[/dim]"
        )

    init_swarm_stack(runner, cfg, console, skip_init)

    if eff.yolo:
        settings_path = ensure_bypass_permissions(cfg.base_dir)
        console.print(
            f"[dim]yolo: wrote permissions.defaultMode=bypassPermissions → "
            f"{settings_path}[/dim]"
        )

    # Parallel multi-task only makes sense in background — each task blocks
    # the terminal otherwise, which is effectively sequential.
    if cfg.task.multi and cfg.task.run_mode == "parallel" and not eff.background:
        console.print(
            "[yellow]warning:[/yellow] run_mode=parallel with foreground "
            "execution runs tasks one after another (terminal blocks). "
            "Add [bold]-d[/bold] / [bold]--background[/bold] for true parallelism."
        )

    console.rule("[bold]5. hive-mind spawn --claude[/bold]")
    console.print(
        f"[dim]mode: background={eff.background} non_interactive={eff.non_interactive} "
        f"yolo={eff.yolo} run_mode={cfg.task.run_mode} tasks={len(tasks)}[/dim]"
    )
    def _log_path_for(tname: str) -> Path:
        # Per-task log in multi mode so parallel runs don't collide.
        if len(tasks) > 1:
            return (
                cfg.base_dir
                / eff.log_file.parent
                / f"{eff.log_file.stem}.{tname}{eff.log_file.suffix}"
            ).resolve()
        return (cfg.base_dir / eff.log_file).resolve()

    # Classify the source of each task so `rufler tasks` can tell inline
    # group items apart from AI-decomposed ones and plain mono `main`.
    if not cfg.task.multi:
        _source = "main"
    elif cfg.task.decompose:
        _source = "decomposed"
    elif cfg.task.group:
        _source = "group"
    else:
        _source = "inline"

    # Map item-name → file_path (if the yml declared group items with files).
    _file_paths: dict[str, str] = {}
    try:
        for item in cfg.task.group:
            if getattr(item, "file_path", None):
                _file_paths[item.name] = str(item.file_path)
    except Exception:
        pass

    # Pre-populate TaskEntry records BEFORE the spawn loop so every task is
    # visible to `rufler tasks` even while queued. Sub-id = `<run_id>.<slot2>`.
    reg_entry.tasks = []
    for i, (tname, _tbody) in enumerate(tasks, 1):
        lp = _log_path_for(tname)
        reg_entry.tasks.append(
            TaskEntry(
                name=tname,
                log_path=str(lp),
                pid=None,
                id=f"{reg_entry.id}.{i:02d}",
                slot=i,
                source=_source,
                file_path=_file_paths.get(tname),
            )
        )
    registry.update(reg_entry)

    # --- Resume logic: determine which tasks to skip ---
    skip_slots: set[int] = set()
    if from_task is not None:
        # Explicit --from N: skip slots 1..N-1.
        for te in reg_entry.tasks:
            if te.slot < from_task:
                skip_slots.add(te.slot)
        if skip_slots:
            console.print(
                f"[cyan]--from {from_task}:[/cyan] skipping "
                f"{len(skip_slots)} task(s) before slot {from_task}"
            )
    elif not new:
        prev_entry = find_resumable_run(registry, cfg.base_dir, cfg_path)
        if prev_entry and prev_entry.tasks:
            done_map = completed_task_names(prev_entry)
            if done_map:
                if cfg.task.run_mode == "parallel" or not cfg.task.multi:
                    for te in reg_entry.tasks:
                        if te.name in done_map:
                            skip_slots.add(te.slot)
                else:
                    for te in reg_entry.tasks:
                        if te.name in done_map:
                            skip_slots.add(te.slot)
                        else:
                            break
                if skip_slots:
                    # Copy timing metadata from previous run for skipped tasks.
                    for te in reg_entry.tasks:
                        if te.slot in skip_slots:
                            prev_te = done_map.get(te.name)
                            if prev_te:
                                te.started_at = prev_te.started_at
                                te.finished_at = prev_te.finished_at
                                te.rc = 0
                    registry.update(reg_entry)
                    remaining = len(reg_entry.tasks) - len(skip_slots)
                    first_active = next(
                        (te.name for te in reg_entry.tasks if te.slot not in skip_slots),
                        "?",
                    )
                    console.print(
                        f"[cyan]resuming:[/cyan] skipping {len(skip_slots)} "
                        f"completed task(s), starting from [bold]{first_active}[/bold] "
                        f"({remaining} remaining)"
                    )
                    console.print(
                        f"[dim]previous run: {prev_entry.id} — "
                        f"use [bold]--new[/bold] to start from scratch[/dim]"
                    )

    def _task_by_slot(slot: int) -> TaskEntry:
        return reg_entry.tasks[slot - 1]

    def _spawn_one(idx: int, tname: str, tbody: str) -> None:
        te = _task_by_slot(idx)
        objective = cfg.build_objective(task_body=tbody, task_name=tname)
        task_id = f"{base_task_id}-{tname}"
        runner.hooks_pre_task(task_id=task_id, description=tbody[:500])
        log_path = Path(te.log_path)
        te.started_at = time.time()
        emit_task_marker(
            log_path, "task_start",
            task_id=te.id, slot=te.slot, name=te.name,
        )
        if eff.background:
            pid = runner.hive_spawn_claude(
                count=len(cfg.agents),
                objective=objective,
                role="specialist",
                non_interactive=True,
                skip_permissions=True,
                log_path=log_path,
                detach=True,
            )
            console.print(
                f"[green]▶ task[/green] [bold]{tname}[/bold] "
                f"[dim]pid={pid} log={log_path}[/dim]"
            )
            te.pid = int(pid) if pid else None
            if pid:
                reg_entry.pids.append(int(pid))
            registry.update(reg_entry)
        else:
            console.rule(f"[bold]▶ {tname}[/bold]  [dim]log={log_path}[/dim]")
            registry.update(reg_entry)
            spawn_rc = runner.hive_spawn_claude(
                count=len(cfg.agents),
                objective=objective,
                role="specialist",
                non_interactive=eff.non_interactive,
                skip_permissions=eff.yolo,
                log_path=log_path,
                detach=False,
            )
            task_rc = int(spawn_rc) if spawn_rc else 0
            te.finished_at = time.time()
            te.rc = task_rc
            emit_task_marker(
                log_path, "task_end",
                task_id=te.id, slot=te.slot, rc=task_rc,
            )
            registry.update(reg_entry)

    try:
        if cfg.task.run_mode == "parallel" or not cfg.task.multi:
            for i, (tname, tbody) in enumerate(tasks, 1):
                if i in skip_slots:
                    console.print(
                        f"[dim]⏭ {tname}[/dim] [dim](completed in previous run)[/dim]"
                    )
                    continue
                _spawn_one(i, tname, tbody)
        else:
            for i, (tname, tbody) in enumerate(tasks, 1):
                if i in skip_slots:
                    console.print(
                        f"[dim]⏭ [{i}/{len(tasks)}] {tname} — skipped (completed)[/dim]"
                    )
                    continue
                console.print(f"\n[bold cyan]━ [{i}/{len(tasks)}] {tname}[/bold cyan]")
                pre_spawn_size = 0
                if eff.background:
                    lp = _log_path_for(tname)
                    if lp.exists():
                        try:
                            pre_spawn_size = lp.stat().st_size
                        except OSError:
                            pre_spawn_size = 0
                _spawn_one(i, tname, tbody)
                if eff.background:
                    te = reg_entry.tasks[i - 1]
                    console.print(f"[dim]waiting for {tname} to finish...[/dim]")
                    found, log_rc = wait_for_log_end(
                        Path(te.log_path),
                        cfg.task.timeout_minutes * 60,
                        console,
                        start_offset=pre_spawn_size,
                    )
                    task_rc = log_rc if log_rc is not None else (0 if found else 1)
                    te.finished_at = time.time()
                    te.rc = task_rc
                    emit_task_marker(
                        Path(te.log_path), "task_end",
                        task_id=te.id, slot=te.slot, rc=task_rc,
                    )
                    registry.update(reg_entry)
    except KeyboardInterrupt:
        # Docker-like: foreground run got Ctrl+C. Child claude -p procs share
        # our foreground process group so they've already received SIGINT from
        # the tty; best-effort SIGTERM any lingering ones tied to this base_dir.
        # In -d mode we never reach here during the spawn loop for already-
        # detached children, but guard anyway so we don't nuke unrelated procs.
        console.print("\n[yellow]interrupted — cleaning up[/yellow]")
        if not eff.background:
            try:
                import signal
                for p in find_claude_procs(cfg.base_dir):
                    try:
                        os.kill(int(p.get("pid", 0)), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, ValueError, OSError):
                        pass
            except Exception:
                pass
        reg_entry.finished_at = time.time()
        try:
            registry.update(reg_entry)
        except Exception:
            pass
        raise typer.Exit(130)

    # In `-d` we are the detached grandchild here, so this runs after every
    # spawned task has actually completed (sequential) or fired (parallel).
    finalize_run(reg_entry, registry, base_task_id, console)
    if eff.background:
        # Daemon supervisor is done — exit cleanly without Typer postprocessing.
        os._exit(0)


app.command("run", help="Run a rufler flow (foreground; -d to detach, like docker run).")(run_cmd)
app.command("start", hidden=True, help="Deprecated alias for `rufler run`.")(run_cmd)


@app.command("build")
def build_cmd(
    flow_file: Optional[Path] = typer.Argument(
        None,
        metavar="[FLOW_FILE]",
        help="Path to flow yml (positional). Overrides --config and default rufler_flow.yml.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    skip_checks: bool = typer.Option(False, "--skip-checks"),
    skip_init: bool = typer.Option(
        False, "--skip-init", help="Skip ruflo init + daemon + memory init"
    ),
):
    """Apply rufler_flow.yml to the project WITHOUT launching the swarm.

    Runs the same preparation steps as `rufler run` — ruflo init + daemon,
    memory init, skills install (packs + extra + custom), swarm init,
    hive-mind init, autopilot config — but stops before spawning Claude Code.
    Use after editing the yml (new agents, changed memory, added/removed
    skills) to re-sync ruflo state; then `rufler run` to actually start.
    """
    global console
    if not skip_checks:
        if not print_checks(console):
            console.print("[red]Dependency check failed.[/red] Fix above or use --skip-checks.")
            raise typer.Exit(1)

    cfg_path = (flow_file if flow_file is not None else config).resolve()
    if not cfg_path.exists():
        console.print(
            f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first "
            f"or pass a flow file: [bold]rufler build path/to/flow.yml[/bold]."
        )
        raise typer.Exit(1)

    try:
        cfg = FlowConfig.load(cfg_path)
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        raise typer.Exit(1)

    if not cfg.agents:
        console.print("[yellow]no agents defined — build will still prep ruflo state[/yellow]")

    console.rule("[bold]Plan (build only)[/bold]")
    console.print(f"Project : [cyan]{cfg.project.name}[/cyan]")
    console.print(
        f"Agents  : [cyan]{len(cfg.agents)}[/cyan]"
        + (f"  ({', '.join(a.name for a in cfg.agents)})" if cfg.agents else "")
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
    s = cfg.skills
    skill_pieces = [f"enabled={s.enabled}"]
    if s.all:
        skill_pieces.append("all=true")
    if s.packs:
        skill_pieces.append(f"packs={s.packs}")
    if s.extra:
        skill_pieces.append(f"extra={s.extra}")
    if s.custom:
        skill_pieces.append(f"custom={[fmt_custom_entry(e) for e in s.custom]}")
    console.print(f"Skills  : {'  '.join(skill_pieces)}")
    console.rule()

    runner = Runner(cwd=cfg.base_dir)

    init_swarm_stack(runner, cfg, console, skip_init)

    console.rule()
    console.print(
        "[bold green]build complete[/bold green] — ruflo state is synced with "
        f"[cyan]{cfg_path.name}[/cyan]. Run [bold]rufler run[/bold] to spawn the swarm."
    )


@app.command()
def status(
    id_prefix: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
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
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    interval: int = typer.Option(10, "--interval", "-i", help="Seconds between refreshes"),
):
    """Poll `rufler status` on a loop until Ctrl-C."""
    _, cwd, _ = resolve_entry_or_cwd(id_prefix, config, console)
    r = Runner(cwd=cwd)
    try:
        while True:
            console.clear()
            console.rule(f"[bold]rufler watch[/bold]  [dim]every {interval}s — Ctrl-C to stop[/dim]")
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
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    last: int = typer.Option(50, "--last", "-n", help="How many events to show"),
    raw: bool = typer.Option(
        False, "--raw", help="Print raw NDJSON run log instead of autopilot events"
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Stream new lines as they are appended (like `docker logs -f` / `tail -f`).",
    ),
):
    """Tail recent autopilot events, or the raw NDJSON run log with --raw.

    With `-f` / `--follow` the run log is streamed live (Ctrl+C to stop).

    Pass a task sub-id (e.g. a1b2c3d4.01) to show only that task's log slice.
    """
    # Task sub-id detection: if the id contains a dot, extract the task's
    # byte range from the shared log and print only that slice.
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
        # Print the tail of `last` lines first so users immediately see
        # context, then (if -f) keep streaming new bytes as they appear.
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
        # docker-style follow: poll for appended bytes, handle truncation
        # and rotation, keep going until Ctrl+C.
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
                    # File was truncated/rotated — restart from the beginning.
                    offset = 0
                if size > offset:
                    try:
                        with open(lp, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(offset)
                            chunk = f.read(size - offset)
                    except OSError:
                        time.sleep(0.5)
                        continue
                    if chunk:
                        # Strip trailing newline so console.print doesn't
                        # add a second one.
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
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
):
    """Live TUI dashboard with task progress, AI conversation, and system log.

    Tails all per-task NDJSON logs and renders a dashboard with four panels:
    tasks list, session stats, AI conversation stream, and system events.
    Replays existing log content for context, then streams live. Ctrl+C to stop.
    """
    entry, cwd, yml_log = resolve_entry_or_cwd(id_prefix, config, console)

    # Without an explicit id, resolve_entry_or_cwd returns entry=None.
    # Auto-pick the most recent run for this cwd (running preferred, then
    # latest finished) so `rufler follow` works without arguments.
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
    if entry and entry.tasks:
        task_logs = [
            (te.name, Path(te.log_path))
            for te in entry.tasks
            if te.log_path
        ]
        from .follow import TaskSeed
        task_defs = [
            TaskSeed(
                task_id=te.id,
                name=te.name,
                started_at=te.started_at,
                finished_at=te.finished_at,
                rc=te.rc,
            )
            for te in entry.tasks
        ]

    n_logs = len(task_logs) if task_logs else 1
    run_label = f"  [cyan]{entry.id}[/cyan]" if entry else ""
    console.print(
        f"[dim]following{run_label}  {n_logs} log(s) — Ctrl+C to stop[/dim]"
    )
    from .follow import follow as _follow_tui
    _follow_tui(lp, task_logs=task_logs, task_defs=task_defs)


@app.command()
def progress(
    id_prefix: Optional[str] = typer.Argument(None, metavar="[ID]"),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    last: int = typer.Option(20, "--last", "-n", help="How many recent log events to show"),
    query: Optional[str] = typer.Option(
        None,
        "--query",
        "-q",
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
        console.rule(f"[bold]autopilot history[/bold] [dim]query={query!r}[/dim]")
        r.autopilot_history(query=query, limit=last)


@app.command()
def stop(
    id_prefix: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    success: bool = typer.Option(
        True, "--success/--failure", help="Record outcome for neural training"
    ),
    kill: bool = typer.Option(
        True,
        "--kill/--no-kill",
        help="SIGTERM the run's supervisor pids and their descendants (default on). "
             "Use --no-kill to only run ruflo teardown without touching processes.",
    ),
    grace: float = typer.Option(
        8.0, "--grace",
        help="Seconds between SIGTERM and the guaranteed SIGKILL. Long enough "
             "for claude to flush its session state to disk, short enough that "
             "stop never hangs.",
    ),
    teardown_timeout: float = typer.Option(
        10.0, "--teardown-timeout",
        help="Per-step timeout for best-effort ruflo teardown (post-task, "
             "session-end, daemon stop). Runs after the kill so it can never "
             "block the actual stop.",
    ),
):
    """Shutdown autopilot, hive-mind, daemon; SIGTERM the supervisor pid tree;
    record post-task outcome; end session.

    By default this DOES kill the supervisor and every descendant claude proc.
    Pass --no-kill to skip the kill step (e.g. when you only want the ruflo
    teardown side-effects but the run is already dead).
    """
    import signal as _sig
    # If base_dir is gone we still want to kill pids + mark the entry finished,
    # so we tolerate a missing directory here.
    entry, cwd, _ = resolve_entry_or_cwd(
        id_prefix, config, console, require_existing_dir=False
    )

    # Without an explicit id, _resolve_entry_or_cwd may return entry=None
    # even when there IS a live run for this cwd. Auto-pick it so `rufler stop`
    # actually kills + marks finished instead of silently no-opping.
    if entry is None and not id_prefix:
        try:
            cwd_resolved = cwd.resolve()
            candidates = [
                e for e in Registry().list_refreshed(include_all=False)
                if Path(e.base_dir).resolve() == cwd_resolved
            ]
        except Exception:
            candidates = []
        if len(candidates) == 1:
            entry = candidates[0]
        elif len(candidates) > 1:
            console.print(f"[red]multiple running runs in[/red] {cwd_resolved}:")
            for e in candidates:
                console.print(f"  [cyan]{e.id}[/cyan]  {e.project}")
            console.print("[dim]pass an id explicitly: rufler stop <id>[/dim]")
            raise typer.Exit(1)

    def _terminate(entry_obj):
        """SIGTERM the registered supervisor pids and any live `claude -p`
        children under the project's base_dir. After `grace` seconds, SIGKILL
        anything still alive."""
        if not kill or entry_obj is None:
            return
        pids: list[int] = list(entry_obj.pids or [])
        # Also collect lingering claude children we know about by cwd.
        try:
            for p in find_claude_procs(Path(entry_obj.base_dir)):
                pid = int(p.get("pid") or 0)
                if pid and pid not in pids:
                    pids.append(pid)
        except Exception:
            pass
        if not pids:
            return
        sent_total = 0
        for pid in pids:
            n = kill_pid_tree(pid, _sig.SIGTERM)
            if n:
                sent_total += n
        if sent_total:
            console.print(
                f"[yellow]sent SIGTERM to {sent_total} process(es) under "
                f"id={entry_obj.id}[/yellow] [dim](grace={grace}s)[/dim]"
            )
        # Wait for graceful shutdown, then SIGKILL stragglers.
        deadline = time.time() + max(0.0, grace)
        while time.time() < deadline:
            alive = any(_pid_alive(pid) for pid in pids)
            if not alive:
                break
            time.sleep(0.2)
        leftover = [pid for pid in pids if _pid_alive(pid)]
        # Also re-scan for any `claude -p` still under base_dir — they may have
        # reparented and survived the first sweep.
        try:
            for p in find_claude_procs(Path(entry_obj.base_dir)):
                pid = int(p.get("pid") or 0)
                if pid and pid not in leftover and _pid_alive(pid):
                    leftover.append(pid)
        except Exception:
            pass
        if leftover:
            killed = 0
            for pid in leftover:
                killed += kill_pid_tree(pid, _sig.SIGKILL)
            if killed:
                console.print(
                    f"[red]SIGKILL fallback:[/red] {killed} process(es) refused SIGTERM"
                )

    # Order: SIGTERM first (gives claude a chance to flush its session state
    # via its own signal handler), short grace, then guaranteed SIGKILL, then
    # best-effort ruflo teardown. The teardown runs AFTER the kill so a hung
    # ruflo subprocess can never block `rufler stop` from actually stopping.
    if kill:
        console.print(
            f"[dim]sending SIGTERM (claude has {grace:.0f}s to flush state to "
            f"memory before SIGKILL)[/dim]"
        )
    _terminate(entry)

    if entry and not Path(entry.base_dir).exists():
        console.print(
            f"[yellow]base_dir gone:[/yellow] {entry.base_dir} "
            f"[dim](skipping ruflo teardown, only marking entry finished)[/dim]"
        )
        entry.finished_at = time.time()
        Registry().update(entry)
        console.print(f"[green]stopped run[/green] [cyan]{entry.id}[/cyan]")
        return

    # Best-effort ruflo teardown — wrapped in a hard per-step timeout via a
    # background thread so a hung `npx ruflo …` can't make `rufler stop` hang.
    # We do NOT wait for the thread on timeout; ruflo daemon processes are
    # independently managed and surviving subprocesses are harmless here.
    import threading

    def _bg(label: str, fn) -> None:
        done = threading.Event()
        err: list[Exception] = []

        def _runner_thread():
            try:
                fn()
            except Exception as e:
                err.append(e)
            finally:
                done.set()

        th = threading.Thread(target=_runner_thread, daemon=True)
        th.start()
        if not done.wait(timeout=teardown_timeout):
            console.print(
                f"[yellow]{label}: timed out after {teardown_timeout:.0f}s "
                f"(skipping)[/yellow]"
            )
            return
        if err:
            console.print(f"[yellow]{label} failed:[/yellow] [dim]{err[0]}[/dim]")

    r = Runner(cwd=cwd)
    project_name = entry.project if entry else cwd.name
    task_id = f"rufler-{project_name}-stop"
    console.print("[dim]best-effort ruflo teardown[/dim]")
    _bg("hooks post-task", lambda: r.hooks_post_task(task_id=task_id, success=success))
    _bg("autopilot disable", r.autopilot_disable)
    _bg("hive-mind shutdown", r.hive_shutdown)
    _bg("hooks session-end", r.hooks_session_end)
    _bg("daemon stop", r.daemon_stop)

    # Mark entry as finished in the registry + recount tokens.
    if entry:
        entry.finished_at = time.time()
        reg = Registry()
        reg.update(entry)
        try:
            reg.recompute_tokens(entry)
        except Exception:
            pass
        console.print(f"[green]stopped run[/green] [cyan]{entry.id}[/cyan]")


@app.command("rm")
def rm(
    ids: list[str] = typer.Argument(
        None, metavar="[ID]...", help="Run ids (prefixes) to remove from the registry"
    ),
    all_finished: bool = typer.Option(
        False, "--all-finished", help="Remove every entry that is not currently running"
    ),
    older_than_days: Optional[int] = typer.Option(
        None,
        "--older-than-days",
        help="Remove finished entries older than N days",
    ),
):
    """Remove rufler run entries from the central registry.

    This only deletes the registry bookkeeping — it does NOT touch log files,
    project directories or ruflo state. Use `rufler stop <id>` first if the
    run is still alive.
    """
    reg = Registry()

    # Mutually exclusive: explicit ids, --all-finished, and --older-than-days
    # are three distinct selectors. Combining them used to silently drop the
    # explicit id list — now it's an error.
    selectors = sum(
        1
        for x in (bool(ids), all_finished, older_than_days is not None)
        if x
    )
    if selectors > 1:
        console.print(
            "[red]conflicting flags:[/red] pass exactly one of "
            "[bold]<ids>[/bold], [bold]--all-finished[/bold], or "
            "[bold]--older-than-days[/bold]"
        )
        raise typer.Exit(2)

    if older_than_days is not None:
        removed = reg.prune(missing_dirs=False, older_than_sec=older_than_days * 86400)
        console.print(f"[dim]removed {removed} entries older than {older_than_days}d[/dim]")
        return

    if all_finished:
        entries = reg.list_refreshed(include_all=True)
        targets = [e for e in entries if e.status != "running"]
        if not targets:
            console.print("[yellow]nothing to remove (no finished runs)[/yellow]")
            return
        removed = reg.remove_many([e.id for e in targets])
        console.print(f"[green]removed {removed} finished runs[/green]")
        return

    if not ids:
        console.print("[red]pass one or more run ids, or use --all-finished / --older-than-days[/red]")
        raise typer.Exit(1)

    removed_count = 0
    for raw in ids:
        matches = reg.find_ambiguous(raw)
        if not matches:
            console.print(f"[yellow]no match:[/yellow] {raw}")
            continue
        if len(matches) > 1:
            console.print(
                f"[red]ambiguous prefix[/red] [bold]{raw}[/bold] "
                f"[red]— {len(matches)} matches[/red]"
            )
            continue
        entry = reg.refresh_status(matches[0])
        if entry.status == "running":
            console.print(
                f"[yellow]{entry.id} is still running[/yellow] "
                f"[dim](use `rufler stop {entry.id}` first)[/dim]"
            )
            continue
        if reg.remove(entry.id):
            console.print(f"[green]removed[/green] [cyan]{entry.id}[/cyan]  {entry.project}")
            removed_count += 1
    if removed_count:
        console.print(f"[dim]{removed_count} entries removed[/dim]")


@app.command("ps")
def ps_cmd(
    id_prefix: Optional[str] = typer.Argument(
        None,
        metavar="[ID]",
        help="Run id prefix for a detailed single-run view. Omit for the list.",
    ),
    all_runs: bool = typer.Option(
        False, "--all", "-a", help="Show all runs, not just currently running"
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
    """Docker-style list of rufler runs.

    \b
    No args → currently running only (like `docker ps`).
    -a     → everything ever run (like `docker ps -a`).
    <ID>   → detailed view of one run: status, tasks, pids, log tail.
    --prune / --prune-older-than-days → clean up stale entries.
    """
    reg = Registry()

    if prune:
        removed = reg.prune(missing_dirs=True)
        console.print(f"[dim]pruned {removed} entries with missing base_dir[/dim]")
        return
    if prune_older_than_days is not None:
        removed = reg.prune(missing_dirs=False, older_than_sec=prune_older_than_days * 86400)
        console.print(f"[dim]pruned {removed} entries older than {prune_older_than_days}d[/dim]")
        return

    if id_prefix:
        entry, _, _ = resolve_entry_or_cwd(
            id_prefix, Path(DEFAULT_FLOW_FILE), console,
            require_existing_dir=False,
        )
        if entry is None:
            console.print(f"[red]no run matching[/red] [bold]{id_prefix}[/bold]")
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

    table = Table(
        title=f"rufler ps {'(all)' if all_runs else '(running)'}",
        show_lines=False,
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("PROJECT", style="bold")
    table.add_column("MODE", style="dim")
    table.add_column("STATUS")
    table.add_column("TASKS", justify="right")
    table.add_column("CREATED", style="dim")
    table.add_column("TOKENS", justify="right", style="magenta")
    table.add_column("BASE DIR", overflow="fold", style="dim")

    for e in entries:
        color = STATUS_COLORS.get(e.status, "white")
        if e.status == "failed" and e.exit_code is not None:
            status_cell = f"[{color}]{e.status} ({e.exit_code})[/{color}]"
        else:
            status_cell = f"[{color}]{e.status}[/{color}]"

        created = fmt_age(e.started_at)
        tok = fmt_tokens(e.total_tokens) if e.total_tokens else "-"
        task_count = str(len(e.tasks)) if e.tasks else "-"
        base = e.base_dir
        if not Path(base).exists():
            base = f"[dim strike]{base}[/dim strike]"

        table.add_row(
            e.id, e.project, e.mode, status_cell,
            task_count, created, tok, base,
        )

    console.print(table)
    console.print(
        f"[dim]{len(entries)} run(s)"
        + ("" if all_runs else " — use [bold]-a[/bold] for all")
        + "[/dim]"
    )


def _ps_detail(entry: RunEntry, console: Console) -> None:
    """Detailed single-run view for `rufler ps <id>`."""
    color = STATUS_COLORS.get(entry.status, "white")
    console.rule(
        f"[bold]run[/bold] [cyan]{entry.id}[/cyan]  "
        f"[{color}]{entry.status}[/{color}]"
    )
    console.print(f"  project     : [bold]{entry.project}[/bold]")
    console.print(f"  flow_file   : {entry.flow_file}")
    console.print(f"  base_dir    : {entry.base_dir}")
    console.print(f"  mode        : {entry.mode}  run_mode={entry.run_mode}")
    console.print(f"  log_path    : {entry.log_path}")
    console.print(f"  started_at  : {fmt_age(entry.started_at)}")
    if entry.finished_at:
        console.print(f"  finished_at : {fmt_age(entry.finished_at)}")
    if entry.exit_code is not None:
        rc_style = "green" if entry.exit_code == 0 else "red"
        console.print(f"  exit_code   : [{rc_style}]{entry.exit_code}[/{rc_style}]")

    if entry.pids:
        alive_pids = [p for p in entry.pids if _pid_alive(p)]
        dead_pids = [p for p in entry.pids if not _pid_alive(p)]
        parts = []
        if alive_pids:
            parts.append(f"[green]{','.join(str(p) for p in alive_pids)}[/green]")
        if dead_pids:
            parts.append(f"[dim]{','.join(str(p) for p in dead_pids)}[/dim]")
        console.print(f"  pids        : {' '.join(parts)}")

    console.print()
    console.print(f"  tokens      : [magenta]{entry.total_tokens:,}[/magenta]  "
                   f"[dim](in={entry.input_tokens:,} out={entry.output_tokens:,} "
                   f"cache_r={entry.cache_read:,} cache_c={entry.cache_creation:,})[/dim]")

    if entry.tasks:
        console.print()
        console.rule("[dim]tasks[/dim]")
        resolved = resolve_tasks_for_entry(entry)
        from .tasks import TASK_STATUS_COLORS
        for te, st, tok in resolved:
            tc = TASK_STATUS_COLORS.get(st, "white")
            tok_str = f"  [{fmt_tokens(tok)}]" if tok else ""
            console.print(
                f"  [cyan]{te.id}[/cyan]  {te.name:<20s}  "
                f"[{tc}]{st:<8s}[/{tc}]{tok_str}"
            )

    try:
        procs = find_claude_procs(Path(entry.base_dir))
        if procs:
            console.print()
            console.rule("[dim]claude processes[/dim]")
            for p in procs:
                console.print(
                    f"  pid={p.get('pid','?')}  "
                    f"elapsed={p.get('elapsed','?')}  "
                    f"[dim]{(p.get('cmd') or '')[:80]}[/dim]"
                )
    except Exception:
        pass


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

    table = Table(title=f"rufler projects ({len(projs)})", show_lines=False)
    table.add_column("PROJECT", style="cyan", no_wrap=True)
    table.add_column("LAST RUN", style="bold")
    table.add_column("AGE", style="dim")
    table.add_column("RUNS", justify="right")
    table.add_column("TOKENS", justify="right", style="magenta")
    table.add_column("BASE DIR", overflow="fold")

    grand = {"in": 0, "out": 0, "cr": 0, "cc": 0}
    for p in projs:
        age = fmt_age(p.last_started_at) if p.last_started_at else "-"
        last_id = p.last_run_id or "-"
        base = p.last_base_dir or "-"
        if base != "-" and not Path(base).exists():
            base = f"[dim strike]{base}[/dim strike]"
        proj_total = (
            p.total_input_tokens
            + p.total_output_tokens
            + p.total_cache_read
            + p.total_cache_creation
        )
        grand["in"] += p.total_input_tokens
        grand["out"] += p.total_output_tokens
        grand["cr"] += p.total_cache_read
        grand["cc"] += p.total_cache_creation
        table.add_row(
            p.name, last_id, age, str(p.total_runs), fmt_tokens(proj_total), base
        )
    console.print(table)
    grand_total = grand["in"] + grand["out"] + grand["cr"] + grand["cc"]
    console.print(
        f"[dim]grand total:[/dim] [magenta]{fmt_tokens(grand_total)}[/magenta] "
        f"[dim](in={fmt_tokens(grand['in'])}, out={fmt_tokens(grand['out'])}, "
        f"cache_read={fmt_tokens(grand['cr'])}, "
        f"cache_creation={fmt_tokens(grand['cc'])})[/dim]"
    )


@app.command("tasks")
def tasks_cmd(
    id_or_task: Optional[str] = typer.Argument(
        None,
        metavar="[ID]",
        help="Run id or task sub-id (e.g. a1b2c3d4 or a1b2c3d4.01). "
             "Omit to show tasks of the latest run in cwd.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    all_runs: bool = typer.Option(
        False, "--all", "-a", help="Show tasks across ALL runs, not just the latest"
    ),
    status_filter: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="Filter by status: queued, running, exited, failed, stopped, skipped"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show detailed card for a single task"
    ),
):
    """List tasks for a rufler run with status, tokens, and timing.

    \b
    By default shows the latest run's tasks for the current directory.
    Pass a run id prefix to inspect a specific run, or a full task sub-id
    (e.g. a1b2c3d4.01) plus -v for a detailed card.
    """
    reg = Registry()

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
            run_id_prefix, config, console, require_existing_dir=False
        )
        if entry is None:
            console.print(f"[red]no run matching[/red] [bold]{run_id_prefix}[/bold]")
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

    # Detail view for a single task.
    if target_task_id and verbose:
        for entry in entries:
            resolved = resolve_tasks_for_entry(entry)
            for te, st, tok in resolved:
                if te.id == target_task_id or te.id.startswith(target_task_id):
                    render_task_detail(entry, te, st, tok, console)
                    return
        console.print(f"[red]task not found:[/red] [bold]{target_task_id}[/bold]")
        raise typer.Exit(1)

    # Table view.
    all_rows: list[tuple[RunEntry, TaskEntry, str, int]] = []
    for entry in entries:
        resolved = resolve_tasks_for_entry(entry)
        for te, st, tok in resolved:
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
        title += f"  [dim]{e.project}[/dim]  [cyan]{e.id}[/cyan]"
        title += f"  [dim]({e.status})[/dim]"

    render_tasks_table(
        all_rows,
        console=console,
        title=title,
        show_run_column=all_runs,
    )


@app.command("tokens")
def tokens_cmd(
    id_prefix: Optional[str] = typer.Argument(
        None,
        metavar="[ID]",
        help="Run id (prefix) to inspect. Omit for the per-project + grand totals.",
    ),
    rescan: bool = typer.Option(
        False, "--rescan", help="Re-parse run logs before reporting (slower but accurate)."
    ),
    by_task: bool = typer.Option(
        False, "--by-task", help="Show per-task token breakdown instead of run/project totals."
    ),
):
    """Show token usage — per run, per project, and grand total across all projects.

    Token totals are persisted on each registry entry and bubbled up into the
    per-project rollup, so this command is cheap and works even after the
    underlying log files have been deleted (use --rescan if you want to
    refresh from disk).

    Use --by-task to show per-task token breakdown for a specific run.
    """
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
            console.print("[red]no run found[/red] — pass a run id or run from a project dir")
            raise typer.Exit(1)
        resolved = resolve_tasks_for_entry(entry)
        if not resolved:
            console.print(f"[yellow]no tasks in run[/yellow] [cyan]{entry.id}[/cyan]")
            raise typer.Exit(0)
        render_tokens_by_task(entry, resolved, console)
        return

    if id_prefix:
        entry, _, _ = resolve_entry_or_cwd(id_prefix, Path(DEFAULT_FLOW_FILE), console, require_existing_dir=False)
        if entry is None:
            console.print(f"[red]no run matching[/red] [bold]{id_prefix}[/bold]")
            raise typer.Exit(1)
        if rescan:
            try:
                reg.recompute_tokens(entry)
            except Exception as e:
                console.print(f"[yellow]rescan failed:[/yellow] {e}")
        console.rule(f"[bold]tokens[/bold] [cyan]{entry.id}[/cyan] [dim]{entry.project}[/dim]")
        console.print(f"input          : [bold]{entry.input_tokens:>12,}[/bold]")
        console.print(f"output         : [bold]{entry.output_tokens:>12,}[/bold]")
        console.print(f"cache_read     : [bold]{entry.cache_read:>12,}[/bold]")
        console.print(f"cache_creation : [bold]{entry.cache_creation:>12,}[/bold]")
        console.rule()
        console.print(
            f"[magenta bold]TOTAL          : {entry.total_tokens:>12,}[/magenta bold]  "
            f"[dim]({fmt_tokens(entry.total_tokens)})[/dim]"
        )
        return

    # No id → per-project + grand total
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

    table = Table(title="token usage by project", show_lines=False)
    table.add_column("PROJECT", style="cyan", no_wrap=True)
    table.add_column("INPUT", justify="right")
    table.add_column("OUTPUT", justify="right")
    table.add_column("CACHE READ", justify="right")
    table.add_column("CACHE CREATION", justify="right")
    table.add_column("TOTAL", justify="right", style="magenta bold")

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
        table.add_row(
            p.name,
            f"{p.total_input_tokens:,}",
            f"{p.total_output_tokens:,}",
            f"{p.total_cache_read:,}",
            f"{p.total_cache_creation:,}",
            f"{total:,}",
        )
    console.print(table)
    grand_total = grand_in + grand_out + grand_cr + grand_cc
    console.print(
        f"[bold]grand total:[/bold] [magenta]{grand_total:,}[/magenta] "
        f"[dim]({fmt_tokens(grand_total)}) — in={fmt_tokens(grand_in)} "
        f"out={fmt_tokens(grand_out)} cache_read={fmt_tokens(grand_cr)} "
        f"cache_creation={fmt_tokens(grand_cc)}[/dim]"
    )


if __name__ == "__main__":
    app()
