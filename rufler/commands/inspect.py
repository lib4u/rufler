"""``rufler agents``, ``rufler skills``, ``rufler mcp`` commands."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..checks import find_ruflo_skills_dir
from ..config import FlowConfig
from ..process import DEFAULT_FLOW_FILE, resolve_entry_or_cwd
from ..skills import delete_project_skills, fmt_custom_entry, render_skills_table


def register(app: typer.Typer, console: Console) -> None:

    @app.command("agents")
    def agents_cmd(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id from `rufler ps`. Omit to use current dir.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        full: bool = typer.Option(
            False, "--full",
            help="Print full prompt body instead of a 150-char preview",
        ),
    ):
        """List agents declared in a rufler flow (name, type, role, prompt preview)."""
        entry, cwd, _ = resolve_entry_or_cwd(
            id_prefix, config, console, require_existing_dir=False,
        )
        cfg_path = Path(entry.flow_file) if entry else config.resolve()
        if not cfg_path.exists():
            console.print(
                f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first "
                f"or pass a flow file via [bold]--config[/bold]."
            )
            raise typer.Exit(1)
        try:
            cfg = FlowConfig.load(cfg_path)
        except Exception as e:
            console.print(f"[red]Failed to load config:[/red] {e}")
            raise typer.Exit(1)
        if not cfg.agents:
            console.print(f"[yellow]no agents defined in[/yellow] {cfg_path}")
            raise typer.Exit(0)

        def _preview(text: str, limit: int = 150) -> str:
            text = " ".join(text.split())
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

        if full:
            lines: list[str] = []
            for a in cfg.agents:
                try:
                    body = a.resolved_prompt(cfg.base_dir)
                except Exception as e:
                    body = f"<prompt unavailable: {e}>"
                lines.append(f"{'─' * 50}")
                lines.append(f"{a.name}")
                lines.append(
                    f"  type={a.type}  role={a.role}  seniority={a.seniority}"
                )
                lines.append(body or "(empty)")
                lines.append("")

            console.rule(
                f"[bold]agents[/bold] [dim]{cfg.project.name} — {cfg_path}[/dim]"
            )
            for a in cfg.agents:
                try:
                    body = a.resolved_prompt(cfg.base_dir)
                except Exception as e:
                    body = f"<prompt unavailable: {e}>"
                console.rule(f"[cyan]{a.name}[/cyan]")
                console.print(
                    f"[dim]type=[/dim]{a.type}  [dim]role=[/dim]{a.role}  "
                    f"[dim]seniority=[/dim]{a.seniority}"
                )
                console.print(body or "[dim](empty)[/dim]")
            return

        col_defs = [
            ("NAME", {"no_wrap": True}),
            ("TYPE", {}),
            ("ROLE", {}),
            ("SENIORITY", {}),
            ("DEPENDS ON", {}),
            ("PROMPT (first 150)", {}),
        ]
        rows: list[list[str]] = []
        for a in cfg.agents:
            try:
                body = a.resolved_prompt(cfg.base_dir)
            except Exception as e:
                body = f"<prompt unavailable: {e}>"
            deps = ", ".join(a.depends_on) if a.depends_on else "-"
            rows.append([a.name, a.type, a.role, a.seniority, deps, _preview(body)])

        title = f"agents  {cfg.project.name} — {cfg_path}"
        footer = f"{len(cfg.agents)} agent(s) — use --full to see full prompts"

        console.rule(
            f"[bold]agents[/bold] [dim]{cfg.project.name} — {cfg_path}[/dim]"
        )
        table = Table(show_lines=False)
        table.add_column("NAME", style="cyan", no_wrap=True)
        table.add_column("TYPE")
        table.add_column("ROLE")
        table.add_column("SENIORITY")
        table.add_column("DEPENDS ON")
        table.add_column("PROMPT (first 150)", overflow="fold")
        for row in rows:
            table.add_row(*row)
        console.print(table)
        console.print(f"[dim]{footer}[/dim]")

    @app.command("skills")
    def skills_cmd(
        id_prefix: Optional[str] = typer.Argument(
            None, metavar="[ID]",
            help="Run id from `rufler ps`. Omit to use current dir.",
        ),
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        available: bool = typer.Option(
            False, "--available",
            help="List skills bundled inside ruflo instead of project skills",
        ),
        delete: bool = typer.Option(
            False, "--delete",
            help="Delete every non-symlinked skill dir under <project>/.claude/skills/.",
        ),
        yes: bool = typer.Option(
            False, "--yes", "-y",
            help="Skip the confirmation prompt for --delete.",
        ),
    ):
        """List skills installed under `<project>/.claude/skills/` plus the skills
        configuration declared in rufler_flow.yml. Use --available to see what's
        shipped inside ruflo's source tree (useful for picking `extra:` names).
        Use --delete to wipe installed skills (symlinks are preserved)."""
        entry, _cwd, _ = resolve_entry_or_cwd(
            id_prefix, config, console, require_existing_dir=False,
        )
        cfg_path = Path(entry.flow_file) if entry else config.resolve()
        base_dir = Path(entry.base_dir) if entry else cfg_path.parent

        if delete:
            delete_project_skills(base_dir, yes, console)
            return

        if available:
            src = find_ruflo_skills_dir(base_dir)
            if src is None:
                console.print(
                    "[yellow]cannot locate ruflo's bundled .claude/skills[/yellow] "
                    "(likely running via npx). Install ruflo locally or globally."
                )
                raise typer.Exit(1)
            count = render_skills_table(src, title="available skills", console=console)
            console.print(
                f"[dim]{count} skill dir(s) — add to `skills.extra` in rufler_flow.yml[/dim]"
            )
            return

        if cfg_path.exists():
            try:
                cfg = FlowConfig.load(cfg_path)
                s = cfg.skills
                pieces = [f"enabled={s.enabled}", f"clean={s.clean}", f"all={s.all}"]
                if s.packs:
                    pieces.append(f"packs={s.packs}")
                if s.extra:
                    pieces.append(f"extra={s.extra}")
                if s.custom:
                    pieces.append(f"custom={[fmt_custom_entry(e) for e in s.custom]}")
                suffix = (
                    " [dim](disabled — `rufler run` will skip install)[/dim]"
                    if not s.enabled else ""
                )
                console.print(
                    f"[dim]config ({cfg_path.name}):[/dim] {'  '.join(pieces)}{suffix}"
                )
            except Exception as e:
                console.print(f"[yellow]flow config unreadable:[/yellow] {e}")
        else:
            console.print(
                f"[dim]no flow file at {cfg_path} — showing installed dirs only[/dim]"
            )

        skills_dir = base_dir / ".claude" / "skills"
        if not skills_dir.is_dir():
            console.print(
                f"[yellow]no skills installed[/yellow] at {skills_dir}. "
                f"Run [bold]rufler run[/bold] or add skills manually."
            )
            raise typer.Exit(0)

        count = render_skills_table(
            skills_dir, title=f"skills — {base_dir.name}", console=console,
        )
        if count == 0:
            console.print(f"[yellow]{skills_dir} is empty[/yellow]")
        else:
            console.print(
                f"[dim]{count} skill(s) in {skills_dir} — "
                f"use [bold]rufler skills --available[/bold] to see what ruflo ships[/dim]"
            )

    @app.command("mcp")
    def mcp_cmd(
        config: Path = typer.Option(
            Path(DEFAULT_FLOW_FILE), "--config", "-c",
            help="Path to rufler_flow.yml",
        ),
        active: bool = typer.Option(
            False, "--active",
            help="Show MCP servers actually registered in ~/.claude.json for this project",
        ),
    ):
        """List MCP servers declared in rufler_flow.yml or registered with Claude Code."""
        if active:
            import json as _json
            claude_json = Path.home() / ".claude.json"
            if not claude_json.exists():
                console.print("[yellow]~/.claude.json not found[/yellow]")
                raise typer.Exit(0)
            try:
                data = _json.loads(claude_json.read_text(encoding="utf-8"))
            except Exception as e:
                console.print(f"[red]failed to read ~/.claude.json:[/red] {e}")
                raise typer.Exit(1)
            cwd_resolved = str(Path.cwd().resolve())
            projects = data.get("projects") or {}
            proj = projects.get(cwd_resolved) or {}
            servers = proj.get("mcpServers") or {}
            if not servers:
                console.print(
                    f"[yellow]no MCP servers registered[/yellow] for {cwd_resolved}"
                )
                raise typer.Exit(0)

            col_defs = [
                ("NAME", {"no_wrap": True}),
                ("TYPE", {}),
                ("COMMAND / URL", {}),
                ("ARGS", {}),
            ]
            rows: list[list[str]] = []
            for name, srv_cfg in servers.items():
                transport = srv_cfg.get("type", "stdio")
                if transport == "stdio":
                    cmd_or_url = srv_cfg.get("command", "-")
                    args = " ".join(str(a) for a in srv_cfg.get("args", []))
                else:
                    cmd_or_url = srv_cfg.get("url", "-")
                    args = ""
                rows.append([name, transport, cmd_or_url, args])

            title = f"active MCP servers — {cwd_resolved}"
            footer = f"{len(servers)} server(s)"

            table = Table(title=title, show_lines=False)
            table.add_column("NAME", style="cyan", no_wrap=True)
            table.add_column("TYPE", style="dim")
            table.add_column("COMMAND / URL")
            table.add_column("ARGS", overflow="fold", style="dim")
            for row in rows:
                table.add_row(*row)
            console.print(table)
            console.print(f"[dim]{footer}[/dim]")
            return

        cfg_path = config.resolve()
        if not cfg_path.exists():
            console.print(
                f"[red]{cfg_path} not found.[/red] Run [bold]rufler init[/bold] first."
            )
            raise typer.Exit(1)
        try:
            cfg = FlowConfig.load(cfg_path)
        except Exception as e:
            console.print(f"[red]Failed to load config:[/red] {e}")
            raise typer.Exit(1)

        servers_list = cfg.mcp.servers
        if not servers_list:
            console.print(
                "[yellow]no MCP servers declared[/yellow] in "
                f"[cyan]{cfg_path.name}[/cyan]\n"
                "[dim]add an `mcp.servers` section to your flow yml[/dim]"
            )
            raise typer.Exit(0)

        col_defs = [
            ("NAME", {"no_wrap": True}),
            ("TRANSPORT", {}),
            ("COMMAND / URL", {}),
            ("ARGS", {}),
            ("ENV", {}),
        ]
        rows = []
        for s in servers_list:
            if s.transport == "stdio":
                cmd_or_url = s.command
                args = " ".join(s.args)
            else:
                cmd_or_url = s.url
                args = ""
            env_str = (
                " ".join(f"{k}={v}" for k, v in s.env.items()) if s.env else ""
            )
            rows.append([s.name, s.transport, cmd_or_url, args, env_str])

        title = f"MCP servers — {cfg_path.name}"
        footer = f"{len(servers_list)} server(s) — use --active to see what's registered with claude"

        table = Table(title=title, show_lines=False)
        table.add_column("NAME", style="cyan", no_wrap=True)
        table.add_column("TRANSPORT", style="dim")
        table.add_column("COMMAND / URL")
        table.add_column("ARGS", overflow="fold", style="dim")
        table.add_column("ENV", overflow="fold", style="dim")
        for row in rows:
            table.add_row(*row)
        console.print(table)
        console.print(f"[dim]{footer}[/dim]")
