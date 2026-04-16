"""``rufler stop`` and ``rufler rm`` commands."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from ..process import (
    DEFAULT_FLOW_FILE,
    find_claude_procs,
    kill_pid_tree,
    resolve_entry_or_cwd,
)
from ..registry import Registry, _pid_alive
from ..runner import Runner


def register(app: typer.Typer, console: Console) -> None:

    @app.command()
    def stop(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id from `rufler ps`. Omit to use current dir.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        success: bool = typer.Option(
            True, "--success/--failure",
            help="Record outcome for neural training",
        ),
        kill: bool = typer.Option(
            True, "--kill/--no-kill",
            help="SIGTERM the run's supervisor pids and their descendants "
                 "(default on). Use --no-kill to only run ruflo teardown "
                 "without touching processes.",
        ),
        grace: float = typer.Option(
            8.0, "--grace",
            help="Seconds between SIGTERM and the guaranteed SIGKILL. Long "
                 "enough for claude to flush its session state to disk, short "
                 "enough that stop never hangs.",
        ),
        teardown_timeout: float = typer.Option(
            10.0, "--teardown-timeout",
            help="Per-step timeout for best-effort ruflo teardown (post-task, "
                 "session-end, daemon stop). Runs after the kill so it can "
                 "never block the actual stop.",
        ),
    ):
        """Shutdown autopilot, hive-mind, daemon; SIGTERM the supervisor pid tree;
        record post-task outcome; end session.

        By default this DOES kill the supervisor and every descendant claude proc.
        Pass --no-kill to skip the kill step (e.g. when you only want the ruflo
        teardown side-effects but the run is already dead).
        """
        import signal as _sig

        entry, cwd, _ = resolve_entry_or_cwd(
            id_prefix, config, console, require_existing_dir=False,
        )

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
                console.print(
                    "[dim]pass an id explicitly: rufler stop <id>[/dim]"
                )
                raise typer.Exit(1)

        def _terminate(entry_obj):
            if not kill or entry_obj is None:
                return
            pids: list[int] = list(entry_obj.pids or [])
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
            deadline = time.time() + max(0.0, grace)
            while time.time() < deadline:
                alive = any(_pid_alive(pid) for pid in pids)
                if not alive:
                    break
                time.sleep(0.2)
            leftover = [pid for pid in pids if _pid_alive(pid)]
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
                        f"[red]SIGKILL fallback:[/red] {killed} process(es) "
                        f"refused SIGTERM"
                    )

        if kill:
            console.print(
                f"[dim]sending SIGTERM (claude has {grace:.0f}s to flush state "
                f"to memory before SIGKILL)[/dim]"
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
                console.print(
                    f"[yellow]{label} failed:[/yellow] [dim]{err[0]}[/dim]"
                )

        r = Runner(cwd=cwd)
        project_name = entry.project if entry else cwd.name
        task_id = f"rufler-{project_name}-stop"
        console.print("[dim]best-effort ruflo teardown[/dim]")
        _bg("hooks post-task",
            lambda: r.hooks_post_task(task_id=task_id, success=success))
        _bg("autopilot disable", r.autopilot_disable)
        _bg("hive-mind shutdown", r.hive_shutdown)
        _bg("hooks session-end", r.hooks_session_end)
        _bg("daemon stop", r.daemon_stop)

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
            None, metavar="[ID]...",
            help="Run ids (prefixes) to remove from the registry",
        ),
        all_finished: bool = typer.Option(
            False, "--all-finished",
            help="Remove every entry that is not currently running",
        ),
        older_than_days: Optional[int] = typer.Option(
            None, "--older-than-days",
            help="Remove finished entries older than N days",
        ),
    ):
        """Remove rufler run entries from the central registry.

        This only deletes the registry bookkeeping — it does NOT touch log files,
        project directories or ruflo state. Use `rufler stop <id>` first if the
        run is still alive.
        """
        reg = Registry()

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
            removed = reg.prune(
                missing_dirs=False, older_than_sec=older_than_days * 86400,
            )
            console.print(
                f"[dim]removed {removed} entries older than {older_than_days}d[/dim]"
            )
            return

        if all_finished:
            entries = reg.list_refreshed(include_all=True)
            targets = [e for e in entries if e.status != "running"]
            if not targets:
                console.print(
                    "[yellow]nothing to remove (no finished runs)[/yellow]"
                )
                return
            removed = reg.remove_many([e.id for e in targets])
            console.print(f"[green]removed {removed} finished runs[/green]")
            return

        if not ids:
            console.print(
                "[red]pass one or more run ids, or use "
                "--all-finished / --older-than-days[/red]"
            )
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
                console.print(
                    f"[green]removed[/green] [cyan]{entry.id}[/cyan]  "
                    f"{entry.project}"
                )
                removed_count += 1
        if removed_count:
            console.print(f"[dim]{removed_count} entries removed[/dim]")
