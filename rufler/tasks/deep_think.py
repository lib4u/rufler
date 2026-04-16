"""Deep Think phase — project analysis before task decomposition or execution.

Spawns a read-only ``claude -p`` session that scans the project structure,
reads key files, reasons about the current state, and writes a structured
analysis to disk.  The analysis is then injected into the decomposer prompt
(if ``decompose: true``) or directly into the objective (mono mode).

Activated by ``task.deep_think: true`` in the flow YAML.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


DEFAULT_DEEP_THINK_PROMPT = """\
You are a senior software architect performing a **deep analysis** of a \
project before any coding begins.  Your job is to understand the project \
thoroughly so that the task below can be planned and decomposed correctly.

## Rules
- READ ONLY — do NOT create, edit, or delete any files.
- Use Glob, Grep, Read, Bash(ls/tree) to explore.
- Be thorough: check package manifests, entry points, route definitions, \
models, tests, CI configs, and any documentation.
- Respond with a single Markdown document (no fenced outer block).

## Output structure

### 1. Project Overview
Language/framework, package manager, key dependencies.

### 2. Directory Structure
Top-level layout with one-line descriptions of important directories.

### 3. Existing Implementation
What is already built — endpoints, models, services, tests.  \
Reference concrete file paths.

### 4. Gaps & Missing Pieces
What is NOT implemented relative to the task below.

### 5. Dependencies & Impact
Which existing files/modules will be affected.  \
Flag any risk areas (shared state, migrations, breaking changes).

### 6. Recommended Approach
High-level step-by-step plan for the task, informed by the analysis above.

---

## TASK TO ANALYZE

{main}
"""


def build_deep_think_prompt(
    main_task: str,
    template: Optional[str] = None,
) -> str:
    """Render the deep-think prompt, filling ``{main}``."""
    tpl = template if template is not None else DEFAULT_DEEP_THINK_PROMPT
    main_stripped = main_task.strip()
    if "{main}" not in tpl:
        return f"{tpl.rstrip()}\n\n## TASK TO ANALYZE\n\n{main_stripped}"
    return tpl.replace("{main}", main_stripped)


def deep_think(
    main_task: str,
    output_path: Path,
    *,
    model: str = "sonnet",
    effort: str = "max",
    prompt_template: Optional[str] = None,
    timeout: int = 600,
    allowed_tools: Optional[str] = None,
    budget: Optional[float] = None,
    log_path: Optional[Path] = None,
) -> str:
    """Run a read-only claude session to analyze the project.

    Returns the analysis text.  Also writes it to *output_path*.
    When *log_path* is given, claude's stream-json output is written to
    the NDJSON log in real time so ``rufler follow`` can show progress.
    Raises ``RuntimeError`` on failure.
    """
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` binary not found on PATH — can't run deep think.")

    prompt = build_deep_think_prompt(main_task, prompt_template)

    cmd = [
        claude,
        "-p",
        "--model", model,
        "--effort", effort,
        "--output-format", "text",
        "--dangerously-skip-permissions",
    ]
    if allowed_tools is not None:
        cmd.extend(["--allowedTools", allowed_tools])
    if budget is not None:
        cmd.extend(["--max-budget-usd", str(budget)])
    cmd.append(prompt)

    from ..stream_log import stream_claude

    try:
        res = stream_claude(
            cmd, log_path=log_path, timeout=timeout, phase="deep_think",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"deep_think timed out after {timeout}s: {e}") from e

    if res.returncode != 0:
        raise RuntimeError(
            f"deep_think failed (rc={res.returncode}): {res.stderr[:500]}"
        )

    analysis = res.stdout.strip()
    if not analysis:
        raise RuntimeError("deep_think: claude returned empty output")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(analysis + "\n", encoding="utf-8")

    return analysis
