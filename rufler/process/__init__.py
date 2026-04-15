"""Process subpackage — daemonization, log paths, proc discovery, signalling."""
from .daemon import (
    DEFAULT_FLOW_FILE,
    DEFAULT_LOG_REL,
    daemonize,
    resolve_entry_or_cwd,
    resolve_log_path,
    setup_log_for,
    wait_for_log_end,
)
from .procs import find_claude_procs, fmt_age, human_size, kill_pid_tree

__all__ = [
    "DEFAULT_FLOW_FILE",
    "DEFAULT_LOG_REL",
    "daemonize",
    "find_claude_procs",
    "fmt_age",
    "human_size",
    "kill_pid_tree",
    "resolve_entry_or_cwd",
    "resolve_log_path",
    "setup_log_for",
    "wait_for_log_end",
]
