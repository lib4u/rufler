"""skills.sh CLI integration — invoke `npx skills add` for each entry.

Separated from install.py so the filesystem-path branch of `copy_custom_skills`
doesn't drag a `subprocess` dependency into tests that only exercise local
paths.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from ..config import SkillsShEntry


def verify_skill_md(skill_dir: Path) -> bool:
    """A skill is discoverable by Claude Code iff it has a SKILL.md at root."""
    return (skill_dir / "SKILL.md").is_file()


def install_skills_sh(
    base_dir: Path,
    entries: list[SkillsShEntry],
    console: Console,
) -> None:
    """Install each skills.sh entry from `skills.custom` via `npx skills add`.

    Invokes the skills.sh CLI (https://skills.sh) from `<base_dir>` so
    installs land in `<base_dir>/.claude/skills/`. We pass `--yes` (non-
    interactive) and `--copy` (no symlinks — safer for repos that move
    around), plus `-a claude-code` so the agent target matches ruflo/rufler.

    For each entry:
      1. Snapshot `<.claude/skills>` contents before install.
      2. Run `npx skills add <source> [--skill <name>] -a <agent> --copy -y`.
      3. Diff the directory listing to find newly created skill dirs.
      4. Verify each new dir contains a `SKILL.md` — warn if missing.

    Non-fatal: any failure (missing `npx`, non-zero exit, missing SKILL.md)
    is surfaced as a warning and the run continues. Use `rufler check` with
    any skills.sh entry in `skills.custom` to catch these upfront.
    """
    npx = shutil.which("npx")
    if not npx:
        console.print(
            "[yellow]skills.sh:[/yellow] `npx` not found on PATH — "
            "cannot install skills_sh entries. Install Node.js 20+."
        )
        return

    target_root = base_dir / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    def _snapshot() -> set[str]:
        if not target_root.is_dir():
            return set()
        return {c.name for c in target_root.iterdir() if c.is_dir()}

    installed: list[str] = []
    failed: list[str] = []
    missing_skill_md: list[str] = []

    for entry in entries:
        before = _snapshot()
        # Two `-y` flags are intentional: the first auto-confirms the npx
        # package download prompt, the second auto-confirms `skills add`'s
        # own interactive confirmation.
        cmd = [npx, "-y", "skills", "add", entry.source, "-a", entry.agent, "-y"]
        if entry.copy:
            cmd.append("--copy")
        if entry.skill:
            cmd.extend(["-s", entry.skill])

        label = entry.source + (f"#{entry.skill}" if entry.skill else "")
        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        try:
            r = subprocess.run(
                cmd,
                cwd=base_dir,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            console.print(f"[red]skills.sh:[/red] {label} timed out after 180s")
            failed.append(label)
            continue
        except OSError as e:
            console.print(f"[red]skills.sh:[/red] {label} failed to launch: {e}")
            failed.append(label)
            continue

        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            console.print(
                f"[red]skills.sh:[/red] {label} exited {r.returncode}"
                + (f"\n[dim]  {' | '.join(tail)}[/dim]" if tail else "")
            )
            failed.append(label)
            continue

        after = _snapshot()
        new_dirs = sorted(after - before)
        if not new_dirs:
            console.print(
                f"[yellow]skills.sh:[/yellow] {label} reported success but no "
                f"new dirs appeared under {target_root} — already installed?"
            )
            continue

        for name in new_dirs:
            if not verify_skill_md(target_root / name):
                missing_skill_md.append(name)
            installed.append(name)

    if installed:
        console.print(
            f"[green]skills.sh:[/green] installed {len(installed)} skill(s): "
            f"{', '.join(installed)}"
        )
    if missing_skill_md:
        console.print(
            f"[yellow]skills.sh:[/yellow] installed dirs with NO SKILL.md — "
            f"Claude Code may not discover them: {', '.join(missing_skill_md)}"
        )
    if failed:
        console.print(
            f"[yellow]skills.sh:[/yellow] {len(failed)} entry/entries failed: "
            f"{', '.join(failed)}"
        )
