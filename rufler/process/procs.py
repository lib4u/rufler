"""Process discovery, signalling, and formatting utilities.

Linux-centric: `/proc` walks for descendant discovery and `ps -eo` for
listing `claude -p` workers tied to a project dir. On non-Linux the
discovery helpers return empty lists; `kill_pid_tree` still signals the
leader PID.
"""
from __future__ import annotations

import os
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

    Linux-only — reads ``/proc/<pid>/cmdline`` directly instead of parsing
    ``ps -eo`` output, which avoids breakage when command-line formatting
    changes or ``ps`` is unavailable. On other platforms returns [].
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return []

    procs: list[dict] = []
    target = str(project_dir.resolve())
    my_pid = os.getpid()

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmdline_raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline_raw:
            continue
        argv = cmdline_raw.rstrip(b"\x00").split(b"\x00")
        argv_strs = [a.decode("utf-8", errors="replace") for a in argv]
        # Match: any argv[i] ending with "claude" followed by "-p" anywhere.
        has_claude = any(
            a.endswith("claude") or a.endswith("claude.js") for a in argv_strs
        )
        has_p_flag = "-p" in argv_strs or "--print" in argv_strs
        if not (has_claude and has_p_flag):
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if not str(cwd).startswith(target):
            continue
        # Compute elapsed time from /proc/<pid>/stat field 22 (starttime).
        etime = _proc_etime(pid)
        cmd = " ".join(argv_strs)[:80]
        procs.append({"pid": pid, "etime": etime, "cmd": cmd})
    return procs


def _proc_etime(pid: int) -> str:
    """Compute elapsed time string from ``/proc/<pid>/stat`` starttime.

    Falls back to ``"-"`` on any error.
    """
    try:
        stat_raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rp = stat_raw.rfind(")")
        if rp < 0:
            return "-"
        fields = stat_raw[rp + 2:].split()
        # Field index 19 (0-based after the comm closing paren) is starttime
        # in clock ticks since boot.
        starttime_ticks = int(fields[19])
        clk_tck = os.sysconf("SC_CLK_TCK")

        uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
        start_secs = starttime_ticks / clk_tck
        # Process started at boot_time + start_secs; boot_time = now - uptime
        boot_epoch = time.time() - uptime_secs
        proc_start_epoch = boot_epoch + start_secs
        return fmt_age(proc_start_epoch)
    except (OSError, ValueError, IndexError):
        return "-"


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
