from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Package spec used when falling back to npx. Override with env var:
#   RUFLER_RUFLO_SPEC=ruflo@v3alpha   rufler check
#   RUFLER_RUFLO_SPEC=ruflo@3.5.65    rufler start
DEFAULT_RUFLO_SPEC = os.environ.get("RUFLER_RUFLO_SPEC", "ruflo@latest")


@dataclass
class CheckResult:
    name: str
    ok: bool
    version: Optional[str] = None
    hint: Optional[str] = None
    source: Optional[str] = None  # "path" | "local" | "global" | "npx"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return r.returncode, out[0] if out else ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def check_node() -> CheckResult:
    if not shutil.which("node"):
        return CheckResult("node", False, hint="Install Node.js 20+ (https://nodejs.org)")
    code, out = _run(["node", "--version"])
    return CheckResult("node", code == 0, version=out if code == 0 else None)


def check_claude() -> CheckResult:
    if not shutil.which("claude"):
        return CheckResult(
            "claude",
            False,
            hint="Install Claude Code: https://docs.anthropic.com/claude/docs/claude-code",
        )
    code, out = _run(["claude", "--version"])
    return CheckResult("claude", code == 0, version=out if code == 0 else None)


def _find_local_ruflo(start: Path | None = None) -> Path | None:
    """Walk up from cwd looking for ./node_modules/.bin/ruflo."""
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        cand = d / "node_modules" / ".bin" / "ruflo"
        if cand.exists() and os.access(cand, os.X_OK):
            return cand
    return None


def _find_global_ruflo() -> str | None:
    """Ask npm for its global bin dir, then look for a ruflo binary there.
    Works regardless of nvm/volta/asdf/corepack setups."""
    if not shutil.which("npm"):
        return None
    code, out = _run(["npm", "bin", "-g"], timeout=10)
    if code != 0 or not out:
        # newer npm removed `npm bin -g`; try `npm prefix -g` + /bin fallback
        code, out = _run(["npm", "prefix", "-g"], timeout=10)
        if code != 0 or not out:
            return None
        out = str(Path(out) / "bin")
    cand = Path(out) / "ruflo"
    if cand.exists() and os.access(cand, os.X_OK):
        return str(cand)
    return None


def find_ruflo_skills_dir(cwd: Path | None = None) -> Path | None:
    """Locate the bundled `.claude/skills` directory inside the resolved ruflo
    install. Used by rufler to copy extra (standalone) skills that aren't part
    of `ruflo init skills --<pack>` flags. Returns None when ruflo is only
    reachable via `npx` (no stable on-disk path) or when the skills dir can't
    be found."""
    cmd, source = resolve_ruflo_cmd(cwd)
    if source == "npx" or not cmd:
        return None
    bin_path = Path(cmd[0])
    try:
        bin_path = bin_path.resolve()
    except OSError:
        return None
    # The ruflo bin is typically <pkg>/bin/ruflo.js or a symlink pointing there.
    # Walk up looking for `.claude/skills` — correct for both local
    # node_modules and global installs.
    for parent in [bin_path.parent, *bin_path.parents]:
        candidate = parent / ".claude" / "skills"
        if candidate.is_dir():
            return candidate
        # Stop once we hit node_modules boundary to avoid walking too far.
        if parent.name == "node_modules":
            break
    return None


def resolve_ruflo_cmd(cwd: Path | None = None) -> tuple[list[str], str]:
    """Return the command prefix to invoke ruflo, plus a source label.

    Resolution order (first hit wins):
      1. $RUFLER_RUFLO_BIN  — explicit override
      2. ./node_modules/.bin/ruflo (walking up from cwd) — project-local
      3. `ruflo` on PATH — typical global install or nvm-linked
      4. npm's global bin — found via `npm bin -g` / `npm prefix -g`
      5. `npx -y <spec>`  — spec from $RUFLER_RUFLO_SPEC, default ruflo@latest
    """
    override = os.environ.get("RUFLER_RUFLO_BIN")
    if override and Path(override).exists():
        return [override], "override"

    local = _find_local_ruflo(cwd)
    if local:
        return [str(local)], "local"

    path_bin = shutil.which("ruflo")
    if path_bin:
        return [path_bin], "path"

    global_bin = _find_global_ruflo()
    if global_bin:
        return [global_bin], "global"

    if shutil.which("npx"):
        return ["npx", "-y", DEFAULT_RUFLO_SPEC], "npx"

    return [], "missing"


def check_ruflo(cwd: Path | None = None) -> CheckResult:
    cmd, source = resolve_ruflo_cmd(cwd)
    if source == "missing":
        return CheckResult(
            "ruflo",
            False,
            hint=(
                "Not found. Options: `npm i -g ruflo`, add as devDependency, "
                "set $RUFLER_RUFLO_BIN to a binary path, "
                "or install npm/npx so rufler can fall back to npx."
            ),
        )

    # Longer timeout only for npx (may download package on first run)
    timeout = 180 if source == "npx" else 15
    code, out = _run(cmd + ["--version"], timeout=timeout)
    ok = code == 0
    label = f"ruflo ({source})" if source != "path" else "ruflo"
    return CheckResult(
        label,
        ok,
        version=out if ok else None,
        hint=None if ok else f"`{' '.join(cmd)} --version` failed: {out}",
        source=source,
    )


def check_skills_sh_cli(deep: bool = False) -> CheckResult:
    """Report availability of `npx skills` (https://skills.sh).

    skills.sh is **optional** — only needed when `skills.skills_sh` entries
    appear in the yml. We still surface it in `rufler check` so users know
    whether the path is wired up.

    `deep=False` (default): fast presence check — just verifies `npx` is on
    PATH. Used by the always-on `rufler check` table so we don't pay the
    180s first-run `npx -y skills --help` download on every invocation.

    `deep=True`: actually runs `npx -y skills --help`. Triggered when the
    yml declares skills.sh entries — rufler refuses to silently skip an
    install that's about to run.
    """
    if not shutil.which("npx"):
        return CheckResult(
            "skills.sh",
            False,
            hint=(
                "`npx` not found — install Node.js 20+ so rufler can run "
                "`npx skills add <repo>`. See https://skills.sh"
            ),
        )
    if not deep:
        return CheckResult(
            "skills.sh",
            True,
            version="optional — available via npx",
            source="npx",
        )
    code, out = _run(["npx", "-y", "skills", "--help"], timeout=180)
    if code != 0:
        return CheckResult(
            "skills.sh",
            False,
            hint=(
                f"`npx -y skills --help` failed: {out[:200] if out else '(no output)'}. "
                "Check network / npm registry access. See https://skills.sh"
            ),
        )
    return CheckResult("skills.sh", True, version="skills CLI reachable", source="npx")


def check_all(cwd: Path | None = None) -> list[CheckResult]:
    return [check_node(), check_claude(), check_ruflo(cwd), check_skills_sh_cli()]
