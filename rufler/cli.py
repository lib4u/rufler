from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .checks import check_all
from .config import FlowConfig
from .registry import Registry, RunEntry, TaskEntry, new_entry, _pid_alive
from .runner import Runner, ensure_bypass_permissions
from .templates import SAMPLE_FLOW_YML

app = typer.Typer(
    help="rufler — one-command wrapper around ruflo for AI agent orchestration.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_FLOW_FILE = "rufler_flow.yml"
DEFAULT_LOG_REL = Path(".rufler") / "run.log"

# Hoisted from `ps` list view — used by any command that renders run status.
STATUS_COLORS = {
    "running": "green",
    "exited": "blue",
    "failed": "red",
    "stopped": "yellow",
    "dead": "bright_black",
}


def _setup_log_for(run_log: Path) -> Path:
    """Sibling raw-text log for the daemonized supervisor's stdout/stderr.

    Why a separate file: the NDJSON `run.log` is what `rufler follow` watches.
    If we dump raw ruflo init/swarm/hive-mind output into it, the file becomes
    a mix of NDJSON and raw text, the dashboard shows nothing until the claude
    stream finally starts, and `cat .rufler/run.log` looks broken. Setup goes
    to `.rufler/setup.log`, the live NDJSON stream stays clean in `run.log`.
    """
    return run_log.with_name(run_log.stem + ".setup" + run_log.suffix)


def _daemonize(log_path: Path) -> None:
    """Double-fork into a session leader, redirecting std{in,out,err}.

    docker-like `-d`: caller's parent prints id+log line and `os._exit(0)`s,
    the grandchild continues as the supervisor with stdout/stderr appended
    to `log_path`. After this returns we are the grandchild.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except OSError:
        pass


def _resolve_log_path(
    entry: Optional[RunEntry],
    cli_override: Optional[Path],
    cwd: Path,
    yml_log: Optional[Path] = None,
) -> Path:
    """Single source of truth for log path resolution across commands.

    Priority: CLI override > registry entry > yml execution.log_file > default.
    Used by `logs`, `follow`, and `ps <id>` so they all behave the same.
    """
    if cli_override is not None:
        return (cwd / cli_override).resolve()
    if entry and entry.log_path:
        return Path(entry.log_path)
    if yml_log is not None:
        return yml_log
    return (cwd / DEFAULT_LOG_REL).resolve()


def _resolve_entry_or_cwd(
    id_or_none: Optional[str],
    config: Path,
    *,
    require_existing_dir: bool = True,
) -> tuple[Optional[RunEntry], Path, Optional[Path]]:
    """Resolve a docker-style id prefix against the central registry.

    Returns (entry_or_none, base_dir, log_path_or_none):
    - If `id_or_none` is given, look it up in the registry and use its
      base_dir + log_path. Ambiguous prefix → print error + exit.
      If `require_existing_dir` and the stored base_dir no longer exists,
      print a clean error and exit (suggests `rufler ps --prune`).
    - Otherwise fall back to the current directory + the flow file there.
    """
    reg = Registry()
    if id_or_none:
        matches = reg.find_ambiguous(id_or_none)
        if not matches:
            console.print(f"[red]no rufler run matching id[/red] [bold]{id_or_none}[/bold]")
            console.print("[dim]use [bold]rufler ps -a[/bold] to see all runs[/dim]")
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(
                f"[red]id prefix[/red] [bold]{id_or_none}[/bold] "
                f"[red]is ambiguous — {len(matches)} matches:[/red]"
            )
            for e in matches:
                console.print(f"  [cyan]{e.id}[/cyan]  {e.project}  [dim]{e.base_dir}[/dim]")
            raise typer.Exit(1)
        entry = reg.refresh_status(matches[0])
        base = Path(entry.base_dir)
        if require_existing_dir and not base.exists():
            console.print(
                f"[red]base_dir for run[/red] [cyan]{entry.id}[/cyan] "
                f"[red]no longer exists:[/red] {base}\n"
                f"[dim]run [bold]rufler ps --prune[/bold] or "
                f"[bold]rufler rm {entry.id}[/bold] to clean up[/dim]"
            )
            raise typer.Exit(1)
        return entry, base, Path(entry.log_path) if entry.log_path else None
    # No id → use current dir / flow file
    cwd = config.resolve().parent if config.exists() else Path.cwd()
    log_path: Optional[Path] = None
    if config.exists():
        try:
            cfg = FlowConfig.load(config.resolve())
            log_path = (cfg.base_dir / cfg.execution.log_file).resolve()
        except Exception:
            log_path = None
    return None, cwd, log_path


def _wait_for_log_end(log_path: Path, timeout_sec: int, start_offset: int = 0) -> bool:
    """Poll log_path for the logwriter's 'log ended' marker, only scanning
    bytes ADDED after `start_offset`. This avoids false positives from stale
    'log ended' lines left over from a previous run.

    Returns True when the current run's end-marker is found, False on timeout.
    """
    import json as _json
    deadline = time.time() + timeout_sec
    last_size = start_offset
    warned = False
    while time.time() < deadline:
        if log_path.exists():
            try:
                size = log_path.stat().st_size
                if size > last_size:
                    with open(log_path, "rb") as f:
                        f.seek(last_size)
                        chunk = f.read(size - last_size).decode("utf-8", errors="replace")
                    for ln in chunk.splitlines():
                        ln = ln.strip()
                        if not ln.startswith("{"):
                            continue
                        try:
                            rec = _json.loads(ln)
                        except Exception:
                            continue
                        text = str(rec.get("text") or "")
                        if rec.get("src") == "rufler" and text.startswith("log ended"):
                            return True
                    last_size = size
            except Exception as e:
                if not warned:
                    console.print(
                        f"[yellow]warning: log poll error on {log_path}:[/yellow] "
                        f"[dim]{e}[/dim] (further errors suppressed)"
                    )
                    warned = True
        time.sleep(3)
    return False


def _print_checks() -> bool:
    results = check_all(Path.cwd())
    table = Table(title="rufler dependency check")
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Version / Hint")
    all_ok = True
    for r in results:
        status = "[green]OK[/green]" if r.ok else "[red]MISSING[/red]"
        info = r.version or r.hint or ""
        table.add_row(r.name, status, r.source or "-", info)
        all_ok = all_ok and r.ok
    console.print(table)
    return all_ok


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
    entry, cwd, _ = _resolve_entry_or_cwd(id_prefix, config, require_existing_dir=False)
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


@app.command()
def check(
    deep: bool = typer.Option(
        False, "--deep", help="Also run `ruflo doctor --fix` for full system diagnostics"
    ),
):
    """Verify node, claude code and ruflo are available."""
    ok = _print_checks()
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
):
    """Validate config, init project, start daemons, launch autonomous swarm."""
    global console
    if not skip_checks:
        if not _print_checks():
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

    # ---- Resolve task(s) ----
    # Autogenerated multi: decompose main task into subtasks first.
    if cfg.task.multi and cfg.task.autogenerated and not cfg.task.group:
        try:
            main_body = cfg.task.resolved_main(cfg.base_dir)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if not main_body:
            console.print(
                "[red]autogenerated multi: task.main (or main_path) is required[/red]"
            )
            raise typer.Exit(1)
        out_dir = (cfg.base_dir / cfg.task.autogen_dir).resolve()
        yml_out = (cfg.base_dir / cfg.task.autogen_file).resolve()
        console.rule(
            f"[bold]0. decompose main → {cfg.task.autogen_count} subtasks[/bold]"
        )
        console.print(f"[dim]claude -p decomposer → {yml_out}[/dim]")
        # Optional user-supplied decomposer prompt (inline or file)
        prompt_template: Optional[str] = None
        if cfg.task.autogen_prompt:
            prompt_template = cfg.task.autogen_prompt
        elif cfg.task.autogen_prompt_path:
            pt_path = (cfg.base_dir / cfg.task.autogen_prompt_path).expanduser().resolve()
            if not pt_path.exists():
                console.print(
                    f"[red]task.autogen_prompt_path not found:[/red] {pt_path}"
                )
                raise typer.Exit(1)
            prompt_template = pt_path.read_text(encoding="utf-8")
        if prompt_template:
            console.print("[dim]using custom decomposer prompt from yml[/dim]")
        try:
            from .decomposer import decompose
            written = decompose(
                main_body,
                cfg.task.autogen_count,
                out_dir,
                yml_out,
                prompt_template=prompt_template,
            )
        except Exception as e:
            console.print(f"[red]decomposer failed:[/red] {e}")
            raise typer.Exit(1)
        # Merge generated items into cfg.task.group.
        # `w["file_path"]` is relative to the companion yml's directory; we
        # resolve it to an absolute path so TaskItem.resolved() (which joins
        # against cfg.base_dir) picks up the file correctly regardless of
        # where autogen_dir sits.
        from .config import TaskItem as _TI
        cfg.task.group = [
            _TI(
                name=w["name"],
                file_path=str((yml_out.parent / w["file_path"]).resolve()),
            )
            for w in written
        ]
        console.print(
            f"[green]generated {len(written)} subtasks[/green] → "
            + ", ".join(w["name"] for w in written)
        )

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

    # ---- Plan ----
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

    if dry_run:
        console.print("[yellow]dry-run: stopping before executing ruflo[/yellow]")
        raise typer.Exit(0)

    runner = Runner(cwd=cfg.base_dir)
    base_task_id = f"rufler-{cfg.project.name}-{int(time.time())}"

    # Merge execution settings early: CLI flag wins, else yml, else default.
    # Done BEFORE registry creation so the registry entry gets the real mode
    # and the effective log path from the start.
    eff_background = background if background is not None else cfg.execution.background
    eff_non_interactive = (
        non_interactive if non_interactive is not None else cfg.execution.non_interactive
    )
    eff_yolo = yolo if yolo is not None else cfg.execution.yolo
    eff_log_file = log_file if log_file is not None else Path(cfg.execution.log_file)
    if eff_background:
        eff_non_interactive = True
        eff_yolo = True

    # Central registry entry — one per `rufler run` invocation.
    registry = Registry()
    primary_log_path = (cfg.base_dir / eff_log_file).resolve()
    reg_entry = new_entry(
        project=cfg.project.name,
        flow_file=cfg_path,
        base_dir=cfg.base_dir,
        mode="background" if eff_background else "foreground",
        run_mode=cfg.task.run_mode if cfg.task.multi else "sequential",
        log_path=primary_log_path,
    )
    registry.add(reg_entry)

    # docker-like `-d`: print id+log to the user's terminal, then fully detach.
    # Parent exits immediately; grandchild becomes the supervisor and runs the
    # rest of run_cmd with stdout/stderr appended to the run log.
    if eff_background:
        setup_log_path = _setup_log_for(primary_log_path)
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
        _daemonize(setup_log_path)
        # We are the grandchild now. Record our pid as the supervisor pid so
        # `rufler ps` and `rufler stop` can find us.
        from .registry import _pid_starttime as _pst
        reg_entry.pids = [os.getpid()]
        reg_entry.pid_starttimes = [_pst(os.getpid()) or 0]
        registry.update(reg_entry)
        # Rebind module-level console so any rich output goes to the log file
        # (the existing global Console captured the old stdout fd at import).
        import sys as _sys
        console = Console(file=_sys.stdout, force_terminal=False, soft_wrap=True)
    else:
        reg_entry.pids = [os.getpid()]
        from .registry import _pid_starttime as _pst
        reg_entry.pid_starttimes = [_pst(os.getpid()) or 0]
        registry.update(reg_entry)
        console.print(
            f"[bold green]rufler id:[/bold green] [cyan]{reg_entry.id}[/cyan]  "
            f"[dim](use: rufler logs {reg_entry.id} | rufler follow {reg_entry.id} | "
            f"rufler stop {reg_entry.id})[/dim]"
        )

    if not skip_init:
        console.rule("[bold]1. ruflo init + daemon[/bold]")
        runner.init_project(start_daemon=True)
        if cfg.memory.init:
            runner.memory_init(backend=cfg.memory.backend)

    console.rule("[bold]2. swarm init[/bold]")
    runner.swarm_init(cfg.swarm.topology, cfg.swarm.max_agents, cfg.swarm.strategy)

    console.rule("[bold]3. hive-mind init[/bold]")
    runner.hive_init(
        topology=cfg.swarm.topology,
        consensus=cfg.swarm.consensus,
        max_agents=cfg.swarm.max_agents,
        memory_backend=cfg.memory.backend,
    )

    if cfg.task.autonomous:
        console.rule("[bold]4. autopilot enable[/bold]")
        runner.autopilot_config(
            max_iterations=cfg.task.max_iterations,
            timeout_minutes=cfg.task.timeout_minutes,
        )
        runner.autopilot_enable()

    if eff_yolo:
        settings_path = ensure_bypass_permissions(cfg.base_dir)
        console.print(
            f"[dim]yolo: wrote permissions.defaultMode=bypassPermissions → "
            f"{settings_path}[/dim]"
        )

    # Parallel multi-task only makes sense in background — each task blocks
    # the terminal otherwise, which is effectively sequential.
    if cfg.task.multi and cfg.task.run_mode == "parallel" and not eff_background:
        console.print(
            "[yellow]warning:[/yellow] run_mode=parallel with foreground "
            "execution runs tasks one after another (terminal blocks). "
            "Add [bold]-d[/bold] / [bold]--background[/bold] for true parallelism."
        )

    console.rule("[bold]5. hive-mind spawn --claude[/bold]")
    console.print(
        f"[dim]mode: background={eff_background} non_interactive={eff_non_interactive} "
        f"yolo={eff_yolo} run_mode={cfg.task.run_mode} tasks={len(tasks)}[/dim]"
    )
    def _log_path_for(tname: str) -> Path:
        # Per-task log in multi mode so parallel runs don't collide.
        if len(tasks) > 1:
            return (
                cfg.base_dir
                / eff_log_file.parent
                / f"{eff_log_file.stem}.{tname}{eff_log_file.suffix}"
            ).resolve()
        return (cfg.base_dir / eff_log_file).resolve()

    def _spawn_one(idx: int, tname: str, tbody: str) -> None:
        objective = cfg.build_objective(task_body=tbody, task_name=tname)
        task_id = f"{base_task_id}-{tname}"
        runner.hooks_pre_task(task_id=task_id, description=tbody[:500])
        log_path = _log_path_for(tname)
        if eff_background:
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
            reg_entry.tasks.append(
                TaskEntry(name=tname, log_path=str(log_path), pid=int(pid) if pid else None)
            )
            if pid:
                reg_entry.pids.append(int(pid))
            registry.update(reg_entry)
        else:
            console.rule(f"[bold]▶ {tname}[/bold]  [dim]log={log_path}[/dim]")
            reg_entry.tasks.append(TaskEntry(name=tname, log_path=str(log_path), pid=None))
            registry.update(reg_entry)
            runner.hive_spawn_claude(
                count=len(cfg.agents),
                objective=objective,
                role="specialist",
                non_interactive=eff_non_interactive,
                skip_permissions=eff_yolo,
                log_path=log_path,
                detach=False,
            )

    try:
        if cfg.task.run_mode == "parallel" or not cfg.task.multi:
            # Parallel (or single mono task): fire everything, return.
            for i, (tname, tbody) in enumerate(tasks, 1):
                _spawn_one(i, tname, tbody)
        else:
            # Sequential multi: run one at a time. In background mode we poll
            # the per-task log for the logwriter's "log ended" marker, starting
            # the scan from the file size captured BEFORE spawn so stale markers
            # from previous runs don't trigger false positives.
            for i, (tname, tbody) in enumerate(tasks, 1):
                console.print(f"\n[bold cyan]━ [{i}/{len(tasks)}] {tname}[/bold cyan]")
                pre_spawn_size = 0
                if eff_background:
                    lp = _log_path_for(tname)
                    if lp.exists():
                        try:
                            pre_spawn_size = lp.stat().st_size
                        except OSError:
                            pre_spawn_size = 0
                _spawn_one(i, tname, tbody)
                if eff_background and i < len(tasks):
                    console.print(f"[dim]waiting for {tname} to finish...[/dim]")
                    _wait_for_log_end(
                        _log_path_for(tname),
                        cfg.task.timeout_minutes * 60,
                        start_offset=pre_spawn_size,
                    )
    except KeyboardInterrupt:
        # Docker-like: foreground run got Ctrl+C. Child claude -p procs share
        # our foreground process group so they've already received SIGINT from
        # the tty; best-effort SIGTERM any lingering ones tied to this base_dir.
        # In -d mode we never reach here during the spawn loop for already-
        # detached children, but guard anyway so we don't nuke unrelated procs.
        console.print("\n[yellow]interrupted — cleaning up[/yellow]")
        if not eff_background:
            try:
                import signal
                for p in _find_claude_procs(cfg.base_dir):
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

    # Mark finished + count tokens for both modes. In `-d` we are the
    # detached grandchild here, so this runs after every spawned task has
    # actually completed (sequential) or fired (parallel).
    reg_entry.finished_at = time.time()
    try:
        registry.update(reg_entry)
    except Exception:
        pass
    try:
        registry.recompute_tokens(reg_entry)
    except Exception as e:
        console.print(f"[dim]token accounting skipped: {e}[/dim]")
    from .tokens import fmt_tokens as _ft
    console.print(
        f"\n[green bold]rufler run complete[/green bold]  "
        f"[dim]id={reg_entry.id} task_id={base_task_id} "
        f"tokens={_ft(reg_entry.total_tokens)}[/dim]"
    )
    if eff_background:
        # Daemon supervisor is done — exit cleanly without Typer postprocessing.
        os._exit(0)


app.command("run", help="Run a rufler flow (foreground; -d to detach, like docker run).")(run_cmd)
app.command("start", hidden=True, help="Deprecated alias for `rufler run`.")(run_cmd)


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
    _, cwd, _ = _resolve_entry_or_cwd(id_prefix, config)
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
    _, cwd, _ = _resolve_entry_or_cwd(id_prefix, config)
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
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
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
    """
    entry, cwd, yml_log = _resolve_entry_or_cwd(id_prefix, config)
    if follow or raw:
        lp = _resolve_log_path(entry, None, cwd, yml_log)
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
    _, cwd, _ = _resolve_entry_or_cwd(id_prefix, config)
    r = Runner(cwd=cwd)
    console.rule("[bold]autopilot status[/bold]")
    r.autopilot_status()
    console.rule(f"[bold]autopilot log[/bold] [dim](last {last})[/dim]")
    r.autopilot_log(last=last)
    if query:
        console.rule(f"[bold]autopilot history[/bold] [dim]query={query!r}[/dim]")
        r.autopilot_history(query=query, limit=last)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _find_claude_procs(project_dir: Path) -> list[dict]:
    """Return live `claude -p` processes whose working dir is inside project_dir.

    Linux-only (relies on /proc/<pid>/cwd). On other platforms returns [].
    """
    if not Path("/proc").is_dir():
        return []
    import subprocess as sp

    try:
        out = sp.run(
            ["ps", "-eo", "pid,etime,command"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return []

    procs: list[dict] = []
    target = str(project_dir.resolve())
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, etime, cmd = parts
        if "claude -p" not in cmd:
            continue
        try:
            cwd = Path(f"/proc/{pid_s}/cwd").resolve()
        except Exception:
            continue
        if not str(cwd).startswith(target):
            continue
        procs.append({"pid": int(pid_s), "etime": etime, "cmd": cmd[:80]})
    return procs


def _fmt_age(ts: Optional[float]) -> str:
    if ts is None or ts <= 0:
        return "-"
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


@app.command()
def ps(
    id_prefix: Optional[str] = typer.Argument(
        None,
        metavar="[ID]",
        help="Optional run id to inspect. Omit to list runs (docker-like).",
    ),
    all_: bool = typer.Option(
        False, "-a", "--all", help="Show all runs including exited ones"
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Drop registry entries whose base_dir no longer exists"
    ),
    prune_older_than_days: Optional[int] = typer.Option(
        None,
        "--prune-older-than-days",
        help="Also drop finished entries older than N days",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", help="Override log path for per-project inspection"
    ),
):
    """List rufler runs (like `docker ps`) or inspect one by id.

    - `rufler ps` → running runs from all projects
    - `rufler ps -a` → all runs (running + exited)
    - `rufler ps <id>` → detailed view (processes, log tail, hive session)
    """
    registry = Registry()
    if prune or prune_older_than_days is not None:
        removed = registry.prune(
            missing_dirs=True,
            older_than_sec=(
                prune_older_than_days * 86400 if prune_older_than_days is not None else None
            ),
        )
        console.print(f"[dim]pruned {removed} stale entries[/dim]")

    # No id → list view (docker ps)
    if not id_prefix:
        entries = registry.list_refreshed(include_all=all_)
        if not entries:
            console.print(
                "[yellow]no "
                + ("runs" if all_ else "running runs")
                + "[/yellow] — use [bold]rufler run[/bold] to launch one"
            )
            return
        from .tokens import fmt_tokens
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID")
        t.add_column("PROJECT")
        t.add_column("MODE")
        t.add_column("STATUS")
        t.add_column("TASKS", justify="right")
        t.add_column("CREATED")
        t.add_column("LAST RUN")
        t.add_column("TOKENS", justify="right", style="magenta")
        t.add_column("BASE DIR")
        for e in entries:
            status_color = STATUS_COLORS.get(e.status, "white")
            # Docker-style status labels with a relative duration hint.
            if e.status == "running":
                status_label = f"Up {_fmt_age(e.started_at)}"
            elif e.status in ("exited", "failed") and e.exit_code is not None:
                rc = e.exit_code
                base = "Exited" if e.status == "exited" else "Failed"
                ago = _fmt_age(e.finished_at) if e.finished_at else "?"
                status_label = f"{base} ({rc}) {ago} ago"
            elif e.status == "stopped":
                ago = _fmt_age(e.finished_at) if e.finished_at else "?"
                status_label = f"Stopped {ago} ago"
            elif e.status == "dead":
                status_label = "Dead"
            else:
                status_label = e.status
            # LAST RUN = when the run last changed state (finished_at if set,
            # else started_at for live runs).
            last_run_ts = e.finished_at if e.finished_at else e.started_at
            t.add_row(
                e.id,
                e.project,
                e.mode,
                f"[{status_color}]{status_label}[/{status_color}]",
                str(len(e.tasks)) or "1",
                _fmt_age(e.started_at),
                _fmt_age(last_run_ts),
                fmt_tokens(e.total_tokens) if e.total_tokens else "-",
                e.base_dir,
            )
        console.print(t)
        return

    # Id given → detailed view
    entry, cwd, yml_log = _resolve_entry_or_cwd(id_prefix, config)
    log_path = _resolve_log_path(entry, log_file, cwd, yml_log)

    if entry:
        console.rule(f"[bold]run[/bold] [cyan]{entry.id}[/cyan]  [dim]{entry.project}[/dim]")
        console.print(
            f"status : [bold]{entry.status}[/bold]"
            + (f" (rc={entry.exit_code})" if entry.exit_code is not None else "")
        )
        console.print(f"mode   : {entry.mode} ({entry.run_mode})")
        console.print(f"base   : {entry.base_dir}")
        console.print(f"flow   : {entry.flow_file}")
        console.print(f"age    : {_fmt_age(entry.started_at)}")
        if entry.tasks:
            console.print(f"tasks  : {len(entry.tasks)}")
            for tt in entry.tasks:
                console.print(
                    f"  • [cyan]{tt.name}[/cyan]  [dim]pid={tt.pid} log={tt.log_path}[/dim]"
                )

    # 1) Live claude processes
    console.rule("[bold]live claude processes[/bold]")
    procs = _find_claude_procs(cwd)
    if not procs:
        console.print("[yellow]no running `claude -p` child under this project[/yellow]")
    else:
        t = Table(show_header=True, header_style="bold")
        t.add_column("PID", justify="right")
        t.add_column("Uptime")
        t.add_column("Command")
        for p in procs:
            t.add_row(str(p["pid"]), p["etime"], p["cmd"])
        console.print(t)

    # 2) Log summary
    console.rule("[bold]run log[/bold]")
    if not log_path.exists():
        console.print(f"[yellow]log not found:[/yellow] {log_path}")
    else:
        size = log_path.stat().st_size
        mtime = time.strftime("%H:%M:%S", time.localtime(log_path.stat().st_mtime))
        console.print(
            f"path : [cyan]{log_path}[/cyan]\nsize : {_human_size(size)}   "
            f"modified: {mtime}"
        )
        try:
            # read last ~4KB, show last 5 non-empty lines
            with open(log_path, "rb") as f:
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="replace")
            last_lines = [ln for ln in tail.splitlines() if ln.strip()][-5:]
            if last_lines:
                console.rule("[dim]last lines[/dim]")
                for ln in last_lines:
                    console.print(f"[dim]{ln[:200]}[/dim]")
        except Exception as e:
            console.print(f"[red]failed to tail:[/red] {e}")

    # 3) Hive-mind session files
    console.rule("[bold]hive-mind sessions[/bold]")
    sess_dir = cwd / ".hive-mind" / "sessions"
    if not sess_dir.exists():
        console.print(f"[yellow]no session dir:[/yellow] {sess_dir}")
    else:
        files = sorted(
            sess_dir.glob("hive-mind-prompt-*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:3]
        if not files:
            console.print("[yellow]no hive session files[/yellow]")
        else:
            for f in files:
                mtime = time.strftime("%H:%M:%S", time.localtime(f.stat().st_mtime))
                sid = f.stem.replace("hive-mind-prompt-", "")
                console.print(
                    f"[cyan]{sid}[/cyan]  [dim]{_human_size(f.stat().st_size)} "
                    f"@ {mtime}[/dim]"
                )


@app.command()
def follow(
    id_prefix: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Run id from `rufler ps`. Omit to use current dir."
    ),
    config: Path = typer.Option(
        Path(DEFAULT_FLOW_FILE), "--config", "-c", help="Path to rufler_flow.yml"
    ),
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", help="Override log path"
    ),
):
    """Follow the NDJSON run log live — pretty dashboard of session state,
    tasks, tokens, last tool activity, recent events. Think `tail -f` with makeup."""
    from .follow import follow as _follow

    entry, cwd, yml_log = _resolve_entry_or_cwd(id_prefix, config)
    log_path = _resolve_log_path(entry, log_file, cwd, yml_log)

    # Multi-task mode: each subtask writes NDJSON to its own per-task log,
    # so the entry's "primary" log mostly captures init/setup chatter that
    # is not NDJSON. Prefer the most recently appended per-task log so the
    # dashboard actually shows the running claude stream.
    if log_file is None and entry and entry.tasks:
        candidates = []
        for t in entry.tasks:
            if not t.log_path:
                continue
            tp = Path(t.log_path)
            try:
                mtime = tp.stat().st_mtime if tp.exists() else 0
            except OSError:
                mtime = 0
            candidates.append((mtime, tp))
        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0][1]
            if best.exists() or not log_path.exists():
                log_path = best

    console.print(f"[dim]watching {log_path} — Ctrl-C to exit[/dim]")
    _follow(log_path)


def _kill_pid_tree(pid: int, sig) -> int:
    """SIGTERM `pid`, its whole session/process group, and every descendant
    we can find via /proc walk.

    Returns the count of signals we tried to send. Linux-only descendant walk;
    on non-Linux we still signal `pid` itself.

    Why both killpg AND a /proc walk:
    - rufler's `-d` supervisor and each spawned logwriter call `os.setsid()` /
      `start_new_session=True`, so they're session/process-group leaders. Many
      of their children (`claude -p`, node, mcp helpers) detach further and
      reparent to init — they're invisible to a ppid-based descendant walk but
      `killpg(<leader>, sig)` still reaches them because they kept the pgid.
    - Conversely, processes that fork off into their own pgid won't get the
      killpg signal, so the /proc walk catches the rest.
    """
    sent = 0
    # Signal the whole process group first. If `pid` is a session/pgid leader
    # this is the only thing that reaches detached `claude -p` descendants.
    try:
        os.killpg(pid, sig)
        sent += 1
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    except OSError:
        pass

    def _children(parent: int) -> list[int]:
        kids: list[int] = []
        proc = Path("/proc")
        if not proc.is_dir():
            return kids
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                with open(d / "stat", "rb") as f:
                    raw = f.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            rp = raw.rfind(")")
            if rp < 0:
                continue
            parts = raw[rp + 2 :].split()
            if len(parts) < 2:
                continue
            try:
                ppid = int(parts[1])
            except ValueError:
                continue
            if ppid == parent:
                kids.append(int(d.name))
        return kids

    # BFS through descendants, signal leaves first then up.
    order: list[int] = []
    stack = [pid]
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        order.append(p)
        stack.extend(_children(p))
    for p in reversed(order):
        try:
            os.kill(p, sig)
            sent += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        except OSError:
            continue
    return sent


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
    entry, cwd, _ = _resolve_entry_or_cwd(
        id_prefix, config, require_existing_dir=False
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
            for p in _find_claude_procs(Path(entry_obj.base_dir)):
                pid = int(p.get("pid") or 0)
                if pid and pid not in pids:
                    pids.append(pid)
        except Exception:
            pass
        if not pids:
            return
        sent_total = 0
        for pid in pids:
            n = _kill_pid_tree(pid, _sig.SIGTERM)
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
            for p in _find_claude_procs(Path(entry_obj.base_dir)):
                pid = int(p.get("pid") or 0)
                if pid and pid not in leftover and _pid_alive(pid):
                    leftover.append(pid)
        except Exception:
            pass
        if leftover:
            killed = 0
            for pid in leftover:
                killed += _kill_pid_tree(pid, _sig.SIGKILL)
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

    from rich.table import Table
    from .tokens import fmt_tokens
    table = Table(title=f"rufler projects ({len(projs)})", show_lines=False)
    table.add_column("PROJECT", style="cyan", no_wrap=True)
    table.add_column("LAST RUN", style="bold")
    table.add_column("AGE", style="dim")
    table.add_column("RUNS", justify="right")
    table.add_column("TOKENS", justify="right", style="magenta")
    table.add_column("BASE DIR", overflow="fold")

    grand = {"in": 0, "out": 0, "cr": 0, "cc": 0}
    for p in projs:
        age = _fmt_age(p.last_started_at) if p.last_started_at else "-"
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
):
    """Show token usage — per run, per project, and grand total across all projects.

    Token totals are persisted on each registry entry and bubbled up into the
    per-project rollup, so this command is cheap and works even after the
    underlying log files have been deleted (use --rescan if you want to
    refresh from disk).
    """
    from .tokens import fmt_tokens
    reg = Registry()

    if id_prefix:
        entry, _, _ = _resolve_entry_or_cwd(id_prefix, Path(DEFAULT_FLOW_FILE), require_existing_dir=False)
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
