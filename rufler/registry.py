"""Centralized rufler run registry (docker-like).

Stores a json index of every `rufler start` invocation across every project
under `~/.rufler/registry.json`, so commands like `rufler ps`, `rufler logs <id>`,
`rufler follow <id>`, `rufler stop <id>` work from any working directory.

Entry shape on disk:
    {
        "id": "a1b2c3d4",               # 8-char hex, docker-like
        "project": "my-service",
        "flow_file": "/abs/path/rufler_flow.yml",
        "base_dir": "/abs/path",
        "mode": "background" | "foreground",
        "run_mode": "sequential" | "parallel",
        "started_at": 1776190000.12,
        "finished_at": null | 1776190500.77,
        "pids": [12345],
        "log_path": "/abs/.rufler/run.log",
        "tasks": [{"name": "main", "log_path": "...", "pid": 12345}]
    }

`status` / `exit_code` are NOT persisted — they are recomputed on every read
from pid liveness + a tail scan of the log for `log ended rc=<N>`.

Concurrency: all read-modify-write paths take an exclusive fcntl.flock on a
sidecar lock file so multiple `rufler start` invocations from different
terminals/projects don't clobber each other.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover — rufler is Linux-first
    fcntl = None  # type: ignore[assignment]


REGISTRY_DIR = Path.home() / ".rufler"
REGISTRY_PATH = REGISTRY_DIR / "registry.json"
LOCK_PATH = REGISTRY_DIR / "registry.lock"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_starttime(pid: int) -> Optional[int]:
    """Return /proc/<pid>/stat field 22 (starttime in clock ticks), or None.

    Used to disambiguate a recycled PID: if the starttime we recorded at spawn
    time no longer matches the current /proc/<pid>/stat, the original process
    is gone and a new one happens to share the number.
    """
    if not pid or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # comm field is parenthesized and may contain spaces — split on the last ')'
    rp = raw.rfind(")")
    if rp < 0:
        return None
    parts = raw[rp + 2 :].split()
    # After comm, fields start at index 3 (state=0). Starttime is field 22,
    # which is parts[22 - 3] = parts[19].
    if len(parts) <= 19:
        return None
    try:
        return int(parts[19])
    except ValueError:
        return None


def _pid_alive_verified(pid: int, expected_starttime: Optional[int]) -> bool:
    """PID liveness with optional starttime verification (anti-recycle)."""
    if not _pid_alive(pid):
        return False
    if expected_starttime is None:
        return True
    actual = _pid_starttime(pid)
    if actual is None:
        # /proc not readable (non-Linux or permission) — fall back to os.kill.
        return True
    return actual == expected_starttime


def _tail_rc(log_path: Path, window: int = 8192) -> Optional[int]:
    """Scan the tail of a log for `log ended rc=<N>` and return N, or None."""
    try:
        size = log_path.stat().st_size
    except OSError:
        return None
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, size - window))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    rc: Optional[int] = None
    for ln in tail.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        text = str(rec.get("text") or "")
        if rec.get("src") == "rufler" and text.startswith("log ended"):
            for part in text.split():
                if part.startswith("rc="):
                    try:
                        rc = int(part[3:])
                    except ValueError:
                        rc = None
    return rc


@dataclass
class ProjectEntry:
    """Per-project rollup — one row per unique project name.

    Lives alongside RunEntry list in registry.json so we can show
    "last time project X ran" even after its individual run entries
    have been pruned. Token totals accumulate across the project's
    entire history; they survive `rufler rm`.
    """
    name: str
    last_run_id: str = ""
    last_base_dir: str = ""
    last_flow_file: str = ""
    last_started_at: float = 0.0
    last_finished_at: Optional[float] = None
    total_runs: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_creation: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectEntry":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class TaskEntry:
    """One row per task inside a run.

    Populated eagerly by `rufler run` *before* spawn so the task is visible
    on `rufler tasks` even while queued. `started_at`/`finished_at`/`rc` and
    per-task token totals are backfilled as the run progresses — either by
    the orchestrator writing `task_start`/`task_end` markers into the NDJSON
    log, or (parallel mode) derived from the existing `log ended rc=` tail
    marker.

    `id` is `<run_id>.<slot2>` e.g. `a1b2c3d4.01`.
    """
    name: str
    log_path: str
    pid: Optional[int] = None
    id: str = ""
    slot: int = 0
    source: str = "inline"          # inline | group | decomposed | main
    file_path: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    rc: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    # Non-persisted, recomputed on read.
    status: str = field(default="queued", repr=False)

    _PERSISTED = (
        "name", "log_path", "pid", "id", "slot", "source", "file_path",
        "started_at", "finished_at", "rc",
        "input_tokens", "output_tokens", "cache_read", "cache_creation",
    )

    def to_dict(self) -> dict:
        full = asdict(self)
        return {k: full[k] for k in self._PERSISTED if k in full}

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read
            + self.cache_creation
        )

    @classmethod
    def from_dict(cls, d: dict) -> "TaskEntry":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class RunEntry:
    id: str
    project: str
    flow_file: str
    base_dir: str
    mode: str                    # background | foreground
    run_mode: str                # sequential | parallel
    started_at: float
    finished_at: Optional[float] = None
    pids: list[int] = field(default_factory=list)
    # Parallel to `pids`: /proc/<pid>/stat starttime captured at spawn time.
    # 0 = "unknown / not captured", which makes liveness fall back to os.kill.
    pid_starttimes: list[int] = field(default_factory=list)
    log_path: str = ""
    tasks: list[TaskEntry] = field(default_factory=list)
    # Token usage — last known totals from the run log(s). Refreshed by
    # `Registry.recompute_tokens(entry)`. Kept on the entry so we don't
    # have to re-parse logs on every `rufler ps` render.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    # --- Non-persisted, recomputed on every read ---
    status: str = field(default="running", repr=False)
    exit_code: Optional[int] = field(default=None, repr=False)

    # Fields that go to disk. Everything else (status, exit_code) is computed.
    _PERSISTED = (
        "id", "project", "flow_file", "base_dir", "mode", "run_mode",
        "started_at", "finished_at", "pids", "pid_starttimes", "log_path", "tasks",
        "input_tokens", "output_tokens", "cache_read", "cache_creation",
    )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read
            + self.cache_creation
        )

    def to_dict(self) -> dict:
        full = asdict(self)
        d = {k: full[k] for k in self._PERSISTED if k in full}
        d["tasks"] = [t.to_dict() for t in self.tasks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RunEntry":
        allowed = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in d.items() if k in allowed and k != "tasks"}
        tasks = [TaskEntry.from_dict(t) for t in d.get("tasks", []) if isinstance(t, dict)]
        return cls(tasks=tasks, **kwargs)


class Registry:
    """File-backed JSON registry with fcntl.flock-based exclusion.

    Every mutating op (`add`, `update`, `remove`, `prune`) takes an exclusive
    lock on `~/.rufler/registry.lock` for the whole read-modify-write window,
    so two `rufler start` invocations from different terminals can't clobber
    each other's entries.
    """

    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self.lock_path = path.parent / "registry.lock"

    # ---- locking ----

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            # Non-POSIX fallback: no locking, best-effort.
            yield
            return
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ---- raw IO ----
    #
    # On-disk format (v2):
    #     {"version": 2, "runs": [...RunEntry dicts...], "projects": {name: {...}}}
    #
    # Legacy format (v1) was just `[...RunEntry dicts...]` — still read
    # transparently so old registries keep working; they're migrated to v2
    # on the next write.

    def _load_full(self) -> tuple[list[dict], dict[str, dict]]:
        if not self.path.exists():
            return [], {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return [], {}
        if isinstance(data, list):
            return data, {}
        if isinstance(data, dict):
            runs = data.get("runs") if isinstance(data.get("runs"), list) else []
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            return runs, projects
        return [], {}

    def _load_raw(self) -> list[dict]:
        return self._load_full()[0]

    def _save_full(self, runs: list[dict], projects: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"version": 2, "runs": runs, "projects": projects}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _save_raw(self, items: list[dict]) -> None:
        # Preserve existing projects map across writes that only touch runs.
        _, projects = self._load_full()
        self._save_full(items, projects)

    # ---- projects rollup ----

    def _touch_project(self, projects: dict[str, dict], entry: RunEntry) -> None:
        """Bump the project rollup for `entry`'s project name."""
        p = projects.get(entry.project) or {"name": entry.project, "total_runs": 0}
        # New run → always advance "last_*"; total_runs only increments on add.
        p["name"] = entry.project
        p["last_run_id"] = entry.id
        p["last_base_dir"] = entry.base_dir
        p["last_flow_file"] = entry.flow_file
        p["last_started_at"] = entry.started_at
        p["last_finished_at"] = entry.finished_at
        projects[entry.project] = p

    # ---- public read API (no locks — readers tolerate a replacement via atomic rename) ----

    def list_all(self) -> list[RunEntry]:
        out: list[RunEntry] = []
        for d in self._load_raw():
            try:
                out.append(RunEntry.from_dict(d))
            except Exception:
                continue
        return out

    def refresh_status(self, entry: RunEntry) -> RunEntry:
        """Recompute status/exit_code from pid liveness + log tail + finished_at.

        Status model (docker-inspired):
          - running : at least one registered pid is alive
          - exited  : pids dead, log shows `log ended rc=0`
          - failed  : pids dead, log shows `log ended rc=N` with N != 0
          - stopped : pids dead, no rc marker, but finished_at is set
                      (rufler itself wrote the end — Ctrl+C or `rufler stop`)
          - dead    : pids dead, no rc marker, no finished_at — the supervisor
                      vanished without cleanup (was previously `unknown`)
        """
        # Pair each pid with its captured starttime (if any) so a recycled
        # PID doesn't fake liveness.
        sts = list(entry.pid_starttimes) if entry.pid_starttimes else []
        pairs = [(p, sts[i] if i < len(sts) else None) for i, p in enumerate(entry.pids)]
        alive = any(_pid_alive_verified(p, st) for p, st in pairs if p)
        if alive:
            entry.status = "running"
            entry.exit_code = None
            return entry
        log = Path(entry.log_path) if entry.log_path else None
        rc = _tail_rc(log) if log else None
        if rc is not None:
            entry.exit_code = rc
            entry.status = "exited" if rc == 0 else "failed"
            if entry.finished_at is None:
                try:
                    entry.finished_at = log.stat().st_mtime if log else time.time()
                except OSError:
                    entry.finished_at = time.time()
            return entry
        # Pid gone, no rc marker in the log.
        entry.exit_code = None
        if entry.finished_at is not None:
            entry.status = "stopped"
        else:
            entry.status = "dead"
        return entry

    def list_refreshed(self, include_all: bool = False) -> list[RunEntry]:
        entries = [self.refresh_status(e) for e in self.list_all()]
        if not include_all:
            entries = [e for e in entries if e.status == "running"]
        entries.sort(key=lambda e: e.started_at, reverse=True)
        return entries

    def find_ambiguous(self, id_prefix: str) -> list[RunEntry]:
        """Return ALL entries whose id starts with the given prefix."""
        id_prefix = (id_prefix or "").strip()
        if not id_prefix:
            return []
        return [e for e in self.list_all() if e.id.startswith(id_prefix)]

    def list_projects(self) -> list[ProjectEntry]:
        """Return the per-project rollup sorted by last_started_at desc."""
        _, projects = self._load_full()
        out: list[ProjectEntry] = []
        for d in projects.values():
            try:
                out.append(ProjectEntry.from_dict(d))
            except Exception:
                continue
        out.sort(key=lambda p: p.last_started_at, reverse=True)
        return out

    # ---- mutating API (all locked) ----

    def add(self, entry: RunEntry) -> None:
        with self._locked():
            items, projects = self._load_full()
            items.append(entry.to_dict())
            # Bump project rollup + total_runs.
            self._touch_project(projects, entry)
            projects[entry.project]["total_runs"] = (
                int(projects[entry.project].get("total_runs") or 0) + 1
            )
            self._save_full(items, projects)

    def update(self, entry: RunEntry) -> None:
        with self._locked():
            items, projects = self._load_full()
            for i, d in enumerate(items):
                if d.get("id") == entry.id:
                    items[i] = entry.to_dict()
                    break
            else:
                items.append(entry.to_dict())
            # Refresh last_* for this project (e.g. to record finished_at on
            # the most recent run) but DON'T re-increment total_runs.
            existing_project = projects.get(entry.project) or {}
            last_id = existing_project.get("last_run_id")
            if not last_id or last_id == entry.id:
                self._touch_project(projects, entry)
                if "total_runs" not in projects[entry.project]:
                    projects[entry.project]["total_runs"] = 1
            self._save_full(items, projects)

    def remove(self, entry_id: str) -> bool:
        with self._locked():
            items = self._load_raw()
            new_items = [d for d in items if d.get("id") != entry_id]
            if len(new_items) == len(items):
                return False
            self._save_raw(new_items)
            return True

    def remove_many(self, entry_ids: list[str]) -> int:
        """Batch remove — one lock acquisition for the whole set."""
        if not entry_ids:
            return 0
        ids_set = set(entry_ids)
        with self._locked():
            items = self._load_raw()
            new_items = [d for d in items if d.get("id") not in ids_set]
            removed = len(items) - len(new_items)
            if removed:
                self._save_raw(new_items)
            return removed

    # ---- CLI-friendly state-transition helpers (T19) ----

    def attach_pid(
        self, entry: RunEntry, pid: int, starttime: Optional[int] = None
    ) -> None:
        """Record a live supervisor pid + its /proc starttime on the entry
        and persist. Keeps pid_starttimes parallel to pids."""
        if not pid:
            return
        entry.pids.append(int(pid))
        entry.pid_starttimes.append(int(starttime or 0))
        self.update(entry)

    def attach_task(self, entry: RunEntry, task: TaskEntry) -> None:
        entry.tasks.append(task)
        self.update(entry)

    def mark_finished(self, entry: RunEntry) -> None:
        entry.finished_at = time.time()
        self.update(entry)

    # ---- token accounting ----

    def recompute_tokens(self, entry: RunEntry) -> RunEntry:
        """Re-parse all known log files for `entry`, update its token totals,
        and propagate the delta into the project rollup.

        Idempotent: only the *delta* vs the previously-recorded entry totals
        is added to the project rollup, so calling this repeatedly does not
        double-count.
        """
        from .tokens import parse_logs  # local import: avoid hard dep cycle

        log_paths: list[Path] = []
        if entry.log_path:
            log_paths.append(Path(entry.log_path))
        for t in entry.tasks:
            if t.log_path:
                log_paths.append(Path(t.log_path))

        usage = parse_logs(log_paths)

        prev_total = entry.input_tokens + entry.output_tokens + entry.cache_read + entry.cache_creation
        delta_in = usage.input_tokens - entry.input_tokens
        delta_out = usage.output_tokens - entry.output_tokens
        delta_cr = usage.cache_read - entry.cache_read
        delta_cc = usage.cache_creation - entry.cache_creation

        entry.input_tokens = usage.input_tokens
        entry.output_tokens = usage.output_tokens
        entry.cache_read = usage.cache_read
        entry.cache_creation = usage.cache_creation

        # Persist entry + bump project rollup atomically.
        with self._locked():
            items, projects = self._load_full()
            for i, d in enumerate(items):
                if d.get("id") == entry.id:
                    items[i] = entry.to_dict()
                    break
            p = projects.get(entry.project)
            if p is None:
                p = {"name": entry.project, "total_runs": 0}
                projects[entry.project] = p
            p["total_input_tokens"] = int(p.get("total_input_tokens") or 0) + delta_in
            p["total_output_tokens"] = int(p.get("total_output_tokens") or 0) + delta_out
            p["total_cache_read"] = int(p.get("total_cache_read") or 0) + delta_cr
            p["total_cache_creation"] = int(p.get("total_cache_creation") or 0) + delta_cc
            self._save_full(items, projects)
        return entry

    def grand_total_tokens(self) -> dict:
        """Sum tokens across every project rollup."""
        out = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        for p in self.list_projects():
            out["input"] += p.total_input_tokens
            out["output"] += p.total_output_tokens
            out["cache_read"] += p.total_cache_read
            out["cache_creation"] += p.total_cache_creation
        return out

    def prune(self, *, missing_dirs: bool = True, older_than_sec: Optional[int] = None) -> int:
        """Drop stale entries.

        - `missing_dirs=True` → drop entries whose base_dir no longer exists.
        - `older_than_sec` → drop entries that finished more than N seconds ago.
          Running entries are always kept regardless of age.
        Returns the number of entries removed.
        """
        now = time.time()
        with self._locked():
            items = self._load_raw()
            kept: list[dict] = []
            for d in items:
                if missing_dirs and not Path(d.get("base_dir", "")).exists():
                    continue
                if older_than_sec is not None:
                    fin = d.get("finished_at")
                    if fin and (now - float(fin)) > older_than_sec:
                        # Only age-prune entries that have actually finished.
                        continue
                kept.append(d)
            removed = len(items) - len(kept)
            if removed:
                self._save_raw(kept)
            return removed


def new_entry(
    *,
    project: str,
    flow_file: Path,
    base_dir: Path,
    mode: str,
    run_mode: str,
    log_path: Path,
) -> RunEntry:
    return RunEntry(
        id=_new_id(),
        project=project,
        flow_file=str(flow_file),
        base_dir=str(base_dir),
        mode=mode,
        run_mode=run_mode,
        started_at=time.time(),
        log_path=str(log_path),
    )
