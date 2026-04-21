"""Judge agent — evaluates whether an iteration produced a good-enough result.

Runs a read-only ``claude -p`` session that reads the project's current
state plus all accumulated per-iteration reports and decides whether the
iterative-refinement loop should stop early. Emits structured JSON so
``run_cmd`` can act on a clean verdict rather than fuzzy-match on prose.

Activated by ``task.iteration_judge: true`` in the flow YAML. Disabled
iterations always run ``task.iterations`` passes unconditionally.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..templates import DENY_RULES_PROMPT


DEFAULT_JUDGE_PROMPT = """\
# OUTPUT CONTRACT — READ FIRST

You MUST return exactly one JSON object, and nothing else. No prose, no \
markdown fences, no "here is my verdict". The very first character of \
your response must be an opening brace and the last a closing brace. \
Downstream code does a strict JSON parse; any deviation drops your \
verdict and the loop continues by default.

Required shape:

    {
      "verdict": "done" | "continue",
      "score": <number between 0.0 and 1.0>,
      "reasoning": "2-6 sentences citing specific files / tests / gaps",
      "remaining_work": "short list of what is still missing (empty string if none)"
    }

Scoring rubric — be strict, not charitable:
- 1.0 = all acceptance criteria met, tests pass, no obvious gaps
- 0.85-0.99 = core done, polish/edge cases remain
- 0.50-0.84 = major functionality present but incomplete or buggy
- < 0.50 = substantial work still required

`verdict` MUST be `"done"` when score >= {threshold}, else `"continue"`.
Do not cheat: if the reports claim a feature works but the code doesn't \
exist, score LOW and explain in `reasoning`.

---

You are a senior reviewer judging whether the iterative-refinement loop \
for project **{project}** can stop. You have read-only access to the \
repository. This is the end of **iteration {iter_num} of {total_iters}**.

## Your process
1. Read the ORIGINAL TASK below — that is the success target.
2. Glob/Read the codebase to inspect current state (structure, key \
entry points, tests, configs).
3. Read every accumulated per-iteration report below to understand \
what each pass claimed to build.
4. Cross-check: do the report claims match what you actually see in the \
repo? Flag any drift.
5. Identify remaining gaps relative to the ORIGINAL TASK.
6. Emit the JSON verdict.

Do NOT edit files. Do NOT call Write / Edit / MultiEdit / NotebookEdit. \
Use only Read / Glob / Grep / Bash(ls/tree/cat for read-only inspection).

---

## ORIGINAL TASK

{main_task}

---

## ACCUMULATED REPORTS (all iterations so far, oldest first)

{accumulated_reports}

---

