"""Cross-cutting orchestration helpers shared by multiple commands.

- `print_checks()` — the `rufler check` dependency table, also used as a
  pre-flight banner inside `run` / `build`.
- `init_swarm_stack()` — ruflo init → skills → swarm → hive → autopilot
  sequence shared between `run_cmd` and `build_cmd`.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from .checks import check_all, check_skills_sh_cli
from .config import FlowConfig, SkillsShEntry
from .process import DEFAULT_FLOW_FILE
from .runner import Runner
from .skills import install_skills


def print_checks(console: Console) -> bool:
    """Run dependency checks and render the rufler check table.

    `check_all()` always includes a shallow skills.sh presence check. When
    the cwd's flow file declares `SkillsShEntry` items, we upgrade that row
    to a deep probe (`npx -y skills --help`) AND mark it as required. For
    a plain `rufler check` with no skills.sh usage it stays optional — a
    missing skills.sh row is shown but doesn't fail the overall result.
    """
    results = check_all(Path.cwd())
    skills_sh_required = False

    try:
        cfg_path = Path.cwd() / DEFAULT_FLOW_FILE
        if cfg_path.exists():
            cfg = FlowConfig.load(cfg_path)
            if cfg.skills.enabled and any(
                isinstance(e, SkillsShEntry) for e in cfg.skills.custom
            ):
                skills_sh_required = True
                deep = check_skills_sh_cli(deep=True)
                results = [
                    deep if r.name == "skills.sh" else r for r in results
                ]
    except Exception as e:
        console.print(
            f"[dim]skills.sh probe skipped: {type(e).__name__}: {e}[/dim]"
        )

    table = Table(title="rufler dependency check")
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Version / Hint")
    all_ok = True
    for r in results:
        optional = r.name == "skills.sh" and not skills_sh_required
        if r.ok:
            status = "[green]OK[/green]"
        elif optional:
            status = "[yellow]OPTIONAL[/yellow]"
        else:
            status = "[red]MISSING[/red]"
        info = r.version or r.hint or ""
        table.add_row(r.name, status, r.source or "-", info)
        if not r.ok and not optional:
            all_ok = False
    console.print(table)
    return all_ok


def init_swarm_stack(
    runner: Runner,
    cfg: FlowConfig,
    console: Console,
    skip_init: bool,
) -> None:
    """Run the shared ruflo init → skills → swarm → hive → autopilot sequence.

    Used by both `run_cmd` and `build_cmd`. Anything that differs between
    those two commands (daemonize, yolo permissions, spawn, success banner)
    stays at the call site.
    """
    if not skip_init:
        console.rule("[bold]1. ruflo init + daemon[/bold]")
        runner.init_project(start_daemon=True)
        if cfg.memory.init:
            runner.memory_init(backend=cfg.memory.backend)
        install_skills(runner, cfg, console)

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
