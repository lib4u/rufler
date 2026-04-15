"""Process discovery, signalling, and formatting utilities.

Linux-centric: `/proc` walks for descendant discovery and `ps -eo` for
listing `claude -p` workers tied to a project dir. On non-Linux the
discovery helpers return empty lists; `kill_pid_tree` still signals the
leader PID.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_age(ts: Optional[float]) -> str:
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


def find_claude_procs(project_dir: Path) -> list[dict]:
    """Return live `claude -p` processes whose working dir is inside project_dir.

    Linux-only (relies on /proc/<pid>/cwd). On other platforms returns [].
    """
    if not Path("/proc").is_dir():
        return []

    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,etime,command"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
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
        except OSError:
            continue
        if not str(cwd).startswith(target):
            continue
        procs.append({"pid": int(pid_s), "etime": etime, "cmd": cmd[:80]})
    return procs


def kill_pid_tree(pid: int, sig) -> int:
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
    try:
        os.killpg(pid, sig)
        sent += 1
    except (ProcessLookupError, PermissionError, OSError):
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
            parts = raw[rp + 2:].split()
            if len(parts) < 2:
                continue
            try:
                ppid = int(parts[1])
            except ValueError:
                continue
            if ppid == parent:
                kids.append(int(d.name))
        return kids

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
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return sent
