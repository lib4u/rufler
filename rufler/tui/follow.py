"""``rufler follow`` — Textual TUI that tails NDJSON logs in real time.

Four-panel layout:
  ┌──────────────────────────────────────────────────┐
  │  HEADER — status, model, tokens, uptime          │
  ├─────────────────┬────────────────────────────────┤
  │  TASKS          │  SESSION stats                  │
  ├─────────────────┴────────────────────────────────┤
  │  CONVERSATION — AI text, thinking, tool_use       │
  ├──────────────────────────────────────────────────┤
  │  SYSTEM LOG — supervisor events                   │
  └──────────────────────────────────────────────────┘

Reuses :class:`~rufler.follow.TuiState` for all NDJSON parsing logic.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from rich.text import Text as RichText
from textual.widgets import DataTable, Footer, Header, Static, RichLog

from ..follow import (
    CONV_KIND_ICON,
    CONV_KIND_STYLE,
    LEVEL_COLOR,
    STATUS_ICON,
    STATUS_STYLE,
    TaskInfo,
    TaskSeed,
    TuiState,
    _fmt_dur,
    _stringify,
)
from ..tokens import fmt_tokens


POLL_INTERVAL = 0.3


class HeaderBar(Static):
    """Top bar with status, model, tokens, uptime."""
    pass


class TasksPanel(DataTable):
    """Tasks list with status icons."""
    pass


class SessionPanel(Static):
    """Session stats: model, tokens, cache, turns, last tool."""
    pass


class ConversationLog(RichLog):
    """AI conversation stream: text, thinking, tool_use, tool_result."""
    pass


class EventsLog(RichLog):
    """System events from supervisor."""
    pass


class FollowApp(App):
    """Live follow TUI — tails NDJSON logs and renders dashboard."""

    TITLE = "rufler"

    CSS = """
    #header-bar {
        height: 3;
        padding: 0 1;
        background: $surface;
        border: solid $accent;
    }
    #middle {
        height: 1fr;
        max-height: 14;
    }
    #tasks-box {
        width: 3fr;
        border: solid cyan;
    }
    #session-box {
        width: 2fr;
        border: solid cyan;
    }
    #conv-box {
        height: 2fr;
        border: solid green;
    }
    #events-box {
        height: 10;
        border: solid $accent;
    }
    ConversationLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    ConversationLog:focus {
        border: tall green;
    }
    EventsLog {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    EventsLog:focus {
        border: tall $accent;
    }
    TasksPanel {
        height: 1fr;
    }
    .panel-title {
        dock: top;
        padding: 0 1;
        text-style: bold;
    }
    #tasks-title { color: cyan; }
    #session-title { color: cyan; }
    #conv-title { color: green; }
    #events-title { color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
        Binding("c", "focus_conv", "Conversation"),
        Binding("l", "focus_log", "Log"),
        Binding("home", "scroll_top", "Top", show=False),
        Binding("end", "scroll_bottom", "Bottom", show=False),
    ]

    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        log_path: Path,
        task_logs: list[tuple[str, Path]] | None = None,
        task_defs: list[TaskSeed] | None = None,
        run_id: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._state = TuiState(log_path=log_path)
        self._run_id = run_id
        self._log_path = log_path
        self._registry_poll_counter = 0

        # Pre-populate tasks from registry
        if task_defs:
            self._apply_task_defs(task_defs)

        # Build log sources
        primary = str(log_path.resolve())
        self._log_sources: list[tuple[Optional[str], Path]] = [
            (None, log_path),
        ]
        if task_logs:
            for tname, tpath in task_logs:
                if str(tpath.resolve()) != primary:
                    self._log_sources.append((tname, tpath))

        self._offsets: dict[str, int] = {
            str(p.resolve()): 0 for _, p in self._log_sources
        }

        # Track what we've already rendered to avoid duplicates
        self._conv_rendered: int = 0
        self._events_rendered: int = 0

    def _apply_task_defs(self, task_defs: list[TaskSeed]) -> None:
        """Merge task definitions into state, adding new ones only."""
        known_ids = {t.task_id for t in self._state.task_list}
        for seed in task_defs:
            if seed.task_id in known_ids:
                continue
            self._state.task_list.append(TaskInfo(
                task_id=seed.task_id,
                name=seed.name,
                status=seed.derived_status,
                started_at=seed.started_at,
                finished_at=seed.finished_at,
                rc=seed.rc,
            ))
        active = next(
            (t for t in self._state.task_list if t.status == "running"),
            None,
        )
        if active:
            self._state.active_task = active.name
            self._state.status = "running"
        else:
            all_terminal = all(
                t.status in ("done", "failed", "stopped", "skipped")
                for t in self._state.task_list
            )
            if all_terminal and self._state.task_list:
                any_failed = any(
                    t.status == "failed" for t in self._state.task_list
                )
                self._state.status = "failed" if any_failed else "done"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield HeaderBar(id="header-bar")
        with Horizontal(id="middle"):
            with Vertical(id="tasks-box"):
                yield Static("Tasks", id="tasks-title", classes="panel-title")
                yield TasksPanel(id="tasks-panel", cursor_type="none")
            with Vertical(id="session-box"):
                yield Static("Session", id="session-title",
                             classes="panel-title")
                yield SessionPanel(id="session-panel")
        with Vertical(id="conv-box"):
            yield Static("Conversation", id="conv-title",
                         classes="panel-title")
            yield ConversationLog(id="conv-log", wrap=True, max_lines=200)
        with Vertical(id="events-box"):
            yield Static("Log", id="events-title", classes="panel-title")
            yield EventsLog(id="events-log", wrap=True, max_lines=100)
        yield Footer()

    def action_focus_conv(self) -> None:
        self.query_one("#conv-log", ConversationLog).focus()

    def action_focus_log(self) -> None:
        self.query_one("#events-log", EventsLog).focus()

    def action_scroll_top(self) -> None:
        focused = self.focused
        if hasattr(focused, "scroll_home"):
            focused.scroll_home()

    def action_scroll_bottom(self) -> None:
        focused = self.focused
        if hasattr(focused, "scroll_end"):
            focused.scroll_end()

    def on_mount(self) -> None:
        tasks = self.query_one("#tasks-panel", TasksPanel)
        tasks.add_columns("", "NAME", "STATUS", "DUR", "TOKENS")

        self._update_all()
        self.set_interval(POLL_INTERVAL, self._poll_and_update)

    def _tail_all(self) -> None:
        """Read new lines from all log files.

        Uses binary mode so ``self._offsets[key]`` stays a true byte
        position. Text-mode seek with a byte count desyncs the UTF-8
        decoder on any multi-byte content (Cyrillic, emojis in claude
        stream-json), which is why follow eventually stopped picking
        up new log lines mid-run.
        """
        for _tname, lp in self._log_sources:
            key = str(lp.resolve())
            if not lp.exists():
                continue
            try:
                with open(lp, "rb") as f:
                    f.seek(self._offsets[key])
                    for raw in f:
                        if not raw.endswith(b"\n"):
                            break
                        self._offsets[key] += len(raw)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._state.ingest(rec)
            except OSError:
                continue

    def _poll_and_update(self) -> None:
        # Every ~10 polls (~3s) re-check registry for new tasks
        self._registry_poll_counter += 1
        if self._run_id and self._registry_poll_counter % 10 == 0:
            self._refresh_from_registry()
        self._tail_all()
        self._update_all()

    def _refresh_from_registry(self) -> None:
        """Re-read the registry entry to pick up tasks added by decompose."""
        try:
            from ..registry import Registry
            reg = Registry()
            matches = reg.find_ambiguous(self._run_id)
            if not matches:
                return
            entry = matches[0]
            _report_sources = ("task_report", "final_report")
            new_defs = [
                TaskSeed(
                    task_id=te.id,
                    name=te.name,
                    started_at=te.started_at,
                    finished_at=te.finished_at,
                    rc=te.rc,
                )
                for te in entry.tasks
                if te.source not in _report_sources
            ]
            if new_defs:
                self._apply_task_defs(new_defs)

            # Also pick up new log files
            primary = str(self._log_path.resolve())
            known_keys = set(self._offsets.keys())
            for te in entry.tasks:
                if te.source in _report_sources or not te.log_path:
                    continue
                lp = Path(te.log_path)
                key = str(lp.resolve())
                if key != primary and key not in known_keys:
                    self._log_sources.append((te.name, lp))
                    self._offsets[key] = 0
        except Exception:
            pass

    def _update_all(self) -> None:
        self._update_header()
        self._update_tasks()
        self._update_session()
        self._update_conversation()
        self._update_events()

    def _update_header(self) -> None:
        bar = self.query_one("#header-bar", HeaderBar)
        s = self._state
        now = time.time()
        elapsed = int(now - s.started_at)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        uptime = f"{hh:02d}:{mm:02d}:{ss:02d}"

        total_tok = s.input_tokens + s.output_tokens + s.cache_read
        tok_str = fmt_tokens(total_tok)
        model = s.model or "-"

        bar.update(
            f"[bold]rufler follow[/bold]  "
            f"[{self._status_color()}]{s.status}[/{self._status_color()}]  "
            f"[dim]{model}[/dim]  "
            f"[magenta]tokens: {tok_str}[/magenta]  "
            f"[dim]{uptime}[/dim]"
        )

        # Update conversation title with active task
        conv_title = self.query_one("#conv-title", Static)
        active_label = ""
        if s.active_task:
            active_label = f" ({s.active_task})"
        elif len(s.task_list) <= 1 and s.status == "running":
            active_label = " (main)"
        conv_title.update(f"Conversation{active_label}")

    def _status_color(self) -> str:
        return {
            "starting": "yellow",
            "claude-launching": "yellow",
            "claude-running": "cyan",
            "running": "cyan",
            "done": "green",
            "failed": "red",
        }.get(self._state.status, "white")

    def _update_tasks(self) -> None:
        table = self.query_one("#tasks-panel", TasksPanel)
        table.clear()
        now = time.time()

        n_done = sum(1 for t in self._state.task_list if t.status == "done")
        total = len(self._state.task_list)
        title = self.query_one("#tasks-title", Static)
        title.update(f"Tasks  {n_done}/{total}" if total else "Tasks")

        for ti in self._state.task_list:
            icon = STATUS_ICON.get(ti.status, "  ")
            dur = ""
            if ti.started_at:
                end = ti.finished_at or now
                dur = _fmt_dur(end - ti.started_at)
                if ti.status == "running":
                    dur += "…"
            tok = fmt_tokens(ti.input_tokens + ti.output_tokens) if (
                ti.input_tokens + ti.output_tokens) else ""
            table.add_row(icon, ti.name, ti.status, dur, tok)

        if not self._state.task_list:
            table.add_row("", "(no tasks)", "", "", "")

    def _update_session(self) -> None:
        panel = self.query_one("#session-panel", SessionPanel)
        s = self._state
        lines = [
            f"[dim]model[/dim]      {s.model or '-'}",
            f"[dim]tokens[/dim]     [magenta]in={s.input_tokens:,}  out={s.output_tokens:,}[/magenta]",
            f"[dim]cache[/dim]      {s.cache_read:,}",
            f"[dim]turns[/dim]      {s.turns}",
            f"[dim]last tool[/dim]  [bold]{s.last_tool or '-'}[/bold]",
        ]
        if s.last_tool_desc:
            lines.append(f"             [dim]{s.last_tool_desc}[/dim]")
        if s.swarm_id:
            lines.append(f"[dim]swarm[/dim]      {s.swarm_id}")
        if s.workers:
            lines.append(f"[dim]workers[/dim]    {s.workers}")
        panel.update("\n".join(lines))

    def _update_conversation(self) -> None:
        log = self.query_one("#conv-log", ConversationLog)
        conv_items = list(self._state.conversation)
        new_items = conv_items[self._conv_rendered:]
        self._conv_rendered = len(conv_items)

        for cts, kind, ctext in new_items:
            ts_str = time.strftime("%H:%M:%S", time.localtime(cts))
            icon = CONV_KIND_ICON.get(kind, " ")
            style = CONV_KIND_STYLE.get(kind, "")
            # Escape user text so it's not parsed as markup
            safe = ctext[:300].replace("[", r"\[")
            line = RichText.from_markup(
                f"[dim]{ts_str}[/dim]  {icon} [{style}]{safe}[/{style}]"
            )
            log.write(line)

    def _update_events(self) -> None:
        log = self.query_one("#events-log", EventsLog)
        ev_items = list(self._state.events)
        new_items = ev_items[self._events_rendered:]
        self._events_rendered = len(ev_items)

        for ets, lvl, etext in new_items:
            ts_str = time.strftime("%H:%M:%S", time.localtime(ets))
            color = LEVEL_COLOR.get(lvl, "white")
            safe = etext[:200].replace("[", r"\[")
            line = RichText.from_markup(
                f"[dim]{ts_str}[/dim]  [{color}]{lvl.upper():>5s}[/{color}]  {safe}"
            )
            log.write(line)


def run_follow(
    log_path: Path,
    *,
    task_logs: list[tuple[str, Path]] | None = None,
    task_defs: list[TaskSeed] | None = None,
    run_id: str | None = None,
) -> None:
    """Entry point called by the CLI command."""
    app = FollowApp(
        log_path=log_path,
        task_logs=task_logs,
        task_defs=task_defs,
        run_id=run_id,
    )
    app.run()
