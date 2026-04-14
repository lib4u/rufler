"""Interactive TUI that tails a rufler NDJSON log and renders a live
dashboard: session state, task counts, recent events, tool activity.

Reads the log produced by `rufler.logwriter` (one JSON per line).
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Optional

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


LEVEL_COLOR = {
    "error": "bold red",
    "warn": "yellow",
    "ok": "green",
    "info": "cyan",
    "debug": "dim",
}


@dataclass
class TuiState:
    log_path: Path
    started_at: float = field(default_factory=time.time)
    session_id: Optional[str] = None
    model: Optional[str] = None
    swarm_id: Optional[str] = None
    workers: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    tasks_started: int = 0
    tasks_completed: int = 0
    last_tool: Optional[str] = None
    last_tool_desc: Optional[str] = None
    status: str = "starting"
    events: Deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=12))

    def ingest(self, rec: dict) -> None:
        src = rec.get("src")
        t = rec.get("type")

        # ---- rufler supervisor envelope lines ----
        if src == "rufler":
            text = rec.get("text") or ""
            if "log started" in text:
                self.status = "starting"
            elif "log ended" in text:
                if "rc=0" in text:
                    self.status = "done"
                else:
                    self.status = "failed"
            self._push_event(rec, text)
            return

        # ---- ruflo prelude text lines ----
        if src == "ruflo":
            text = rec.get("text") or ""
            if "Swarm ID" in text:
                # "  - Swarm ID: hive-..."
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
            self._push_event(rec, text)
            return

        # ---- claude stream-json ----
        if src == "claude":
            sub = rec.get("subtype")
            if t == "system" and sub == "init":
                self.session_id = rec.get("session_id") or self.session_id
                self.model = rec.get("model") or self.model
                self.status = "running"
                self._push_event(rec, f"session init ({self.model or '-'})")
                return

            if t == "system" and sub == "task_started":
                self.tasks_started += 1
                self._push_event(rec, f"task started: {rec.get('description', '?')}")
                return

            if t == "system" and sub == "task_progress":
                usage = rec.get("usage") or {}
                self.turns = max(self.turns, int(usage.get("tool_uses") or 0))
                desc = rec.get("description") or ""
                last_tool = rec.get("last_tool_name")
                if last_tool:
                    self.last_tool = last_tool
                    self.last_tool_desc = desc
                self._push_event(rec, f"[{last_tool or '...'}] {desc}")
                return

            if t == "system" and sub == "task_updated":
                self._push_event(rec, f"task updated: {rec.get('description', '?')}")
                return

            if t == "system" and sub in ("hook_started", "hook_response"):
                name = rec.get("hook_name") or ""
                self._push_event(rec, f"hook {sub}: {name}", level="debug")
                return

            if t == "assistant":
                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                self.input_tokens += int(usage.get("input_tokens") or 0)
                self.output_tokens += int(usage.get("output_tokens") or 0)
                self.cache_read += int(usage.get("cache_read_input_tokens") or 0)
                content = msg.get("content") or []
                summary = _summarize_assistant(content)
                if summary:
                    self._push_event(rec, summary, level="info")
                return

            if t == "user":
                # tool_result from user message
                msg = rec.get("message") or {}
                content = msg.get("content") or []
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        txt = _stringify(c.get("content"))
                        if txt:
                            self._push_event(rec, f"→ tool_result: {txt[:80]}", level="debug")
                return

            if t == "result":
                ok = rec.get("subtype") == "success" and not rec.get("is_error")
                self.status = "done" if ok else "failed"
                self._push_event(
                    rec,
                    f"RESULT {'✓' if ok else '✗'} {rec.get('result', '')[:80]}",
                    level="ok" if ok else "error",
                )
                return

            if t == "rate_limit_event":
                self._push_event(rec, "rate limited", level="warn")
                return

    def _push_event(self, rec: dict, text: str, level: Optional[str] = None) -> None:
        ts = float(rec.get("ts") or time.time())
        lvl = level or rec.get("level") or "debug"
        self.events.append((ts, lvl, text))


def _summarize_assistant(content) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        ct = c.get("type")
        if ct == "text":
            txt = (c.get("text") or "").strip().replace("\n", " ")
            if txt:
                parts.append(txt[:120])
        elif ct == "thinking":
            th = (c.get("thinking") or "").strip().replace("\n", " ")
            if th:
                parts.append(f"(thinking) {th[:100]}")
        elif ct == "tool_use":
            name = c.get("name") or "?"
            inp = c.get("input") or {}
            hint = ""
            for k in ("file_path", "command", "pattern", "path", "query"):
                if k in inp:
                    hint = str(inp[k])[:60]
                    break
            parts.append(f"tool_use: {name}({hint})" if hint else f"tool_use: {name}")
    return " | ".join(parts)[:200]


def _stringify(x) -> str:
    if isinstance(x, str):
        return x.replace("\n", " ")
    if isinstance(x, list):
        return " ".join(_stringify(i) for i in x)
    if isinstance(x, dict):
        return _stringify(x.get("text"))
    return ""


def _render(state: TuiState) -> Layout:
    elapsed = int(time.time() - state.started_at)
    mm, ss = divmod(elapsed, 60)
    hh, mm = divmod(mm, 60)
    uptime = f"{hh:02d}:{mm:02d}:{ss:02d}"

    status_color = {
        "starting": "yellow",
        "claude-launching": "yellow",
        "claude-running": "cyan",
        "running": "cyan",
        "done": "green",
        "failed": "red",
    }.get(state.status, "white")

    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(
        Text.assemble(
            ("rufler tui  ", "bold"),
            (f"[{state.status}]", status_color),
            ("  ", ""),
            (str(state.log_path), "dim"),
        ),
        Text(f"uptime {uptime}", style="dim"),
    )

    summary = Table(show_header=False, box=None, expand=True, pad_edge=False)
    summary.add_column("k", style="dim", width=14)
    summary.add_column("v")
    summary.add_row("session", state.session_id or "-")
    summary.add_row("model", state.model or "-")
    summary.add_row("swarm", state.swarm_id or "-")
    summary.add_row("workers", str(state.workers))
    summary.add_row(
        "tasks",
        f"{state.tasks_completed}/{state.tasks_started}   [dim]turns {state.turns}[/dim]",
    )
    summary.add_row(
        "tokens",
        f"in={state.input_tokens:,}  out={state.output_tokens:,}  "
        f"[dim]cache_r={state.cache_read:,}[/dim]",
    )
    summary.add_row(
        "last tool",
        f"[bold]{state.last_tool or '-'}[/bold]  "
        f"[dim]{(state.last_tool_desc or '')[:60]}[/dim]",
    )

    events_tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
    events_tbl.add_column("ts", style="dim", width=8)
    events_tbl.add_column("lvl", width=5)
    events_tbl.add_column("text", overflow="fold")
    for ts, lvl, text in list(state.events)[-12:]:
        events_tbl.add_row(
            time.strftime("%H:%M:%S", time.localtime(ts)),
            Text(lvl.upper(), style=LEVEL_COLOR.get(lvl, "white")),
            text[:240],
        )

    layout = Layout()
    layout.split_column(
        Layout(Panel(header, border_style="dim"), size=3),
        Layout(Panel(summary, title="session", border_style="cyan"), size=11),
        Layout(Panel(events_tbl, title="recent events", border_style="cyan")),
    )
    return layout


def follow(log_path: Path, poll: float = 0.25) -> None:
    """Tail the NDJSON log forever and render a live dashboard."""
    state = TuiState(log_path=log_path)
    # Seek to start — replay existing lines so summary is accurate, then follow.
    pos = 0
    with Live(_render(state), refresh_per_second=8, screen=False) as live:
        try:
            while True:
                if log_path.exists():
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        for line in f:
                            if not line.endswith("\n"):
                                # partial line — back off
                                break
                            pos += len(line.encode("utf-8", errors="replace"))
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            state.ingest(rec)
                live.update(_render(state))
                if state.status in ("done", "failed"):
                    # keep the final frame on-screen and exit cleanly
                    time.sleep(0.5)
                    live.update(_render(state))
                    return
                time.sleep(poll)
        except KeyboardInterrupt:
            return
