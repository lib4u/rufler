"""Skill installation — packs, extras, custom paths, skills.sh.

All functions here are pure side-effect routines: they take a console as
an explicit parameter and report progress through it. No module-level
globals. Callers (CLI commands) are expected to pass their own Console.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from ..checks import find_ruflo_skills_dir
from ..config import (
    CLI_FLAG_PACKS,
    MANUAL_COPY_PACKS,
    MANUAL_PACK_PREFIXES,
    FlowConfig,
    SkillsShEntry,
)
from .skills_sh import install_skills_sh

if TYPE_CHECKING:
    from ..runner import Runner


def copy_skill_dir(src: Path, dest: Path) -> str:
    """Copy one skill directory into `.claude/skills/`.

    Returns a status string: "copied", "symlink" (dest was a symlink — kept
    untouched), or "missing_src" (src is not a directory). Callers handle
    reporting and aggregation.
    """
    if not src.is_dir():
        return "missing_src"
    if dest.is_symlink():
        return "symlink"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return "copied"


def install_skills(runner: "Runner", cfg: FlowConfig, console: Console) -> None:
    """Install project-global Claude Code skills into `<base_dir>/.claude/skills/`.

    Proxies CLI_FLAG_PACKS to `ruflo init skills --<pack>` and copies
    MANUAL_COPY_PACKS + `extra` directly from ruflo's bundled source tree.
    Non-fatal: any problem is surfaced as a warning and the run continues."""
    skills = cfg.skills
    if not skills.enabled:
        return
    cli_packs = [p for p in skills.packs if p in CLI_FLAG_PACKS]
    manual_packs = [p for p in skills.packs if p in MANUAL_COPY_PACKS]
    has_declaration = bool(
        skills.all or cli_packs or manual_packs or skills.extra or skills.custom
    )
    if not has_declaration and not skills.clean:
        return

    console.rule("[bold]1b. skills install[/bold]")
    if skills.clean:
        prune_installed_skills(cfg.base_dir, console)
    if not has_declaration:
        return
    if skills.all or cli_packs:
        runner.init_skills(all_packs=skills.all, packs=cli_packs)
    if manual_packs or skills.extra:
        copy_manual_skills(cfg.base_dir, manual_packs, skills.extra, console)
    if skills.custom:
        copy_custom_skills(cfg.base_dir, skills.custom, console)


def prune_installed_skills(base_dir: Path, console: Console) -> None:
    """Delete every non-symlinked skill dir under `<base_dir>/.claude/skills/`.

    Invoked by `install_skills` when `skills.clean=true` so the yml is the
    authoritative source of truth — ruflo init's bundled defaults get wiped
    before rufler reinstalls packs/extra/custom. Symlinks are preserved
    (user-managed)."""
    skills_dir = base_dir / ".claude" / "skills"
    if not skills_dir.is_dir():
        return
    pruned: list[str] = []
    kept_symlinks: list[str] = []
    for child in sorted(skills_dir.iterdir()):
        if child.is_symlink():
            kept_symlinks.append(child.name)
            continue
        if child.is_dir():
            try:
                shutil.rmtree(child)
                pruned.append(child.name)
            except OSError as e:
                console.print(
                    f"[yellow]skills:[/yellow] failed to prune {child.name}: {e}"
                )
    if pruned:
        console.print(
            f"[dim]skills: pruned {len(pruned)} stale dir(s) "
            f"(clean=true) before install[/dim]"
        )
    if kept_symlinks:
        console.print(
            f"[dim]skills: kept symlinks untouched: {', '.join(kept_symlinks)}[/dim]"
        )


def copy_manual_skills(
    base_dir: Path,
    manual_packs: list[str],
    extra: list[str],
    console: Console,
) -> None:
    """Copy MANUAL_COPY_PACKS + explicit `extra` skill dirs from ruflo's
    bundled `.claude/skills` source into `<base_dir>/.claude/skills/`.
    Existing destination dirs are replaced; symlinked destinations are
    preserved (treated as user-managed) and skipped with a warning."""
    src = find_ruflo_skills_dir(base_dir)
    if src is None:
        console.print(
            "[yellow]skills:[/yellow] cannot locate ruflo's bundled "
            ".claude/skills source (likely running via npx). "
            "Install ruflo locally or globally to use `extra`/"
            "flowNexus/browser/dualMode packs."
        )
        return

    target_root = base_dir / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    manual_names: set[str] = set(extra)
    for pack in manual_packs:
        prefixes = MANUAL_PACK_PREFIXES.get(pack, ())
        matched = [
            c.name for c in src.iterdir()
            if c.is_dir() and any(c.name.startswith(pfx) for pfx in prefixes)
        ]
        if not matched:
            console.print(
                f"[yellow]skills:[/yellow] pack '{pack}' matched 0 directories "
                f"in {src} (prefixes tried: {list(prefixes)}). "
                f"Use `rufler skills --available` to see what's actually shipped."
            )
            continue
        manual_names.update(matched)

    copied: list[str] = []
    missing: list[str] = []
    skipped_symlinks: list[str] = []
    for name in sorted(manual_names):
        status = copy_skill_dir(src / name, target_root / name)
        if status == "missing_src":
            missing.append(name)
        elif status == "symlink":
            skipped_symlinks.append(name)
        else:
            copied.append(name)

    if copied:
        console.print(
            f"[green]skills:[/green] installed {len(copied)} skill(s): "
            f"{', '.join(copied)}"
        )
    if skipped_symlinks:
        console.print(
            f"[yellow]skills:[/yellow] kept symlinked dirs untouched: "
            f"{', '.join(skipped_symlinks)}"
        )
    if missing:
        console.print(
            f"[yellow]skills:[/yellow] not found in ruflo source: "
            f"{', '.join(missing)}"
        )


def copy_custom_skills(
    base_dir: Path,
    custom_entries: list[str | SkillsShEntry],
    console: Console,
) -> None:
    """Install every entry under `skills.custom` into `.claude/skills/`.

    Each entry is either a plain string (local path OR skills.sh shorthand)
    or a `SkillsShEntry` (explicit skills.sh install — dict form / pasted
    command). Resolution rules for strings:

      1. Try as a filesystem path (absolute / `~` / relative to the yml's
         base_dir). If the resolved path exists AND is a directory → copy
         it into `<base>/.claude/skills/<basename>`.
      2. Otherwise → treat as a skills.sh shorthand and pass to
         `install_skills_sh` which runs `npx skills add <source>`.

    Non-fatal throughout: missing sources, collisions, and failed installs
    are surfaced as warnings."""
    target_root = base_dir / ".claude" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    path_copied: list[str] = []
    not_dirs: list[str] = []
    skipped_symlinks: list[str] = []
    name_collisions: list[str] = []
    seen_names: set[str] = set()

    sh_entries: list[SkillsShEntry] = []

    for raw in custom_entries:
        if isinstance(raw, SkillsShEntry):
            sh_entries.append(raw)
            continue

        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (base_dir / p).resolve()

        if p.is_dir():
            name = p.name
            if name in seen_names:
                name_collisions.append(name)
                continue
            seen_names.add(name)
            status = copy_skill_dir(p, target_root / name)
            if status == "symlink":
                skipped_symlinks.append(name)
            elif status == "copied":
                path_copied.append(name)
            else:
                console.print(
                    f"[yellow]skills:[/yellow] unexpected copy status "
                    f"'{status}' for {name}"
                )
            continue

        if p.exists() and not p.is_dir():
            not_dirs.append(str(p))
            continue

        sh_entries.append(SkillsShEntry(source=raw))

    if path_copied:
        console.print(
            f"[green]skills:[/green] installed {len(path_copied)} custom skill(s) "
            f"from local paths: {', '.join(path_copied)}"
        )
    if skipped_symlinks:
        console.print(
            f"[yellow]skills:[/yellow] kept symlinked dirs untouched: "
            f"{', '.join(skipped_symlinks)}"
        )
    if name_collisions:
        console.print(
            f"[yellow]skills:[/yellow] duplicate custom skill basenames skipped: "
            f"{', '.join(name_collisions)} "
            f"(each entry must land on a unique .claude/skills/<name>)"
        )
    if not_dirs:
        console.print(
            f"[yellow]skills:[/yellow] custom paths that exist but are not "
            f"directories: {', '.join(not_dirs)}"
        )

    if sh_entries:
        install_skills_sh(base_dir, sh_entries, console)


def delete_project_skills(
    base_dir: Path,
    assume_yes: bool,
    console: Console,
) -> None:
    """Delete every non-symlinked skill dir under `<base_dir>/.claude/skills/`.

    Symlinked entries are preserved (user-managed, same rule as
    `copy_manual_skills`). Prompts for confirmation unless `--yes`."""
    skills_dir = base_dir / ".claude" / "skills"
    if not skills_dir.is_dir():
        console.print(f"[yellow]no skills dir at {skills_dir} — nothing to delete[/yellow]")
        return

    to_delete: list[Path] = []
    kept_symlinks: list[str] = []
    for child in sorted(skills_dir.iterdir()):
        if child.is_symlink():
            kept_symlinks.append(child.name)
            continue
        if child.is_dir():
            to_delete.append(child)

    if not to_delete:
        console.print(f"[dim]{skills_dir} has no removable skill dirs[/dim]")
        if kept_symlinks:
            console.print(
                f"[dim]kept symlinks: {', '.join(kept_symlinks)}[/dim]"
            )
        return

    console.print(
        f"[bold]About to delete {len(to_delete)} skill dir(s)[/bold] "
        f"from [cyan]{skills_dir}[/cyan]:"
    )
    for p in to_delete:
        console.print(f"  - {p.name}")
    if kept_symlinks:
        console.print(
            f"[dim]symlinks preserved: {', '.join(kept_symlinks)}[/dim]"
        )

    if not assume_yes:
        confirm = typer.confirm("Proceed?", default=False)
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(1)

    removed: list[str] = []
    errors: list[str] = []
    for p in to_delete:
        try:
            shutil.rmtree(p)
            removed.append(p.name)
        except OSError as e:
            errors.append(f"{p.name}: {e}")

    if removed:
        console.print(
            f"[green]removed {len(removed)} skill(s):[/green] {', '.join(removed)}"
        )
    if errors:
        console.print("[red]errors:[/red]")
        for e in errors:
            console.print(f"  - {e}")
        raise typer.Exit(1)