Emit the JSON verdict now. One object. Nothing else.
"""


@dataclass
class JudgeResult:
    """Structured outcome of one judge run."""
    verdict: str  # "done" | "continue"
    score: float  # 0.0 - 1.0
    reasoning: str
    remaining_work: str
    raw_output: str  # claude's unparsed stdout (for debugging / judge.md)
    parse_error: Optional[str] = None  # non-None => we fell back to "continue"

    @property
    def should_stop(self) -> bool:
        return self.verdict == "done"


def build_judge_prompt(
    *,
    main_task: str,
    project: str,
    iter_num: int,
    total_iters: int,
    threshold: float,
    accumulated_reports: str,
    template: Optional[str] = None,
) -> str:
    """Render the judge prompt. DENY_RULES is prepended unconditionally."""
    tpl = template if template is not None else DEFAULT_JUDGE_PROMPT
    # str.replace not str.format — user reports may contain `{` / `}`.
    # We DO use .format() here only when no template override is given,
    # because DEFAULT_JUDGE_PROMPT is rufler-controlled and safe; but to
    # keep both branches uniform we normalize by .replace.
    filled = (
        tpl.replace("{main_task}", main_task.strip())
           .replace("{project}", project)
           .replace("{iter_num}", str(iter_num))
           .replace("{total_iters}", str(total_iters))
           .replace("{threshold}", f"{threshold:.2f}")
           .replace("{accumulated_reports}", accumulated_reports.strip() or "(no prior reports — this is iteration 1)")
    )
    return DENY_RULES_PROMPT + filled


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(raw: str, threshold: float) -> JudgeResult:
    """Extract the JSON verdict from claude's stdout. Returns a
    "continue" result with *parse_error* set if extraction fails — we
    never stop the loop on a parse error, to avoid premature exit on a
    judge that merely output prose."""
    stripped = raw.strip()
    if not stripped:
        return JudgeResult(
            verdict="continue",
            score=0.0,
            reasoning="(judge returned empty output)",
            remaining_work="",
            raw_output=raw,
            parse_error="empty output",
        )
    # Prefer the whole thing first (strict contract); fall back to first
    # {...} span if claude wrapped the JSON in prose.
    candidates: list[str] = []
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    m = _JSON_OBJECT_RE.search(stripped)
    if m and m.group(0) not in candidates:
        candidates.append(m.group(0))

    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception as e:
            last_err = e
            continue
        if not isinstance(data, dict):
            last_err = ValueError(f"judge JSON not an object: {type(data).__name__}")
            continue
        verdict_raw = str(data.get("verdict") or "").strip().lower()
        if verdict_raw not in ("done", "continue"):
            # Accept a missing/garbled verdict but force it to match the
            # score vs threshold comparison, so a numeric signal survives.
            verdict_raw = ""
        try:
            score = float(data.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        reasoning = str(data.get("reasoning") or "").strip()
        remaining = str(data.get("remaining_work") or "").strip()
        # If verdict is missing/garbled, derive from score vs threshold.
        if not verdict_raw:
            verdict_raw = "done" if score >= threshold else "continue"
        return JudgeResult(
            verdict=verdict_raw,
            score=score,
            reasoning=reasoning,
            remaining_work=remaining,
            raw_output=raw,
        )
    return JudgeResult(
        verdict="continue",
        score=0.0,
        reasoning="(judge output could not be parsed as JSON)",
        remaining_work="",
        raw_output=raw,
        parse_error=str(last_err) if last_err else "no JSON object found",
    )


def judge_iteration(
    *,
    main_task: str,
    project: str,
    iter_num: int,
    total_iters: int,
    threshold: float,
    accumulated_reports: str,
    output_path: Path,
    model: str = "opus",
    effort: str = "max",
    timeout: int = 600,
    prompt_template: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> JudgeResult:
    """Run the judge, write its markdown record to *output_path*, return
    the structured verdict.

    On any failure (missing claude, subprocess error, timeout, parse
    error), returns a JudgeResult with verdict="continue" and an
    informative parse_error — i.e. the loop keeps going. We never stop
    the loop on a judge failure because "fail open and keep refining"
    is the safer default than "fail closed and exit thinking we're
    done".
    """
    claude = shutil.which("claude")
    if not claude:
        return JudgeResult(
            verdict="continue",
            score=0.0,
            reasoning="(judge skipped: `claude` binary not found on PATH)",
            remaining_work="",
            raw_output="",
            parse_error="claude binary missing",
        )

    prompt = build_judge_prompt(
        main_task=main_task,
        project=project,
        iter_num=iter_num,
        total_iters=total_iters,
        threshold=threshold,
        accumulated_reports=accumulated_reports,
        template=prompt_template,
    )

    cmd = [
        claude,
        "-p",
        "--model", model,
        "--effort", effort,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        prompt,
    ]

    from ..stream_log import stream_claude

    try:
        res = stream_claude(
            cmd, log_path=log_path, timeout=timeout, phase="judge",
        )
    except subprocess.TimeoutExpired as e:
        return JudgeResult(
            verdict="continue",
            score=0.0,
            reasoning=f"(judge timed out after {timeout}s)",
            remaining_work="",
            raw_output="",
            parse_error=f"timeout: {e}",
        )
    except Exception as e:
        return JudgeResult(
            verdict="continue",
            score=0.0,
            reasoning=f"(judge subprocess error: {e})",
            remaining_work="",
            raw_output="",
            parse_error=str(e),
        )

    if res.returncode != 0:
        return JudgeResult(
            verdict="continue",
            score=0.0,
            reasoning=f"(judge claude rc={res.returncode})",
            remaining_work="",
            raw_output=res.stdout or "",
            parse_error=f"rc={res.returncode}: {(res.stderr or '')[:200]}",
        )

    result = _parse_verdict(res.stdout, threshold)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Judge — iteration {iter_num} of {total_iters}",
        "",
        f"- **verdict:** `{result.verdict}`",
        f"- **score:** `{result.score:.3f}`  (threshold `{threshold:.2f}`)",
        f"- **should_stop:** `{result.should_stop}`",
    ]
    if result.parse_error:
        lines.append(f"- **parse_error:** `{result.parse_error}`")
    lines.append("")
    lines.append("## Reasoning")
    lines.append(result.reasoning or "(none)")
    lines.append("")
    lines.append("## Remaining work")
    lines.append(result.remaining_work or "(none)")
    lines.append("")
    lines.append("## Raw output")
    lines.append("```")
    lines.append(result.raw_output or "(empty)")
    lines.append("```")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return result
