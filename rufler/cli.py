"""Rufler CLI — thin entrypoint that delegates to command modules.

All command implementations live in ``rufler.commands.*`` submodules.
This file creates the Typer app, registers the ``--version`` callback,
and calls ``register_all`` to wire up every command.
"""
from __future__ import annotations

import typer
from rich.console import Console

from . import __version__
from .commands import register_all

# Re-export STATUS_COLORS from its new home so existing code that does
# ``from rufler.cli import STATUS_COLORS`` keeps working.
from .commands.ps import STATUS_COLORS  # noqa: F401


def _version_callback(value: bool) -> None:
    if value:
        print(f"rufler {__version__}")
        raise typer.Exit()


app = typer.Typer(
    help="rufler — one-command wrapper around ruflo for AI agent orchestration.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback,
        is_eager=True, help="Show version and exit.",
    ),
) -> None:
    """rufler — one-command wrapper around ruflo for AI agent orchestration."""


register_all(app, console)


if __name__ == "__main__":
    app()
