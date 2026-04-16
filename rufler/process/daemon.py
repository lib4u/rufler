"""Daemonization + log path resolution + registry entry resolution.

These helpers used to live in cli.py. They implement docker-like `-d` mode
(double-fork into a session leader) and the single source-of-truth log path
rules shared by `logs`, `follow`, `ps`, and `stop`.
"""
from __future__ import annotations

import json as _json
import os
import resource
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from ..config import FlowConfig
from ..registry import Registry, RunEntry

DEFAULT_FLOW_FILE = "rufler_flow.yml"
DEFAULT_LOG_REL = Path(".rufler") / "run.log"


def setup_log_for(run_log: Path) -> Path:
    """Sibling raw-text log for the daemonized supervisor's stdout/stderr.

    Why a separate file: the NDJSON `run.log` is what `rufler follow` watches.
    If we dump raw ruflo init/swarm/hive-mind output into it, the file becomes
    a mix of NDJSON and raw text, the dashboard shows nothing until the claude
    stream finally starts, and `cat .rufler/run.log` looks broken. Setup goes
    to `.rufler/setup.log`, the live NDJSON stream stays clean in `run.log`.
    """
    return run_log.with_name(run_log.stem + ".setup" + run_log.suffix)


def daemonize(log_path: Path) -> None:
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
    # Close inherited fds (3..max) to avoid leaking parent's sockets/pipes.
    max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    os.closerange(3, max_fd)
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


def resolve_log_path(
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


def resolve_entry_or_cwd(
    id_or_none: Optional[str],
    config: Path,
    console: Console,
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
    cwd = config.resolve().parent if config.exists() else Path.cwd()
    log_path: Optional[Path] = None
    if config.exists():
        try:
            cfg = FlowConfig.load(config.resolve())
            log_path = (cfg.base_dir / cfg.execution.log_file).resolve()
        except Exception:
            log_path = None
    return None, cwd, log_path


def wait_for_log_end(
    log_path: Path,
    timeout_sec: int,
    console: Console,
    *,
    start_offset: int = 0,
) -> tuple[bool, Optional[int]]:
    """Poll log_path for the logwriter's 'log ended' marker, only scanning
    bytes ADDED after `start_offset`. This avoids false positives from stale
    'log ended' lines left over from a previous run.

    Returns (found, rc): found=True when end-marker is detected, rc is the
    exit code parsed from ``log ended rc=N`` (None if unparsable or timed out).
    """
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
                            rc: Optional[int] = None
                            for part in text.split():
                                if part.startswith("rc="):
                                    try:
                                        rc = int(part[3:])
                                    except ValueError:
                                        pass
                            return True, rc
                    last_size = size
            except Exception as e:
                if not warned:
                    console.print(
                        f"[yellow]warning: log poll error on {log_path}:[/yellow] "
                        f"[dim]{e}[/dim] (further errors suppressed)"
                    )
                    warned = True
        time.sleep(3)
    return False, None
