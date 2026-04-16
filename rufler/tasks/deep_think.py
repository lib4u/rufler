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

from ..templates import DENY_RULES_PROMPT


DEFAULT_DEEP_THINK_PROMPT = """\
You are a senior software architect performing a **deep, exhaustive \
analysis** of a project before any coding begins. The downstream \
decomposer and executing agents depend on this document — gaps here \
become gaps in the plan, so prefer too much detail over too little.

## Rules
- READ ONLY — do NOT create, edit, or delete any files.
- Explore broadly before narrowing: Glob for structure, Read for \
intent, Grep to trace how symbols actually flow, Bash(ls/tree) to map \
unknown corners. Don't stop at the first answer — verify with a \
second source whenever a claim is load-bearing.
- Cover everything that could affect the plan: package manifests, \
entry points, route definitions, data models, migrations, tests, CI \
configs, env examples, dockerfiles, documentation, feature flags, \
background jobs, third-party integrations.
- Every concrete claim must cite a real file path, ideally with a \
line number (`path/to/file.py:123`). No hand-wavy statements like \
"handles auth somewhere".
- Respond with a single Markdown document (no fenced outer block).
- Depth target: at least 250 lines of substantive content. Short, \
vague analyses are a failure mode — if a section feels thin, re-read \
the code and add specifics.

## Output structure

Each section below must be detailed. The one-line descriptions shown \
are topic hints, NOT length limits.

### 1. Project Overview
Language, framework, runtime version, package manager, build tooling, \
key runtime dependencies (what each does in this project — not just \
names), test framework, linters/formatters, CI provider. Note any \
unusual or load-bearing tooling choices and why they matter.

### 2. Directory Structure
Walk the top two levels. For each important directory: purpose, what \
lives there, how it connects to neighbours. Call out patterns (e.g. \
layered architecture, DDD bounded contexts, feature folders) and \
deviations (stray files, dead dirs, mismatches between declared and \
actual structure).

### 3. Existing Implementation
The core of the document. For every major subsystem — routes, data \
models, services, background jobs, auth, persistence, API clients, \
UI components, tests — describe:
  - What it does, in enough detail that someone could modify it \
without re-reading the code.
  - Where it lives (`path:line` references).
  - How it's wired into the rest of the system (callers, callees, \
shared state, events).
  - Known patterns, conventions, and invariants the code relies on.
Aim for several paragraphs per major subsystem, not bullet lists.

### 4. Gaps & Missing Pieces
Relative to the task below, what is NOT implemented? Be concrete: \
name the specific endpoints, models, migrations, tests, configs, or \
integrations that need to exist but don't. For each gap, note \
whether there's a partial skeleton already (and where) or it's \
greenfield.

### 5. Dependencies & Impact
Which existing files/modules will be touched or broken by the task? \
For each: current behaviour, what will change, blast radius, risk \
(shared state, migrations, breaking API changes, performance cliffs, \
auth/security surface). Flag anything that implies coordinated \
changes across layers.

### 6. Recommended Approach
A concrete, ordered plan. Each step should name: files to create or \
modify, the shape of the change (new function, schema alter, route \
added), tests to write, and the decision points where a human might \
want to pick a direction. Call out ordering constraints (migrations \
before code, shared types before consumers, etc). This is the input \
the decomposer will split into subtasks — make it implementable, not \
aspirational.

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
        body = f"{tpl.rstrip()}\n\n## TASK TO ANALYZE\n\n{main_stripped}"
    else:
        body = tpl.replace("{main}", main_stripped)
    # DENY_RULES_PROMPT is always prepended, even for user-supplied
    # templates, so rufler's infrastructure stays off-limits unconditionally.
    return DENY_RULES_PROMPT + body


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
