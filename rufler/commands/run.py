"""``rufler run`` (+ ``rufler start`` alias) and ``rufler build`` commands."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from ..config import FlowConfig
from ..orchestration import init_swarm_stack, print_checks
from ..process import (
    DEFAULT_FLOW_FILE,
    daemonize,
    find_claude_procs,
    resolve_log_path,
    setup_log_for,
    wait_for_log_end,
)
from ..registry import Registry, TaskEntry, new_entry
from ..run_steps import (
    decompose_task_group,
    finalize_run,
    print_run_plan,
    resolve_exec_overrides,
    run_deep_think,
)
from ..runner import (
    Runner,
    ensure_bypass_permissions,
    ensure_rufler_ignored,
    restore_permissions,
    restore_rufler_ignored,
)
from ..skills import fmt_custom_entry
from ..task_markers import emit_task_marker
from ..tasks import (
    collect_chain_entry,
    completed_task_names,
    find_resumable_run,
    resolve_chain_flag,
    run_report,
)


def register(app: typer.Typer, console: Console) -> None:

    def run_cmd(
        flow_file: Optional[Path] = typer.Argument(
            None,
            metavar="[FLOW_FILE]",
            help="Path to flow yml (positional). Overrides --config and default rufler_flow.yml.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print plan, don't execute ruflo",
        ),
        skip_checks: bool = typer.Option(False, "--skip-checks"),
        skip_init: bool = typer.Option(
            False, "--skip-init",
            help="Skip ruflo init + daemon + memory init",
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
            help="Detach from terminal (implies --non-interactive --yolo). "
                 "Overrides execution.background from yml.",
        ),
        log_file: Optional[Path] = typer.Option(
            None, "--log-file",
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
        # We need to rebind console after daemonize — use a mutable holder.
        con = console

        if not skip_checks:
            if not print_checks(con):
                con.print("[red]Dependency check failed.[/red] Fix above or use --skip-checks.")
                raise typer.Exit(1)

        cfg_path = (flow_file if flow_file is not None else config).resolve()
        if not cfg_path.exists():
            con.print(
                f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first "
                f"or pass a flow file: [bold]rufler run path/to/flow.yml[/bold]."
            )
            raise typer.Exit(1)

        try:
            cfg = FlowConfig.load(cfg_path)
        except Exception as e:
            con.print(f"[red]Failed to load config:[/red] {e}")
            raise typer.Exit(1)

        if not cfg.agents:
            con.print("[red]No agents defined in rufler_flow.yml[/red]")
            raise typer.Exit(1)

        runner = Runner(cwd=cfg.base_dir)
        base_task_id = f"rufler-{cfg.project.name}-{int(time.time())}"

        # Write .claude/settings.local.json BEFORE anything else spawns —
        # ruflo's own init / daemon / hooks pipeline can invoke claude, and
        # so do deep_think / decomposer / hive-mind spawn. Placing this at
        # the very top of the run guarantees `.rufler/**` deny rules are in
        # place for every subprocess that opens in this cwd.
        _rufler_ignore_path, _rufler_previous_deny = ensure_rufler_ignored(
            cfg.base_dir,
        )

        eff = resolve_exec_overrides(cfg, background, non_interactive, yolo, log_file)

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
            from ..registry import _pid_starttime
            reg_entry.pids = [os.getpid()]
            reg_entry.pid_starttimes = [_pid_starttime(os.getpid()) or 0]
            registry.update(reg_entry)

        if eff.background:
            setup_log_path = setup_log_for(primary_log_path)
            con.print(
                f"[bold green]rufler started in background[/bold green]  "
                f"[cyan]{reg_entry.id}[/cyan]  [dim]log={primary_log_path}[/dim]"
            )
            con.print(f"[dim]setup log: {setup_log_path}[/dim]")
            con.print(
                f"[dim]Monitor with [bold]rufler ps[/bold] / "
                f"[bold]rufler follow {reg_entry.id}[/bold] / "
                f"[bold]rufler logs {reg_entry.id}[/bold].[/dim]"
            )
            daemonize(setup_log_path)
            _register_self_pid()
            import sys as _sys
            con = Console(file=_sys.stdout, force_terminal=False, soft_wrap=True)
        else:
            _register_self_pid()
            con.print(
                f"[bold green]rufler id:[/bold green] [cyan]{reg_entry.id}[/cyan]  "
                f"[dim](use: rufler logs {reg_entry.id} | rufler follow {reg_entry.id} | "
                f"rufler stop {reg_entry.id})[/dim]"
            )

        init_swarm_stack(runner, cfg, con, skip_init)

        analysis = run_deep_think(
            cfg, con, force_new=new, log_path=primary_log_path,
        )
        decompose_task_group(
            cfg, con, force_new=new, analysis=analysis,
            log_path=primary_log_path,
        )

        try:
            tasks = cfg.task.iter_tasks(cfg.base_dir)
        except (FileNotFoundError, ValueError) as e:
            con.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if not tasks:
            con.print(
                "[red]task is empty[/red] — set [bold]task.main[/bold], "
                "[bold]task.main_path[/bold], or [bold]task.group[/bold]"
            )
            raise typer.Exit(1)

        print_run_plan(cfg, tasks, con)

        if dry_run:
            con.print("[yellow]dry-run: stopping before executing ruflo[/yellow]")
            raise typer.Exit(0)

        _perm_settings_path: Path | None = None
        _perm_previous_mode: str | None = None
        if eff.yolo:
            _perm_settings_path, _perm_previous_mode = ensure_bypass_permissions(
                cfg.base_dir,
            )
            con.print(
                f"[dim]yolo: wrote permissions.defaultMode=bypassPermissions → "
                f"{_perm_settings_path}[/dim]"
            )

        if cfg.task.multi and cfg.task.run_mode == "parallel" and not eff.background:
            con.print(
                "[yellow]warning:[/yellow] run_mode=parallel with foreground "
                "execution runs tasks one after another (terminal blocks). "
                "Add [bold]-d[/bold] / [bold]--background[/bold] for true parallelism."
            )

        con.rule("[bold]5. hive-mind spawn --claude[/bold]")
        con.print(
            f"[dim]mode: background={eff.background} non_interactive={eff.non_interactive} "
            f"yolo={eff.yolo} run_mode={cfg.task.run_mode} tasks={len(tasks)}[/dim]"
        )

        def _log_path_for(tname: str) -> Path:
            if len(tasks) > 1:
                return (
                    cfg.base_dir
                    / eff.log_file.parent
                    / f"{eff.log_file.stem}.{tname}{eff.log_file.suffix}"
                ).resolve()
            return (cfg.base_dir / eff.log_file).resolve()

        if not cfg.task.multi:
            _source = "main"
        elif cfg.task.decompose:
            _source = "decomposed"
        elif cfg.task.group:
            _source = "group"
        else:
            _source = "inline"

        _file_paths: dict[str, str] = {}
        try:
            for item in cfg.task.group:
                if getattr(item, "file_path", None):
                    _file_paths[item.name] = str(item.file_path)
        except Exception:
            pass

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

        # --- Resume logic ---
        skip_slots: set[int] = set()
        if from_task is not None:
            for te in reg_entry.tasks:
                if te.slot < from_task:
                    skip_slots.add(te.slot)
            if skip_slots:
                con.print(
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
                        for te in reg_entry.tasks:
                            if te.slot in skip_slots:
                                prev_te = done_map.get(te.name)
                                if prev_te:
                                    te.started_at = prev_te.started_at
                                    te.finished_at = prev_te.finished_at
                                    te.rc = 0
                                    # Inherit the original log so future
                                    # resumes still see claude's real
                                    # output when verifying completion —
                                    # the new run's log path for this
                                    # slot will stay empty.
                                    if prev_te.log_path:
                                        te.log_path = prev_te.log_path
                        registry.update(reg_entry)
                        remaining = len(reg_entry.tasks) - len(skip_slots)
                        first_active = next(
                            (te.name for te in reg_entry.tasks
                             if te.slot not in skip_slots),
                            "?",
                        )
                        con.print(
                            f"[cyan]resuming:[/cyan] skipping {len(skip_slots)} "
                            f"completed task(s), starting from "
                            f"[bold]{first_active}[/bold] ({remaining} remaining)"
                        )
                        con.print(
                            f"[dim]previous run: {prev_entry.id} — "
                            f"use [bold]--new[/bold] to start from scratch[/dim]"
                        )

        def _task_by_slot(slot: int) -> TaskEntry:
            return reg_entry.tasks[slot - 1]

        def _spawn_one(
            idx: int, tname: str, tbody: str,
            previous_tasks: list | None = None,
        ) -> None:
            te = _task_by_slot(idx)
            objective = cfg.build_objective(
                task_body=tbody, task_name=tname,
                previous_tasks=previous_tasks, analysis=analysis,
            )
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
                con.print(
                    f"[green]▶ task[/green] [bold]{tname}[/bold] "
                    f"[dim]pid={pid} log={log_path}[/dim]"
                )
                te.pid = int(pid) if pid else None
                if pid:
                    reg_entry.pids.append(int(pid))
                registry.update(reg_entry)
            else:
                con.rule(f"[bold]▶ {tname}[/bold]  [dim]log={log_path}[/dim]")
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
                if task_rc == 0 and cfg.task.on_task_complete.report:
                    run_report(
                        cfg, runner, reg_entry, registry, eff, con,
                        spec=cfg.task.on_task_complete,
                        task_name=tname, source="task_report",
                    )

        chain_history: list = []

        def _collect_chain(idx: int, tname: str, tbody: str, task_rc: int) -> None:
            if not cfg.task.chain:
                return
            item = (
                cfg.task.group[idx - 1]
                if cfg.task.group and idx <= len(cfg.task.group)
                else None
            )
            item_chain = getattr(item, "chain", None) if item else None
            if not resolve_chain_flag(cfg.task, item_chain):
                return
            report_path = None
            if cfg.task.chain_include_report and cfg.task.on_task_complete.report:
                rp = cfg.task.on_task_complete.report_path.replace("{task}", tname)
                candidate = (cfg.base_dir / rp).resolve()
                if candidate.exists():
                    report_path = candidate
            chain_history.append(collect_chain_entry(
                name=tname,
                slot=idx,
                total=len(tasks),
                body=tbody,
                report_path=report_path,
                rc=task_rc,
                max_tokens=cfg.task.chain_max_tokens,
            ))

        def _chain_for_task(idx: int) -> list | None:
            if not cfg.task.chain or not chain_history:
                return None
            item = (
                cfg.task.group[idx - 1]
                if cfg.task.group and idx <= len(cfg.task.group)
                else None
            )
            item_chain = getattr(item, "chain", None) if item else None
            if not resolve_chain_flag(cfg.task, item_chain):
                return None
            return chain_history

        try:
            if cfg.task.run_mode == "parallel" or not cfg.task.multi:
                pre_spawn_sizes: dict[int, int] = {}
                if eff.background:
                    for i, (tname, _tbody) in enumerate(tasks, 1):
                        if i in skip_slots:
                            continue
                        lp = _log_path_for(tname)
                        if lp.exists():
                            try:
                                pre_spawn_sizes[i] = lp.stat().st_size
                            except OSError:
                                pre_spawn_sizes[i] = 0
                        else:
                            pre_spawn_sizes[i] = 0
                for i, (tname, tbody) in enumerate(tasks, 1):
                    if i in skip_slots:
                        con.print(
                            f"[dim]⏭ {tname}[/dim] [dim](completed in previous run)[/dim]"
                        )
                        continue
                    _spawn_one(i, tname, tbody)
                if eff.background:
                    # Background parallel: _spawn_one detaches and returns
                    # without waiting. Without this pass, te.rc stays None so
                    # on_task_complete.report never fires and the final-report
                    # any_succeeded check sees rc=None → skips too.
                    for i, (tname, tbody) in enumerate(tasks, 1):
                        if i in skip_slots:
                            continue
                        te = reg_entry.tasks[i - 1]
                        con.print(f"[dim]waiting for {tname} to finish...[/dim]")
                        found, log_rc = wait_for_log_end(
                            Path(te.log_path),
                            cfg.task.timeout_minutes * 60,
                            con,
                            start_offset=pre_spawn_sizes.get(i, 0),
                            supervisor_pid=te.pid,
                        )
                        task_rc = log_rc if log_rc is not None else (
                            0 if found else 1
                        )
                        te.finished_at = time.time()
                        te.rc = task_rc
                        emit_task_marker(
                            Path(te.log_path), "task_end",
                            task_id=te.id, slot=te.slot, rc=task_rc,
                        )
                        registry.update(reg_entry)
                        if task_rc == 0 and cfg.task.on_task_complete.report:
                            run_report(
                                cfg, runner, reg_entry, registry, eff, con,
                                spec=cfg.task.on_task_complete,
                                task_name=tname, source="task_report",
                            )
            else:
                for i, (tname, tbody) in enumerate(tasks, 1):
                    if i in skip_slots:
                        con.print(
                            f"[dim]⏭ [{i}/{len(tasks)}] {tname} — "
                            f"skipped (completed)[/dim]"
                        )
                        continue
                    con.print(f"\n[bold cyan]━ [{i}/{len(tasks)}] {tname}[/bold cyan]")
                    if chain_history:
                        con.print(
                            f"[dim]chain: injecting retrospective from "
                            f"{len(chain_history)} previous task(s)[/dim]"
                        )
                    pre_spawn_size = 0
                    if eff.background:
                        lp = _log_path_for(tname)
                        if lp.exists():
                            try:
                                pre_spawn_size = lp.stat().st_size
                            except OSError:
                                pre_spawn_size = 0
                    _spawn_one(i, tname, tbody, previous_tasks=_chain_for_task(i))
                    if eff.background:
                        te = reg_entry.tasks[i - 1]
                        con.print(f"[dim]waiting for {tname} to finish...[/dim]")
                        found, log_rc = wait_for_log_end(
                            Path(te.log_path),
                            cfg.task.timeout_minutes * 60,
                            con,
                            start_offset=pre_spawn_size,
                            supervisor_pid=te.pid,
                        )
                        task_rc = log_rc if log_rc is not None else (
                            0 if found else 1
                        )
                        te.finished_at = time.time()
                        te.rc = task_rc
                        emit_task_marker(
                            Path(te.log_path), "task_end",
                            task_id=te.id, slot=te.slot, rc=task_rc,
                        )
                        registry.update(reg_entry)
                        if task_rc == 0 and cfg.task.on_task_complete.report:
                            run_report(
                                cfg, runner, reg_entry, registry, eff, con,
                                spec=cfg.task.on_task_complete,
                                task_name=tname, source="task_report",
                            )
                        _collect_chain(i, tname, tbody, task_rc)
                    else:
                        te = _task_by_slot(i)
                        task_rc = te.rc if te.rc is not None else 0
                        _collect_chain(i, tname, tbody, task_rc)
        except KeyboardInterrupt:
            con.print("\n[yellow]interrupted — cleaning up[/yellow]")
            if not eff.background:
                try:
                    import signal
                    for p in find_claude_procs(cfg.base_dir):
                        try:
                            os.kill(int(p.get("pid", 0)), signal.SIGTERM)
                        except (ProcessLookupError, PermissionError,
                                ValueError, OSError):
                            pass
                except Exception:
                    pass
            reg_entry.finished_at = time.time()
            try:
                registry.update(reg_entry)
            except Exception:
                pass
            raise typer.Exit(130)

        if cfg.task.on_complete.report:
            any_succeeded = any(
                te.rc == 0 for te in reg_entry.tasks
                if te.source not in ("task_report", "final_report")
            )
            if any_succeeded:
                run_report(
                    cfg, runner, reg_entry, registry, eff, con,
                    spec=cfg.task.on_complete,
                    task_name="final", source="final_report",
                )

        if _perm_settings_path is not None:
            restore_permissions(_perm_settings_path, _perm_previous_mode)

        restore_rufler_ignored(_rufler_ignore_path, _rufler_previous_deny)

        finalize_run(reg_entry, registry, base_task_id, con)
        if eff.background:
            os._exit(0)

    app.command(
        "run",
        help="Run a rufler flow (foreground; -d to detach, like docker run).",
    )(run_cmd)
    app.command(
        "start", hidden=True, help="Deprecated alias for `rufler run`.",
    )(run_cmd)

    @app.command("build")
    def build_cmd(
        flow_file: Optional[Path] = typer.Argument(
            None,
            metavar="[FLOW_FILE]",
            help="Path to flow yml (positional). "
                 "Overrides --config and default rufler_flow.yml.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        skip_checks: bool = typer.Option(False, "--skip-checks"),
        skip_init: bool = typer.Option(
            False, "--skip-init",
            help="Skip ruflo init + daemon + memory init",
        ),
    ):
        """Apply rufler_flow.yml to the project WITHOUT launching the swarm.

        Runs the same preparation steps as `rufler run` — ruflo init + daemon,
        memory init, skills install (packs + extra + custom), swarm init,
        hive-mind init, autopilot config — but stops before spawning Claude Code.
        Use after editing the yml (new agents, changed memory, added/removed
        skills) to re-sync ruflo state; then `rufler run` to actually start.
        """
        if not skip_checks:
            if not print_checks(console):
                console.print(
                    "[red]Dependency check failed.[/red] Fix above or use --skip-checks."
                )
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
            console.print(
                "[yellow]no agents defined — build will still prep ruflo state[/yellow]"
            )

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
