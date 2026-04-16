"""CLI command modules — split from the monolithic cli.py.

Each submodule exports a ``register(app, console)`` function that attaches
its commands to the shared Typer app.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer
    from rich.console import Console


def register_all(app: typer.Typer, console: Console) -> None:
    """Import every command module and register its commands on *app*."""
    from . import dashboard, inspect, lifecycle, monitor, ps, run, setup, tasks

    for mod in (dashboard, inspect, lifecycle, monitor, ps, run, setup, tasks):
        mod.register(app, console)
