"""Report generation after task / run completion.

Spawns a short claude session to analyze what was done and write a
markdown report. Two levels share the same `run_report()` entry point:

- **per-task** (`source="task_report"`) — runs after each task completes
- **final** (`source="final_report"`) — runs after all tasks complete

All logic is self-contained: prompt resolution, claude spawn, markers,
registry update, error handling. `cli.py` makes one-liner calls.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from rich.console import Console

from ..config import ReportSpec
from ..registry import Registry, RunEntry, TaskEntry
from ..task_markers import emit_task_marker

if TYPE_CHECKING:
    from ..runner import Runner
    from ..run_steps import ExecOverrides
    from ..config import FlowConfig

DEFAULT_TASK_REPORT_PROMPT = """\
You are a technical report writer. Task '{task_name}' has just completed \
for project '{project}'.

Analyze the changes made during this task and write a brief completion report.

Include:
1. WHAT WAS DONE — concrete changes, files created or modified
2. DECISIONS — key architectural or design choices made
3. STATUS — is the task fully complete or are there loose ends?

Read shared memory namespace '{ns}' for context (keys: progress, \
decisions, blockers, checkpoint:latest).

Write the report as Markdown to: {report_path}
Keep it concise — 1 page max. Do NOT ask for confirmation.
"""

DEFAULT_FINAL_REPORT_PROMPT = """\
You are a technical report writer. All tasks have completed for project \
'{project}'.

Analyze the project codebase, shared memory namespace '{ns}', and any \
per-task reports in .rufler/reports/ to produce a final completion report.

Include:
1. SUMMARY — what was accomplished in this run (1-2 paragraphs)
2. TASKS COMPLETED — list each task with a one-line result
3. KEY DECISIONS — architectural and design choices
4. FILES CHANGED — list of created/modified files with brief descriptions
5. REMAINING WORK — known TODOs, blockers, or incomplete items

Write the report as Markdown to: {report_path}
Do NOT ask for confirmation.
"""


def _resolve_prompt(
    spec: ReportSpec,
    base_dir: Path,
    *,
    is_final: bool,
    task_name: str,
    project: str,
    ns: str,
    report_path: str,
) -> str:
    """Pick custom or default prompt, fill placeholders."""
    if spec.report_prompt:
        tpl = spec.report_prompt
    elif spec.report_prompt_path:
        p = (base_dir / spec.report_prompt_path).expanduser().resolve()
        if p.exists():
            tpl = p.read_text(encoding="utf-8")
        else:
            tpl = (DEFAULT_FINAL_REPORT_PROMPT if is_final
                   else DEFAULT_TASK_REPORT_PROMPT)
    else:
        tpl = (DEFAULT_FINAL_REPORT_PROMPT if is_final
               else DEFAULT_TASK_REPORT_PROMPT)

    return (
        tpl.replace("{task_name}", task_name)
           .replace("{project}", project)
           .replace("{ns}", ns)
           .replace("{report_path}", report_path)
    )


def run_report(
    cfg: "FlowConfig",
    runner: "Runner",
    reg_entry: RunEntry,
    registry: Registry,
    eff: "ExecOverrides",
    console: Console,
    *,
    spec: ReportSpec,
    task_name: str,
    source: str,
) -> None:
    """Spawn a short claude session to generate a report.

    - `spec`: the ReportSpec (from on_task_complete or on_complete)
    - `task_name`: task name for per-task, or "final" for run-level
    - `source`: "task_report" or "final_report" — stored in TaskEntry
    """
    if not spec.report:
        return

    report_path = spec.report_path.replace("{task}", task_name)
    abs_report = (cfg.base_dir / report_path).resolve()

    is_final = source == "final_report"
    label = "final report" if is_final else f"report ({task_name})"
    console.print(f"[dim]generating {label} → {report_path}[/dim]")

    prompt = _resolve_prompt(
        spec,
        cfg.base_dir,
        is_final=is_final,
        task_name=task_name,
        project=cfg.project.name,
        ns=cfg.memory.namespace,
        report_path=report_path,
    )

    log_suffix = "report" if is_final else f"{task_name}.report"
    log_path = (
        cfg.base_dir
        / eff.log_file.parent
        / f"{eff.log_file.stem}.{log_suffix}{eff.log_file.suffix}"
    ).resolve()

    slot = len(reg_entry.tasks) + 1
    te = TaskEntry(
        name=f"report:{task_name}" if not is_final else "report:final",
        log_path=str(log_path),
        id=f"{reg_entry.id}.r{slot:02d}",
        slot=slot,
        source=source,
        started_at=time.time(),
    )
    reg_entry.tasks.append(te)
    registry.update(reg_entry)

    emit_task_marker(
        log_path, "task_start",
        task_id=te.id, slot=te.slot, name=te.name,
    )

    try:
        abs_report.parent.mkdir(parents=True, exist_ok=True)

        rc = runner.hive_spawn_claude(
            count=1,
            objective=prompt,
            role="specialist",
            non_interactive=True,
            skip_permissions=True,
            log_path=log_path,
            detach=False,
        )
        task_rc = int(rc) if rc else 0
    except Exception as e:
        console.print(f"[yellow]{label} failed:[/yellow] [dim]{e}[/dim]")
        task_rc = 1

    te.finished_at = time.time()
    te.rc = task_rc
    emit_task_marker(
        log_path, "task_end",
        task_id=te.id, slot=te.slot, rc=task_rc,
    )
    registry.update(reg_entry)

    if task_rc == 0:
        console.print(f"[green]{label} written[/green] → {report_path}")
    else:
        console.print(f"[yellow]{label} finished with rc={task_rc}[/yellow]")
