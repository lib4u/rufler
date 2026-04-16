"""Interactive TUI that tails rufler NDJSON logs and renders a live
dashboard with task progress, AI conversation stream, and system events.

Reads logs produced by `rufler.logwriter` (one JSON object per line).
Supports multi-log tailing for multi-task runs where each task writes
to its own log file.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Optional

from rich.console import Console as RichConsole
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

LEVEL_COLOR = {
    "error": "bold red",
    "warn": "yellow",
    "ok": "green",
    "info": "cyan",
    "debug": "dim",
}

STATUS_ICON = {
    "queued": "  ",
    "running": "▶ ",
    "done": "✓ ",
    "failed": "✗ ",
    "stopped": "■ ",
    "skipped": "⏭ ",
}

STATUS_STYLE = {
    "queued": "dim",
    "running": "bold green",
    "done": "blue",
    "failed": "bold red",
    "stopped": "yellow",
    "skipped": "dim",
}

CONV_KIND_STYLE = {
    "thinking": "dim italic",
    "text": "white",
    "tool_use": "bold cyan",
    "tool_result": "dim green",
}


@dataclass
class TaskInfo:
    """Per-task tracking inside the TUI."""
    task_id: str
    name: str
    status: str = "queued"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    rc: Optional[int] = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TuiState:
    log_path: Path
    started_at: float = field(default_factory=time.time)

    # Session
    session_id: Optional[str] = None
    model: Optional[str] = None
    swarm_id: Optional[str] = None
    workers: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    last_tool: Optional[str] = None
    last_tool_desc: Optional[str] = None
    status: str = "starting"
    _seen_mids: set = field(default_factory=set, repr=False)

    # Tasks
    task_list: list[TaskInfo] = field(default_factory=list)
    active_task: Optional[str] = None

    # Conversation (active task's AI stream)
    conversation: Deque[tuple[float, str, str]] = field(
        default_factory=lambda: deque(maxlen=80)
    )

    # System events log
    events: Deque[tuple[float, str, str]] = field(
        default_factory=lambda: deque(maxlen=30)
    )

    def _task_by_name(self, name: str) -> Optional[TaskInfo]:
        for t in self.task_list:
            if t.name == name:
                return t
        return None

    def ingest(self, rec: dict) -> None:
        src = rec.get("src")
        t = rec.get("type")
        ts = float(rec.get("ts") or time.time())

        # ---- rufler supervisor markers ----
        if src == "rufler":
            text = rec.get("text") or ""

            if t == "task_start":
                name = rec.get("name") or ""
                ti = self._task_by_name(name)
                if ti and ti.status in ("queued",):
                    ti.status = "running"
                    ti.started_at = ti.started_at or ts
                self.active_task = name
                self.status = "running"
                self._push_event(ts, "info", f"task_start {name}")
                return

            if t == "task_end":
                task_id = rec.get("task_id") or ""
                rc = rec.get("rc")
                name = ""
                for ti in self.task_list:
                    if ti.task_id == task_id:
                        name = ti.name
                        ti.finished_at = ti.finished_at or ts
                        ti.rc = int(rc) if rc is not None else ti.rc
                        ti.status = "done" if (ti.rc or 0) == 0 else "failed"
                        break
                st = "done" if rc == 0 else f"failed(rc={rc})"
                self._push_event(ts, "ok" if rc == 0 else "error",
                                 f"task_end {name} {st}")
                nxt = next((x for x in self.task_list
                            if x.status == "queued"), None)
                if nxt:
                    self.active_task = nxt.name
                else:
                    self.active_task = None
                return

            if "log started" in text:
                if self.status == "starting":
                    self.status = "running"
            elif "log ended" in text:
                # Mark active task done when its log ends.
                if self.active_task:
                    ti = self._task_by_name(self.active_task)
                    if ti and ti.status == "running":
                        ti.status = "done" if "rc=0" in text else "failed"
                        ti.finished_at = ti.finished_at or ts
                has_remaining = any(
                    ti.status in ("queued", "running")
                    for ti in self.task_list
                )
                if not has_remaining:
                    self.status = "done" if "rc=0" in text else "failed"
            if text:
                self._push_event(ts, rec.get("level") or "debug", text)
            return

        # ---- ruflo prelude ----
        if src == "ruflo":
            text = rec.get("text") or ""
            if "Swarm ID" in text:
                self.swarm_id = text.split(":", 1)[-1].strip()
            if "Worker Count" in text:
                try:
                    self.workers = int(text.split(":")[-1].strip())
                except Exception:
                    pass
            if "Launching Claude Code" in text:
                self.status = "claude-launching"
            if "Claude Code launched" in text or "Claude Code completed" in text:
                self.status = "claude-running"
            if text:
                self._push_event(ts, rec.get("level") or "debug", text)
            return

        # ---- claude stream-json ----
        if src == "claude":
            sub = rec.get("subtype")

            if t == "system" and sub == "init":
                self.session_id = rec.get("session_id") or self.session_id
                self.model = rec.get("model") or self.model
                self.status = "running"
                self._push_event(ts, "info",
                                 f"session init ({self.model or '-'})")
                return

            if t == "system" and sub == "task_started":
                self._push_event(ts, "info",
                                 f"claude task: {rec.get('description', '?')[:80]}")
                return

            if t == "system" and sub == "task_progress":
                usage = rec.get("usage") or {}
                self.turns = max(self.turns, int(usage.get("tool_uses") or 0))
                last_tool = rec.get("last_tool_name")
                if last_tool:
                    self.last_tool = last_tool
                    self.last_tool_desc = (rec.get("description") or "")[:60]
                return

            if t == "system" and sub == "task_completed":
                self._push_event(ts, "ok",
                                 f"claude task completed")
                return

            if t == "system" and sub in ("hook_started", "hook_response"):
                name = rec.get("hook_name") or ""
                self._push_event(ts, "debug", f"hook {sub}: {name}")
                return

            if t == "assistant":
                msg = rec.get("message") or {}
                mid = msg.get("id") or ""
                usage = msg.get("usage") or {}
                inp = int(usage.get("input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                cc = int(usage.get("cache_creation_input_tokens") or 0)

                # Deduplicate: Claude emits multiple assistant events per
                # turn (one per content block) with the same message.id.
                # Count tokens only on the first event for each mid.
                is_new_turn = mid and mid not in self._seen_mids
                if is_new_turn:
                    self._seen_mids.add(mid)
                    self.input_tokens += inp
                    self.output_tokens += out
                    # cache fields are session-cumulative: take max
                    if cr > self.cache_read:
                        self.cache_read = cr
                    if self.active_task:
                        ti = self._task_by_name(self.active_task)
                        if ti:
                            ti.input_tokens += inp
                            ti.output_tokens += out
                            ti.turns = self.turns

                content = msg.get("content") or []
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ct = c.get("type")
                        if ct == "text":
                            txt = (c.get("text") or "").strip()
                            if txt:
                                for line in txt.splitlines()[:5]:
                                    self.conversation.append(
                                        (ts, "text", line.rstrip()))
                        elif ct == "thinking":
                            th = (c.get("thinking") or "").strip()
                            if th:
                                for line in th.splitlines()[:4]:
                                    self.conversation.append(
                                        (ts, "thinking", line.rstrip()))
                        elif ct == "tool_use":
                            name = c.get("name") or "?"
                            inp_d = c.get("input") or {}
                            hint = ""
                            for k in ("file_path", "command", "pattern",
                                      "path", "query", "content"):
                                if k in inp_d:
                                    hint = str(inp_d[k])[:80]
                                    break
                            label = (f"{name}({hint})" if hint
                                     else name)
                            self.conversation.append(
                                (ts, "tool_use", label))
                return

            if t == "user":
                msg = rec.get("message") or {}
                content = msg.get("content") or []
                for c in (content if isinstance(content, list) else []):
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        txt = _stringify(c.get("content"))
                        if txt:
                            self.conversation.append(
                                (ts, "tool_result", txt[:120]))
                return

            if t == "result":
                ok = (rec.get("subtype") == "success"
                      and not rec.get("is_error"))
                result_text = rec.get("result", "")[:80]
                sym = "✓" if ok else "✗"
                self._push_event(
                    ts, "ok" if ok else "error",
                    f"RESULT {sym} {result_text}")
                # Mark the active task as done/failed.
                if self.active_task:
                    ti = self._task_by_name(self.active_task)
                    if ti and ti.status == "running":
                        ti.status = "done" if ok else "failed"
                        ti.finished_at = ti.finished_at or ts
                has_remaining = any(
                    ti.status in ("queued", "running")
                    for ti in self.task_list
                )
                if not has_remaining:
                    self.status = "done" if ok else "failed"
                return

            if t == "rate_limit_event":
                self._push_event(ts, "warn", "rate limited")
                return

    def _push_event(self, ts: float, level: str, text: str) -> None:
        self.events.append((ts, level, text))


def _stringify(x) -> str:
    if isinstance(x, str):
        return x.replace("\n", " ")
    if isinstance(x, list):
        return " ".join(_stringify(i) for i in x)
    if isinstance(x, dict):
        return _stringify(x.get("text"))
    return ""


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def _fmt_dur(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


def _render(state: TuiState) -> Layout:
    now = time.time()
    elapsed = int(now - state.started_at)
    mm, ss = divmod(elapsed, 60)
    hh, mm = divmod(mm, 60)
    uptime = f"{hh:02d}:{mm:02d}:{ss:02d}"

    sc = {
        "starting": "yellow", "claude-launching": "yellow",
        "claude-running": "cyan", "running": "cyan",
        "done": "green", "failed": "red",
    }.get(state.status, "white")

    # ---- Header ----
    total_tok = state.input_tokens + state.output_tokens + state.cache_read
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(
        Text.assemble(
            ("rufler follow  ", "bold"),
            (f"[{state.status}]", sc),
            ("  ", ""),
            (state.model or "", "dim"),
        ),
        Text.assemble(
            (f"tokens: {_fmt_tokens(total_tok)}  ", "magenta"),
            (f"  {uptime}", "dim"),
        ),
    )
    if state.swarm_id or state.session_id:
        header.add_row(
            Text.assemble(
                ("swarm: ", "dim"),
                (state.swarm_id or "-", ""),
                ("  session: ", "dim"),
                (state.session_id or "-", ""),
            ),
            Text(f"workers: {state.workers}" if state.workers else "",
                 style="dim"),
        )

    # ---- Tasks panel (left) ----
    n_done = sum(1 for t in state.task_list if t.status == "done")
    total_tasks = len(state.task_list)
    tasks_title = (f"Tasks  {n_done}/{total_tasks}"
                   if total_tasks else "Tasks")

    tasks_tbl = Table(show_header=False, box=None, expand=True,
                      pad_edge=False, show_edge=False)
    tasks_tbl.add_column("icon", width=2, no_wrap=True)
    tasks_tbl.add_column("name", ratio=1)
    tasks_tbl.add_column("status", width=8, no_wrap=True)
    tasks_tbl.add_column("dur", width=7, justify="right")
    tasks_tbl.add_column("tok", width=7, justify="right")

    for ti in state.task_list:
        icon = STATUS_ICON.get(ti.status, "  ")
        style = STATUS_STYLE.get(ti.status, "")
        dur = ""
        if ti.started_at:
            end = ti.finished_at or now
            dur = _fmt_dur(end - ti.started_at)
            if ti.status == "running":
                dur += "…"
        tok = _fmt_tokens(ti.input_tokens + ti.output_tokens) if (
            ti.input_tokens + ti.output_tokens) else ""
        tasks_tbl.add_row(
            Text(icon, style=style),
            Text(ti.name, style=style),
            Text(ti.status, style=style),
            Text(dur, style="dim"),
            Text(tok, style="magenta" if tok else "dim"),
        )

    if not state.task_list:
        tasks_tbl.add_row("", Text("(no tasks)", style="dim"), "", "", "")

    # ---- Session panel (right) ----
    sess_tbl = Table(show_header=False, box=None, expand=True,
                     pad_edge=False, show_edge=False)
    sess_tbl.add_column("k", style="dim", width=12, no_wrap=True)
    sess_tbl.add_column("v")
    sess_tbl.add_row("model", state.model or "-")
    sess_tbl.add_row(
        "tokens",
        f"[magenta]in={state.input_tokens:,}  "
        f"out={state.output_tokens:,}[/magenta]")
    sess_tbl.add_row("cache", f"[dim]{state.cache_read:,}[/dim]")
    sess_tbl.add_row("turns", str(state.turns))
    sess_tbl.add_row(
        "last tool",
        f"[bold]{state.last_tool or '-'}[/bold]")
    if state.last_tool_desc:
        sess_tbl.add_row("", f"[dim]{state.last_tool_desc}[/dim]")

    # ---- Conversation panel ----
    active_label = ""
    if state.active_task:
        active_label = f" ({state.active_task})"
    elif total_tasks <= 1 and state.status == "running":
        active_label = " (main)"

    conv_tbl = Table(show_header=False, box=None, expand=True,
                     pad_edge=False, show_edge=False)
    conv_tbl.add_column("ts", style="dim", width=8, no_wrap=True)
    conv_tbl.add_column("kind", width=7, no_wrap=True)
    conv_tbl.add_column("text", overflow="fold")

    conv_items = list(state.conversation)
    for cts, kind, ctext in conv_items[-20:]:
        kind_style = CONV_KIND_STYLE.get(kind, "")
        kind_label = {
            "thinking": "think",
            "text": "text",
            "tool_use": "tool",
            "tool_result": "result",
        }.get(kind, kind)
        conv_tbl.add_row(
            time.strftime("%H:%M:%S", time.localtime(cts)),
            Text(kind_label, style=kind_style),
            Text(ctext[:300], style=kind_style),
        )

    if not conv_items:
        conv_tbl.add_row("", "", Text("waiting for AI output...", style="dim"))

    # ---- Events panel ----
    ev_tbl = Table(show_header=False, box=None, expand=True,
                   pad_edge=False, show_edge=False)
    ev_tbl.add_column("ts", style="dim", width=8, no_wrap=True)
    ev_tbl.add_column("lvl", width=5, no_wrap=True)
    ev_tbl.add_column("text", overflow="fold")
    for ets, lvl, etext in list(state.events)[-8:]:
        ev_tbl.add_row(
            time.strftime("%H:%M:%S", time.localtime(ets)),
            Text(lvl.upper(), style=LEVEL_COLOR.get(lvl, "white")),
            etext[:200],
        )

    # ---- Compose layout ----
    layout = Layout()

    header_size = 4 if (state.swarm_id or state.session_id) else 3
    mid_size = max(4, min(len(state.task_list) + 2, 10))

    mid_layout = Layout()
    mid_layout.split_row(
        Layout(Panel(tasks_tbl, title=tasks_title,
                     border_style="cyan"), ratio=3),
        Layout(Panel(sess_tbl, title="Session",
                     border_style="cyan"), ratio=2),
    )

    layout.split_column(
        Layout(Panel(header, border_style="dim"), size=header_size,
               name="header"),
        Layout(mid_layout, size=mid_size, name="middle"),
        Layout(Panel(conv_tbl, title=f"Conversation{active_label}",
                     border_style="green"), name="conv"),
        Layout(Panel(ev_tbl, title="Log",
                     border_style="dim"), size=10, name="log"),
    )
    return layout


@dataclass
class TaskSeed:
    """Initial task state passed from the registry to pre-populate the TUI."""
    task_id: str
    name: str
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    rc: Optional[int] = None

    @property
    def derived_status(self) -> str:
        if self.finished_at is not None:
            if self.rc is not None:
                return "done" if self.rc == 0 else "failed"
            return "stopped"
        if self.started_at is not None:
            return "running"
        return "queued"


def follow(
    log_path: Path,
    *,
    task_logs: list[tuple[str, Path]] | None = None,
    task_defs: list[TaskSeed] | None = None,
    poll: float = 0.25,
) -> None:
    """Tail NDJSON log(s) and render a live dashboard.

    - `log_path`: primary run log (always tailed)
    - `task_logs`: optional list of (task_name, log_path) for per-task logs
    - `task_defs`: optional list of TaskSeed to pre-populate the task list
      with registry-known state (status, timing)
    """
    state = TuiState(log_path=log_path)

    if task_defs:
        for seed in task_defs:
            state.task_list.append(TaskInfo(
                task_id=seed.task_id,
                name=seed.name,
                status=seed.derived_status,
                started_at=seed.started_at,
                finished_at=seed.finished_at,
                rc=seed.rc,
            ))
        active = next(
            (t for t in state.task_list if t.status == "running"), None
        )
        if active:
            state.active_task = active.name
            state.status = "running"
        else:
            all_terminal = all(
                t.status in ("done", "failed", "stopped", "skipped")
                for t in state.task_list
            )
            if all_terminal and state.task_list:
                any_failed = any(t.status == "failed" for t in state.task_list)
                state.status = "failed" if any_failed else "done"

    # Build the set of files to tail. Primary log always included;
    # per-task logs added if they differ from primary.
    primary = str(log_path.resolve())
    log_sources: list[tuple[Optional[str], Path]] = [(None, log_path)]
    if task_logs:
        for tname, tpath in task_logs:
            if str(tpath.resolve()) != primary:
                log_sources.append((tname, tpath))

    # Per-file byte offsets for incremental tailing.
    offsets: dict[str, int] = {str(p.resolve()): 0 for _, p in log_sources}

    def _tail_all() -> None:
        for _tname, lp in log_sources:
            key = str(lp.resolve())
            if not lp.exists():
                continue
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offsets[key])
                    for line in f:
                        if not line.endswith("\n"):
                            break
                        offsets[key] += len(
                            line.encode("utf-8", errors="replace"))
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        state.ingest(rec)
            except OSError:
                continue

    with Live(_render(state), refresh_per_second=6, screen=False) as live:
        try:
            while True:
                _tail_all()
                live.update(_render(state))
                all_done = all(
                    t.status in ("done", "failed", "skipped")
                    for t in state.task_list
                ) if state.task_list else False
                if state.status in ("done", "failed") or all_done:
                    time.sleep(0.5)
                    _tail_all()
                    live.update(_render(state))
                    return
                time.sleep(poll)
        except KeyboardInterrupt:
            return
