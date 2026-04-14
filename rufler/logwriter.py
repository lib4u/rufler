"""NDJSON log writer. Spawned as a detached child by `rufler start`.

Reads stdout+stderr of a wrapped command line-by-line and writes a
normalized newline-delimited JSON log where EVERY line is a JSON object.

Envelope:
    {"ts": 1712345678.12, "src": "claude", ...rest of parsed line...}
    {"ts": 1712345678.20, "src": "ruflo", "level": "info", "text": "..."}

Rules:
- If the raw line is valid JSON → use it as the record, add `ts` + `src`.
  `src` is inferred: Claude stream-json lines have `type` in a known set,
  everything else is tagged `src=ruflo`.
- Otherwise → wrap as {"src": "ruflo", "level": <detected>, "text": <line>}.
- ANSI escape sequences and decorative ASCII box chars are stripped from
  text lines so they render cleanly in a TUI.

Invoke directly:
    python -m rufler.logwriter <log_path> -- <command> <args...>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Strip box-drawing + common decorative chars so TUI doesn't get messy
DECOR_RE = re.compile(r"[─━═│┃┌┐└┘├┤┬┴┼╭╮╯╰▬]+")

CLAUDE_TYPES = {"system", "assistant", "user", "result", "rate_limit_event"}

LEVEL_MAP = [
    (re.compile(r"^\s*\[ERROR\]|\bERROR\b|✗|❌"), "error"),
    (re.compile(r"^\s*\[WARN(ING)?\]|⚠"), "warn"),
    (re.compile(r"^\s*\[OK\]|✓|🎯"), "ok"),
    (re.compile(r"^\s*\[INFO\]|ℹ|🧠|🚀|📌"), "info"),
]


def detect_level(text: str) -> str:
    for rx, lvl in LEVEL_MAP:
        if rx.search(text):
            return lvl
    return "debug"


def clean_text(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = DECOR_RE.sub("", text)
    return text.rstrip()


def wrap_line(raw: str) -> dict | None:
    stripped = raw.rstrip("\n")
    if not stripped.strip():
        return None
    rec: dict
    if stripped.lstrip().startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                src = "claude" if parsed.get("type") in CLAUDE_TYPES else "ruflo"
                rec = {"ts": time.time(), "src": src}
                rec.update(parsed)
                return rec
        except json.JSONDecodeError:
            pass
    cleaned = clean_text(stripped)
    if not cleaned:
        return None
    return {
        "ts": time.time(),
        "src": "ruflo",
        "level": detect_level(cleaned),
        "text": cleaned,
    }


def run(log_path: Path, command: list[str], tee: bool = False) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # In tee mode the user is watching — forward the child's stdin from
    # our own stdin so interactive prompts still work.
    stdin = None if tee else subprocess.DEVNULL
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=stdin,
        bufsize=1,
        text=True,
        errors="replace",
    )
    start = time.time()
    # Keep the start-marker short: redact oversized --objective payloads so
    # the first log line doesn't balloon to the full prompt size.
    display_cmd = []
    for a in command:
        if a.startswith("--objective=") and len(a) > 200:
            display_cmd.append(a[:200] + "...(truncated)")
        else:
            display_cmd.append(a)
    with open(log_path, "a", buffering=1, encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": start,
                    "src": "rufler",
                    "level": "info",
                    "text": f"log started: {' '.join(display_cmd)}",
                    "pid": proc.pid,
                }
            )
            + "\n"
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            if tee:
                sys.stdout.write(raw)
                sys.stdout.flush()
            rec = wrap_line(raw)
            if rec is None:
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        rc = proc.wait()
        f.write(
            json.dumps(
                {
                    "ts": time.time(),
                    "src": "rufler",
                    "level": "ok" if rc == 0 else "error",
                    "text": f"log ended rc={rc} elapsed={time.time() - start:.1f}s",
                }
            )
            + "\n"
        )
    return rc


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2 or "--" not in argv:
        print("usage: python -m rufler.logwriter [--tee] <log_path> -- <cmd...>", file=sys.stderr)
        return 2
    tee = False
    if argv[0] == "--tee":
        tee = True
        argv = argv[1:]
    log_arg = argv[0]
    sep = argv.index("--")
    command = argv[sep + 1 :]
    if not command:
        print("missing command after --", file=sys.stderr)
        return 2
    return run(Path(log_arg).resolve(), command, tee=tee)


if __name__ == "__main__":
    raise SystemExit(main())
