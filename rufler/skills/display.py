"""Skills rendering helpers — tables, SKILL.md parsing, custom-entry formatting.

Pure presentation layer: no side effects on the filesystem beyond reading
SKILL.md files. Everything here takes an explicit `console` so tests can
capture output.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from ..config import SkillsShEntry


def read_skill_description(skill_md: Path, limit: int = 120) -> str:
    """Pull `description:` from a SKILL.md YAML frontmatter block; fall back
    to the first non-empty prose line. Best-effort — never raises."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "-"

    def _clip(s: str) -> str:
        s = " ".join(s.split())
        return s[:limit] + ("…" if len(s) > limit else "") if s else "-"

    if text.startswith("---\n") or text.startswith("---\r\n"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        end_match = None
        for marker in ("\n---\n", "\n---\r\n", "\n...\n"):
            idx = body.find(marker)
            if idx != -1 and (end_match is None or idx < end_match):
                end_match = idx
        if end_match is not None:
            fm_text = body[:end_match]
            try:
                fm = yaml.safe_load(fm_text)
            except yaml.YAMLError:
                fm = None
            if isinstance(fm, dict) and fm.get("description"):
                return _clip(str(fm["description"]).strip())

    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and line != "---":
            return _clip(line)
    return "-"


def collect_skills_rows(root: Path) -> list[list[str]]:
    """Scan ``root`` for skill subdirectories and return table rows.

    Each row is ``[name, has_skill_md, description]``.
    """
    rows: list[list[str]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        has_md = skill_md.exists()
        desc = read_skill_description(skill_md) if has_md else "-"
        rows.append([child.name, "✓" if has_md else "✗", desc])
    return rows


def render_skills_table(root: Path, title: str, console: Console) -> int:
    """Print a skills table for every immediate subdirectory of `root`.
    Returns the number of skill directories rendered."""
    rows = collect_skills_rows(root)

    console.rule(f"[bold]{title}[/bold] [dim]{root}[/dim]")
    table = Table(show_lines=False)
    table.add_column("NAME", style="cyan", no_wrap=True)
    table.add_column("SKILL.md", justify="center")
    table.add_column("DESCRIPTION", overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)

    return len(rows)


def fmt_custom_entry(e: "str | SkillsShEntry") -> str:
    """Compact display form for a `skills.custom` entry."""
    if isinstance(e, SkillsShEntry):
        return e.source + (f"#{e.skill}" if e.skill else "")
    return str(e)
