from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from rich.console import Console

from .checks import resolve_ruflo_cmd


def ensure_bypass_permissions(cwd: Path) -> Path:
    """Write .claude/settings.local.json with permissions.defaultMode=bypassPermissions.

    This is a belt-and-suspenders fix: even if ruflo's arg parser drops
    --dangerously-skip-permissions when objective is huge, Claude Code will
    still read the project-local setting and skip prompts.
    """
    settings_dir = cwd / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"

    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}

    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    perms["defaultMode"] = "bypassPermissions"
    data["permissions"] = perms

    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return settings_path

console = Console()


class Runner:
    """Thin wrapper that invokes ruflo CLI. Resolution order matches
    `rufler check`: $RUFLER_RUFLO_BIN → local node_modules → PATH →
    npm global bin → `npx -y $RUFLER_RUFLO_SPEC` (default ruflo@latest)."""

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd)
        cmd, source = resolve_ruflo_cmd(self.cwd)
        if not cmd:
            raise RuntimeError(
                "ruflo not found. Run `rufler check` for installation options."
            )
        self.base_cmd = cmd
        self.source = source

    def run(self, args: list[str], check: bool = False) -> int:
        cmd = self.base_cmd + args
        console.print(f"[dim]$ {' '.join(self._display(a) for a in cmd)}[/dim]")
        try:
            r = subprocess.run(cmd, cwd=self.cwd)
            if check and r.returncode != 0:
                raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}")
            return r.returncode
        except FileNotFoundError as e:
            console.print(f"[red]command not found:[/red] {e}")
            return 127

    @staticmethod
    def _display(arg: str) -> str:
        # Shorten very long --objective= values for log readability
        if arg.startswith("--objective=") and len(arg) > 120:
            return arg[:117] + "..."
        return arg

    # ---- ruflo subcommand helpers ----

    def init_project(self, start_daemon: bool = False):
        # `ruflo init` has no --non-interactive flag; --force avoids overwrite prompts.
        # --start-daemon optionally boots the daemon in the same step.
        args = ["init", "--force"]
        if start_daemon:
            args.append("--start-daemon")
        self.run(args)

    def init_skills(
        self,
        *,
        all_packs: bool = False,
        packs: list[str] | None = None,
        force: bool = True,
    ) -> int:
        """Run `ruflo init skills` with the given pack toggles. Maps directly
        to ruflo's skillsCommand (v3/@claude-flow/cli/src/commands/init.ts).
        `packs` must contain only names from config.CLI_FLAG_PACKS — caller
        is responsible for filtering out MANUAL_COPY_PACKS which have no CLI
        flag. Returns the subprocess rc — non-fatal for the caller."""
        args = ["init", "skills"]
        if all_packs:
            args.append("--all")
        else:
            for p in packs or []:
                args.append(f"--{p}")
        if force:
            args.append("--force")
        return self.run(args)

    def daemon_start(self):
        self.run(["daemon", "start"])

    def daemon_stop(self):
        self.run(["daemon", "stop"])

    def memory_init(self, backend: str | None = None):
        args = ["memory", "init"]
        if backend:
            args.append(f"--backend={backend}")
        self.run(args)

    def swarm_init(self, topology: str, max_agents: int, strategy: str):
        self.run(
            [
                "swarm",
                "init",
                f"--topology={topology}",
                f"--max-agents={max_agents}",
                f"--strategy={strategy}",
            ]
        )

    def swarm_status(self):
        self.run(["swarm", "status"])

    def hive_init(
        self,
        topology: str,
        consensus: str,
        max_agents: int | None = None,
        memory_backend: str | None = None,
    ):
        args = [
            "hive-mind",
            "init",
            f"--topology={topology}",
            f"--consensus={consensus}",
        ]
        if max_agents is not None:
            args.append(f"--max-agents={max_agents}")
        if memory_backend:
            args.append(f"--memory-backend={memory_backend}")
        self.run(args)

    def hive_status(self):
        self.run(["hive-mind", "status"])

    def hive_shutdown(self, force: bool = True):
        args = ["hive-mind", "shutdown"]
        if force:
            args.append("--force")
        self.run(args)

    def hive_spawn_claude(
        self,
        count: int,
        objective: str,
        role: str = "specialist",
        non_interactive: bool = False,
        skip_permissions: bool = False,
        dry_run: bool = False,
        log_path: Path | None = None,
        detach: bool = False,
    ) -> int | None:
        """Run `ruflo hive-mind spawn --claude ...`.

        - `log_path=None, detach=False` → plain foreground, stdout to terminal.
        - `log_path=<p>, detach=False` → foreground via logwriter --tee
           (output streams to terminal AND NDJSON file).
        - `log_path=<p>, detach=True`  → background supervisor (detached,
           stdio → /dev/null, NDJSON only). Returns PID.
        """
        # IMPORTANT: --objective MUST be last. Its value is huge (full
        # composed prompt) and some arg parsers greedily consume trailing
        # tokens into it, which silently drops flags placed after it.
        args = [
            "hive-mind",
            "spawn",
            f"--count={count}",
            f"--role={role}",
            "--claude",
        ]
        if non_interactive:
            args.append("--non-interactive")
        if skip_permissions:
            # Use =true form so the boolean flag survives any permissive parser.
            args.append("--dangerously-skip-permissions=true")
        if dry_run:
            args.append("--dry-run")
        args.append(f"--objective={objective}")

        # No log file → simple foreground passthrough
        if log_path is None:
            return self.run(args)

        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = self.base_cmd + args
        import sys as _sys

        if detach:
            # Background supervisor, detached from terminal.
            console.print(
                f"[dim]$ (detached, ndjson) {' '.join(self._display(a) for a in cmd)}[/dim]"
            )
            writer_cmd = [
                _sys.executable, "-m", "rufler.logwriter",
                str(log_path), "--", *cmd,
            ]
            # Open /dev/null in a with-block so the parent-side fd is closed
            # immediately after Popen dups it into the child. Prevents fd leaks
            # when spawning many background tasks in one run.
            with open(os.devnull, "ab") as devnull_out:
                proc = subprocess.Popen(
                    writer_cmd,
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=devnull_out,
                    stderr=devnull_out,
                    start_new_session=True,
                    close_fds=True,
                )
            return proc.pid

        # Foreground tee mode: streams to terminal AND NDJSON log.
        console.print(
            f"[dim]$ (tee→{log_path.name}) {' '.join(self._display(a) for a in cmd)}[/dim]"
        )
        writer_cmd = [
            _sys.executable, "-m", "rufler.logwriter",
            "--tee", str(log_path), "--", *cmd,
        ]
        try:
            r = subprocess.run(writer_cmd, cwd=self.cwd)
            return r.returncode
        except FileNotFoundError as e:
            console.print(f"[red]command not found:[/red] {e}")
            return 127

    def agent_spawn(self, agent_type: str, name: str):
        self.run(["agent", "spawn", f"--type={agent_type}", f"--name={name}"])

    # ---- autopilot (persistent completion loop) ----

    def autopilot_config(self, max_iterations: int, timeout_minutes: int):
        self.run(
            [
                "autopilot",
                "config",
                f"--max-iterations={max_iterations}",
                f"--timeout={timeout_minutes}",
            ]
        )

    def autopilot_enable(self):
        self.run(["autopilot", "enable"])

    def autopilot_disable(self):
        self.run(["autopilot", "disable"])

    def autopilot_status(self):
        self.run(["autopilot", "status"])

    def autopilot_history(self, query: str, limit: int = 20):
        self.run(
            [
                "autopilot",
                "history",
                f"--query={query}",
                f"--limit={limit}",
            ]
        )

    def autopilot_log(self, last: int = 50):
        self.run(["autopilot", "log", f"--last={last}"])

    # ---- hooks lifecycle ----

    def hooks_pre_task(self, task_id: str, description: str):
        self.run(
            [
                "hooks",
                "pre-task",
                f"--task-id={task_id}",
                f"--description={description}",
            ]
        )

    def hooks_post_task(self, task_id: str, success: bool = True):
        self.run(
            [
                "hooks",
                "post-task",
                f"--task-id={task_id}",
                f"--success={'true' if success else 'false'}",
            ]
        )

    def hooks_session_end(self):
        self.run(["hooks", "session-end"])

    # ---- doctor ----

    def doctor(self, fix: bool = False):
        args = ["doctor"]
        if fix:
            args.append("--fix")
        self.run(args)

    # ---- top-level status ----

    def system_status(self):
        self.run(["status"])
