"""Token usage accounting for rufler runs.

Parses the NDJSON run log produced by `rufler.logwriter` (which wraps
`claude -p --output-format stream-json`) and extracts cumulative token
counts per Claude `assistant` event.

Used by:
- `rufler tokens [ID]` — show per-run / per-project / grand totals
- `rufler ps` — TOKENS column on the run list
- `rufler projects` — TOTAL TOKENS column on the project rollup
- `Registry.recompute_tokens(entry)` — re-scan logs into the entry on update
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class TokenUsage:
    """Sum of Anthropic API token usage across all assistant turns in a log."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read
            + self.cache_creation
        )

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_creation += other.cache_creation

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TokenUsage":
        return cls(
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
            cache_read=int(d.get("cache_read") or 0),
            cache_creation=int(d.get("cache_creation") or 0),
        )


def parse_log_range(
    log_path: Path,
    *,
    start_offset: int = 0,
    end_offset: Optional[int] = None,
) -> TokenUsage:
    """Sum token usage within a byte range `[start_offset, end_offset)`.

    Used by `rufler tasks` to attribute tokens to a specific task's slice of
    a shared sequential log. When both offsets are None/0 the entire file is
    scanned. Tolerates partial lines, malformed JSON, and missing fields.

    Claude stream-json emits multiple `assistant` events per turn (one per
    content block: text, thinking, tool_use). They share the same
    `message.id` and carry identical `usage` — we deduplicate by keeping
    only the last event per message id.

    Additionally, `input_tokens` / `output_tokens` are per-turn deltas,
    but `cache_read_input_tokens` / `cache_creation_input_tokens` are
    cumulative across the session. We sum the former and take the max
    of the latter.
    """
    out = TokenUsage()
    if not log_path or not log_path.exists():
        return out
    try:
        # First pass: deduplicate by message.id (last event wins).
        by_mid: dict[str, dict] = {}
        mid_order: list[str] = []
        with open(log_path, "rb") as fb:
            if start_offset and start_offset > 0:
                fb.seek(start_offset)
                fb.readline()
            while True:
                pos = fb.tell()
                if end_offset is not None and pos >= end_offset:
                    break
                raw = fb.readline()
                if not raw:
                    break
                ln = raw.decode("utf-8", errors="replace").strip()
                if not ln or not ln.startswith("{"):
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("src") != "claude":
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                mid = msg.get("id") or ""
                usage = msg.get("usage") or {}
                if mid and mid not in by_mid:
                    mid_order.append(mid)
                if mid:
                    by_mid[mid] = usage
                else:
                    # No message id — treat as unique turn.
                    key = f"_anon_{len(mid_order)}"
                    mid_order.append(key)
                    by_mid[key] = usage

        # Second pass: input/output are per-turn deltas (sum them),
        # cache fields are session-cumulative (take max).
        for mid in mid_order:
            usage = by_mid[mid]
            out.input_tokens += int(usage.get("input_tokens") or 0)
            out.output_tokens += int(usage.get("output_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            cc = int(usage.get("cache_creation_input_tokens") or 0)
            if cr > out.cache_read:
                out.cache_read = cr
            if cc > out.cache_creation:
                out.cache_creation = cc
    except OSError:
        pass
    return out


def parse_log(log_path: Path) -> TokenUsage:
    """Walk an NDJSON run log and sum all claude assistant token usage."""
    return parse_log_range(log_path)


def parse_logs(log_paths: Iterable[Path]) -> TokenUsage:
    """Sum token usage across multiple log files (e.g. one per task)."""
    total = TokenUsage()
    seen: set[str] = set()
    for p in log_paths:
        if not p:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        total.add(parse_log(p))
    return total


def fmt_tokens(n: int) -> str:
    """Compact human-readable token count (e.g. 1.2K, 3.4M)."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1_000_000_000:.2f}B"
