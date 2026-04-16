"""``rufler dashboard`` command — live Textual TUI."""
from __future__ import annotations

import typer
from rich.console import Console


def register(app: typer.Typer, console: Console) -> None:

    @app.command()
    def dashboard(
        all_runs: bool = typer.Option(
            False, "--all", "-a",
            help="Show all runs, not just currently running",
        ),
    ):
        """Live TUI dashboard with runs, tasks, and log tail.

        Arrow keys to navigate runs. Selected run's tasks and log tail
        update in real-time. Press q to quit, r to force-refresh.
        """
        try:
            from ..tui.dashboard import run_dashboard
        except ImportError:
            console.print(
                "[red]textual is required for the dashboard:[/red]\n"
                "  pip install textual"
            )
            raise typer.Exit(1)
        run_dashboard(show_all=all_runs)
