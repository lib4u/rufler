"""``rufler check`` and ``rufler init`` commands."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..orchestration import print_checks
from ..process import DEFAULT_FLOW_FILE
from ..runner import Runner
from ..templates import SAMPLE_FLOW_YML


def register(app: typer.Typer, console: Console) -> None:

    @app.command()
    def check(
        deep: bool = typer.Option(
            False, "--deep",
            help="Also run `ruflo doctor --fix` for full system diagnostics",
        ),
    ):
        """Verify node, claude code and ruflo are available."""
        ok = print_checks(console)
        if deep:
            console.rule("[bold]ruflo doctor --fix[/bold]")
            Runner(cwd=Path.cwd()).doctor(fix=True)
        raise typer.Exit(0 if ok else 1)

    @app.command()
    def init(
        force: bool = typer.Option(
            False, "--force", help="Overwrite existing rufler_flow.yml",
        ),
    ):
        """Create a sample rufler_flow.yml in the current directory."""
        target = Path.cwd() / DEFAULT_FLOW_FILE
        if target.exists() and not force:
            console.print(
                f"[yellow]{target} already exists[/yellow] (use --force to overwrite)"
            )
            raise typer.Exit(1)
        target.write_text(SAMPLE_FLOW_YML, encoding="utf-8")
        console.print(f"[green]Created {target}[/green]")
        console.print("Edit it, then run [bold cyan]rufler run[/bold cyan]")
