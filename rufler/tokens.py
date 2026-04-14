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
from typing import Iterable


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


def parse_log(log_path: Path) -> TokenUsage:
    """Walk an NDJSON run log and sum token usage from every claude assistant
    record. Tolerates partial lines, malformed JSON, and missing fields.
    """
    out = TokenUsage()
    if not log_path or not log_path.exists():
        return out
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
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
                usage = msg.get("usage") or {}
                out.input_tokens += int(usage.get("input_tokens") or 0)
                out.output_tokens += int(usage.get("output_tokens") or 0)
                out.cache_read += int(usage.get("cache_read_input_tokens") or 0)
                out.cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
    except OSError:
        pass
    return out


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
