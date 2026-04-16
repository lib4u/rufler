"""``rufler dashboard`` — live Textual TUI with runs, tasks, and log tail.

Three-panel layout:
  ┌─────────────────┬──────────────────────────────┐
  │  RUNS (table)   │  TASKS for selected run       │
  │                 │                                │
  ├─────────────────┴──────────────────────────────┤
  │  LOG TAIL — last N lines of selected run's log  │
  └─────────────────────────────────────────────────┘

Auto-refreshes every few seconds. Arrow keys / click to select a run.
Press ``q`` to quit.
"""
from __future__ import annotations

import time
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from ..process import fmt_age
from ..registry import Registry, RunEntry, _pid_alive
from ..tasks import resolve_tasks_for_entry
from ..tasks.display import TASK_STATUS_COLORS, _task_preview, fmt_duration
from ..tokens import fmt_tokens


# ── Refresh interval (seconds) ──────────────────────────────────────
REFRESH_INTERVAL = 3.0


class RunsTable(DataTable):
    """Left panel — all runs."""
    pass


class TasksTable(DataTable):
    """Right panel — tasks for selected run."""
    pass


class LogPanel(Static):
    """Bottom panel — log tail."""

    log_text: reactive[str] = reactive("", layout=True)

    def watch_log_text(self, value: str) -> None:
        self.update(value)


class DashboardApp(App):
    """Live rufler dashboard."""

    TITLE = "rufler"

    CSS = """
    #top {
        height: 1fr;
    }
    #runs-box {
        width: 1fr;
        min-width: 30;
        border: solid $accent;
    }
    #tasks-box {
        width: 2fr;
        border: solid $accent;
    }
    #log-box {
        height: 12;
        border: solid $accent;
        overflow-y: auto;
    }
    RunsTable {
        height: 1fr;
    }
    TasksTable {
        height: 1fr;
    }
    .box-title {
        dock: top;
        padding: 0 1;
        background: $accent;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    ENABLE_COMMAND_PALETTE = False

    selected_run_id: reactive[str] = reactive("")

    def __init__(self, show_all: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._show_all = show_all
        self._entries: list[RunEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            with Vertical(id="runs-box"):
                yield Static("RUNS", classes="box-title")
                yield RunsTable(id="runs-table", cursor_type="row")
            with Vertical(id="tasks-box"):
                yield Static("TASKS", classes="box-title")
                yield TasksTable(id="tasks-table", cursor_type="row")
        with Vertical(id="log-box"):
            yield Static("LOG", classes="box-title")
            yield LogPanel(id="log-panel")
        yield Footer()

    def on_mount(self) -> None:
        runs = self.query_one("#runs-table", RunsTable)
        runs.add_columns("ID", "PROJECT", "STATUS", "TASKS", "TOKENS", "AGE")

        tasks = self.query_one("#tasks-table", TasksTable)
        tasks.add_columns("ID", "NAME", "STATUS", "DURATION", "TOKENS", "PREVIEW")

        self._do_refresh()
        self.set_interval(REFRESH_INTERVAL, self._do_refresh)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if isinstance(event.data_table, RunsTable) and event.row_key:
            self.selected_run_id = str(event.row_key.value)

    def watch_selected_run_id(self, run_id: str) -> None:
        self._refresh_tasks(run_id)
        self._refresh_log(run_id)

    def action_refresh(self) -> None:
        self._do_refresh()

    @work(thread=True, exclusive=True, group="refresh")
    def _do_refresh(self) -> None:
        reg = Registry()
        self._entries = reg.list_refreshed(include_all=self._show_all)
        self.call_from_thread(self._apply_runs)

    def _apply_runs(self) -> None:
        table = self.query_one("#runs-table", RunsTable)
        prev_selected = self.selected_run_id

        table.clear()
        for e in self._entries:
            real_tasks = [
                t for t in e.tasks
                if t.source not in ("task_report", "final_report")
            ]
            task_count = str(len(real_tasks)) if real_tasks else "-"
            tok = fmt_tokens(e.total_tokens) if e.total_tokens else "-"
            age = fmt_age(e.started_at)
            table.add_row(
                e.id[:8], e.project, e.status, task_count, tok, age,
                key=e.id,
            )

        if prev_selected:
            self.selected_run_id = prev_selected
            self._refresh_tasks(prev_selected)
            self._refresh_log(prev_selected)
        elif self._entries:
            self.selected_run_id = self._entries[0].id

    def _find_entry(self, run_id: str) -> RunEntry | None:
        for e in self._entries:
            if e.id == run_id:
                return e
        return None

    def _refresh_tasks(self, run_id: str) -> None:
        table = self.query_one("#tasks-table", TasksTable)
        table.clear()

        entry = self._find_entry(run_id)
        if not entry or not entry.tasks:
            return

        resolved = resolve_tasks_for_entry(entry)
        for te, st, tok in resolved:
            if te.source in ("task_report", "final_report"):
                continue
            dur = fmt_duration(te.started_at, te.finished_at,
                               running=(st == "running"))
            tok_str = fmt_tokens(tok) if tok else "-"
            preview = _task_preview(te, limit=60)
            table.add_row(te.id, te.name, st, dur, tok_str, preview)

    def _refresh_log(self, run_id: str) -> None:
        panel = self.query_one("#log-panel", LogPanel)
        entry = self._find_entry(run_id)
        if not entry or not entry.log_path:
            panel.log_text = "(no log)"
            return
        lp = Path(entry.log_path)
        if not lp.exists():
            panel.log_text = f"(log not found: {lp})"
            return
        try:
            with open(lp, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = lines[-15:]
            # Escape brackets so Raw JSON isn't parsed as Rich markup
            text = "".join(tail).rstrip().replace("[", r"\[")
            panel.log_text = text
        except OSError as e:
            panel.log_text = f"(error: {e})"


def run_dashboard(show_all: bool = False) -> None:
    """Entry point called by the CLI command."""
    app = DashboardApp(show_all=show_all)
    app.run()
