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
    except Exception as e:
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


def check_all(cwd: Path | None = None) -> list[CheckResult]:
    return [check_node(), check_claude(), check_ruflo(cwd)]
