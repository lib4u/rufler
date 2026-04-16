"""Reusable Textual table viewer for one-shot rufler commands.

Renders a DataTable with optional title and footer. Exits immediately
on ``q`` or ``Ctrl+C`` — no confirmation dialog.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static


class TableApp(App):
    """Generic full-screen table viewer. Exits on q or Ctrl+C."""

    TITLE = "rufler"

    CSS = """
    #title-bar {
        height: 1;
        padding: 0 1;
        background: $surface;
        text-style: bold;
    }
    #footer-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #main-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        columns: list[tuple[str, dict]],
        rows: list[list[str]],
        title: str = "",
        footer: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._columns = columns
        self._rows = rows
        self._title = title
        self._footer = footer

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        if self._title:
            yield Static(self._title, id="title-bar")
        yield DataTable(id="main-table", cursor_type="row")
        if self._footer:
            yield Static(self._footer, id="footer-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#main-table", DataTable)
        # DataTable.add_column only accepts: width, key, default.
        # Silently drop Rich-style hints (no_wrap, justify, style, etc.)
        _valid_keys = {"width", "key", "default"}
        for name, opts in self._columns:
            filtered = {k: v for k, v in opts.items() if k in _valid_keys}
            table.add_column(name, **filtered)
        for row in self._rows:
            table.add_row(*row)


class DetailApp(App):
    """Full-screen detail viewer for key-value displays. Exits on q or Ctrl+C."""

    TITLE = "rufler"

    CSS = """
    #title-bar {
        height: 1;
        padding: 0 1;
        background: $surface;
        text-style: bold;
    }
    #detail-body {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    ENABLE_COMMAND_PALETTE = False

    def __init__(self, *, title: str = "", body: str = "", **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        if self._title:
            yield Static(self._title, id="title-bar")
        yield Static(self._body, id="detail-body")
        yield Footer()


def show_table(
    *,
    columns: list[tuple[str, dict]],
    rows: list[list[str]],
    title: str = "",
    footer: str = "",
) -> None:
    """One-shot: render a table and block until user quits."""
    app = TableApp(columns=columns, rows=rows, title=title, footer=footer)
    app.run()


def show_detail(*, title: str = "", body: str = "") -> None:
    """One-shot: render a detail view and block until user quits."""
    app = DetailApp(title=title, body=body)
    app.run()
