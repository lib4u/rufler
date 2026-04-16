"""Stream ``claude -p --output-format stream-json`` into an NDJSON log.

Provides :func:`stream_claude`, a drop-in replacement for
``subprocess.run(capture_output=True)`` that writes every stream-json
line to the run log in real time while collecting the final text output.

Also emits lightweight phase markers (``phase_start`` / ``phase_end``)
that :class:`~rufler.follow.TuiState` renders in the Conversation panel.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

from .logwriter import wrap_line


def _emit(f, rec: dict) -> None:
    """Write one NDJSON record."""
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()


def emit_phase(log_path: Path, phase: str, *, end: bool = False) -> None:
    """Write a ``phase_start`` or ``phase_end`` marker to *log_path*."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "src": "rufler",
        "type": "phase_end" if end else "phase_start",
        "phase": phase,
    }
    with open(log_path, "a", buffering=1, encoding="utf-8") as f:
        _emit(f, rec)


def stream_claude(
    cmd: list[str],
    *,
    log_path: Optional[Path] = None,
    timeout: int = 600,
    phase: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* (a ``claude -p`` invocation) and stream its output.

    If *log_path* is given, every stdout line is written to the NDJSON log
    in real time (using :func:`logwriter.wrap_line`).  The final text output
    is collected from ``stream-json`` content blocks and returned as
    ``stdout`` on the :class:`~subprocess.CompletedProcess`.

    When *log_path* is ``None``, falls back to ``subprocess.run`` with
    ``capture_output=True`` (legacy behaviour).
    """
    if log_path is None:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )

    # Switch to stream-json so we get structured events.
    # claude -p requires --verbose for stream-json output.
    run_cmd = list(cmd)
    for i, arg in enumerate(run_cmd):
        if arg == "--output-format" and i + 1 < len(run_cmd):
            run_cmd[i + 1] = "stream-json"
            break
    else:
        run_cmd.extend(["--output-format", "stream-json"])
    if "--verbose" not in run_cmd:
        run_cmd.append("--verbose")

    if phase:
        emit_phase(log_path, phase)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    text_parts: list[str] = []

    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        bufsize=1,
        text=True,
        errors="replace",
    )

    timed_out = False
    try:
        deadline = time.time() + timeout
        assert proc.stdout is not None
        with open(log_path, "a", buffering=1, encoding="utf-8") as f:
            for raw_line in proc.stdout:
                if time.time() > deadline:
                    timed_out = True
                    break

                # Write to NDJSON log via logwriter's wrap_line
                rec = wrap_line(raw_line)
                if rec is not None:
                    _emit(f, rec)

                # Collect text output from assistant content blocks
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "assistant":
                    msg = obj.get("message") or {}
                    for block in msg.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text") or "")
                # Also handle the top-level result.result for final text
                if obj.get("type") == "result":
                    result_text = obj.get("result")
                    if isinstance(result_text, str) and result_text:
                        text_parts.clear()
                        text_parts.append(result_text)
    finally:
        if timed_out:
            proc.kill()
        proc.stdout.close()  # type: ignore[union-attr]
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.stderr.close()  # type: ignore[union-attr]
        rc = proc.wait()

    if phase:
        emit_phase(log_path, phase, end=True)

    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout)

    stdout_text = "\n".join(text_parts)
    return subprocess.CompletedProcess(
        args=cmd, returncode=rc, stdout=stdout_text, stderr=stderr,
    )
