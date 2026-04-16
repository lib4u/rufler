"""Task chaining — pass compressed retrospective from completed tasks into
the next task's prompt so each new ``claude -p`` session has context about
what happened before.

Activated by ``task.chain: true`` in the flow YAML. Per-task override via
``chain:`` on individual group items.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ChainedTask:
    """Compressed snapshot of a completed task, ready for prompt injection."""
    name: str
    slot: int
    total: int
    body_compressed: str
    report_compressed: str
    rc: int


def compress_task_context(text: str, max_tokens: int = 2000) -> str:
    """Deterministic text compression for prompt injection.

    Strips markdown decoration, collapses whitespace, and truncates to
    *max_tokens* words (cheap proxy for real token count — roughly 1:1
    for English, ~0.5:1 for code, good enough for budget control).
    """
    if not text:
        return ""

    s = text

    # Strip HTML tags.
    s = re.sub(r"<[^>]+>", "", s)

    # Remove horizontal rules (---, ===, ***).
    s = re.sub(r"^[\-=\*]{3,}\s*$", "", s, flags=re.MULTILINE)

    # Flatten fenced code blocks: ```lang\n...\n``` → [code: first line…]
    def _code_summary(m: re.Match) -> str:
        lang = m.group(1) or ""
        body = m.group(2).strip()
        first = body.split("\n", 1)[0][:120]
        tag = f"code:{lang}" if lang else "code"
        return f"[{tag}: {first}…]"

    s = re.sub(
        r"```(\w*)\n(.*?)```",
        _code_summary,
        s,
        flags=re.DOTALL,
    )

    # Downgrade markdown headers to compact form: ## Foo → [Foo]
    s = re.sub(r"^#{1,6}\s+(.+)$", r"[\1]", s, flags=re.MULTILINE)

    # Remove bold/italic markers.
    s = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", s)
    s = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", s)

    # Collapse multiple blank lines into one.
    s = re.sub(r"\n{3,}", "\n\n", s)

    # Collapse runs of spaces/tabs (but keep newlines).
    s = re.sub(r"[^\S\n]+", " ", s)

    # Strip leading/trailing whitespace per line and globally.
    s = "\n".join(line.strip() for line in s.splitlines())
    s = s.strip()

    # Truncate to max_tokens words.
    words = s.split()
    if len(words) > max_tokens:
        s = " ".join(words[:max_tokens]) + " [… truncated]"

    return s


def build_retrospective(history: list[ChainedTask]) -> str:
    """Render the PREVIOUS TASK RETROSPECTIVE prompt section."""
    if not history:
        return ""

    lines: list[str] = []
    lines.append("# PREVIOUS TASK RETROSPECTIVE (compressed, read-only context)")
    lines.append(
        "The tasks below ran in earlier sessions. Use this context to "
        "understand what was already done — do NOT redo completed work."
    )
    lines.append("")

    for ct in history:
        status = "completed" if ct.rc == 0 else f"failed (rc={ct.rc})"
        lines.append(f"## [{ct.slot}/{ct.total}] {ct.name} — {status}")
        if ct.body_compressed:
            lines.append(ct.body_compressed)
        if ct.report_compressed:
            lines.append("")
            lines.append(f"### Report ({ct.name}):")
            lines.append(ct.report_compressed)
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def collect_chain_entry(
    name: str,
    slot: int,
    total: int,
    body: str,
    report_path: Optional[Path],
    rc: int,
    max_tokens: int = 2000,
) -> ChainedTask:
    """Compress a completed task + its report into a ChainedTask."""
    report_text = ""
    if report_path and report_path.exists():
        try:
            report_text = report_path.read_text(encoding="utf-8")
        except OSError:
            pass

    report_budget = min(max_tokens // 3, max(max_tokens - 200, 0))
    body_budget = max(max_tokens - report_budget, 1)

    return ChainedTask(
        name=name,
        slot=slot,
        total=total,
        body_compressed=compress_task_context(body, body_budget),
        report_compressed=compress_task_context(report_text, report_budget),
        rc=rc,
    )


def resolve_chain_flag(
    task_spec: object,
    item_chain: Optional[bool],
) -> bool:
    """Determine effective chain flag: per-item wins, else global default."""
    if item_chain is not None:
        return item_chain
    return getattr(task_spec, "chain", False)
