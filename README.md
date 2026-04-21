<img width="2582" height="1384" src="https://github.com/user-attachments/assets/1c45f01d-27bc-49c0-9cfb-3d455822def6" />
# Rufler

**One command. One YAML file. A whole autonomous AI swarm.**

`rufler` is a Python wrapper around [`ruflo`](https://github.com/ruvnet/ruflo) (the TypeScript AI agent orchestration CLI from the `claude-flow` family) that turns a dozen bootstrap commands into a single, declarative `rufler_flow.yml` file.

Instead of memorizing `ruflo init → daemon start → memory init → swarm init → hive-mind init → hive-mind spawn --claude --objective=...`, you write a YAML file that describes your project, your agent team and your task, and run:

```bash
rufler run
```

rufler handles preflight checks, daemon lifecycle, swarm/memory/hive-mind init, objective composition, logging, multi-task sequencing, AI-driven task decomposition, background supervision and live log following.

---

## Why rufler and not ruflo directly?

`ruflo` is powerful but low-level. It exposes ~26 commands and 140+ subcommands that all have to be called in the right order with the right flags. For day-to-day driving of an autonomous Claude-Code swarm, that's a lot of ceremony.

| Problem with raw `ruflo` | How `rufler` fixes it |
|--------------------------|-----------------------|
| 6–10 shell commands to boot a project (`init` → `daemon start` → `memory init` → `swarm init` → `hive-mind init` → `hive-mind spawn --claude --objective=...`) | **One command: `rufler run`** reads your YAML and runs the whole pipeline. |
| Objective prompt must be assembled by hand — project name, task body, every agent's role and prompt, autonomy flags | **Auto-composed objective** from `rufler_flow.yml` — agents are sorted lead → senior → junior and injected into a single coherent prompt. |
| No native multi-task support — you spawn one hive per task manually | **Multi-task mode** (`task.multi: true`) — run a list of subtasks sequentially or in parallel from one YAML. |
| No task decomposition | **AI decomposition** (`task.decompose: true`) — rufler calls `claude -p` to split a big `main` task into N subtasks before spawning the swarm. |
| Backgrounding is fragile: detaching from terminal, redirecting stdout, closing fds | **Built-in supervisor** (`rufler.logwriter`) — detached runs write a clean NDJSON log and never leak fds or tie up your terminal. |
| No uniform log — `ruflo`/Claude emit mixed ANSI text + stream-JSON + plain logs | **Normalized NDJSON**: every line is a JSON object with `ts`, `src` (`claude` / `ruflo` / `rufler`), detected `level` and cleaned `text`. Trivial to parse, tail, grep. |
| No way to see "what's the swarm doing right now?" without 3 different commands | **`rufler follow`** — `tail -f` with makeup: live dashboard of session, tasks, tokens, last tool activity, recent events. |
| Preflight failures are silent until something crashes mid-run | **`rufler check`** and `rufler doctor`-style start gate — validates node, claude, ruflo and config before touching anything. |
| No graceful teardown | **`rufler stop`** — shuts down autopilot, hive-mind, daemon and records post-task metrics in one call. |
| Flags like `--dangerously-skip-permissions` get silently dropped when `--objective` is huge | **Belt-and-suspenders**: rufler also writes `.claude/settings.local.json` with `permissions.defaultMode=bypassPermissions` so headless runs never stall on a prompt. |
| YAML config? There isn't one. | **`rufler_flow.yml`** — checked into your repo, reviewable in PRs, diffable over time. |
| Third-party skills have to be cloned / vendored / copied by hand | **[skills.sh](https://skills.sh) integration** — list GitHub repos, `owner/repo` shorthands, or paste full `skills add …` commands under `skills.custom`; rufler runs `npx skills add` for you and verifies every `SKILL.md` before the swarm starts. |

In short: `ruflo` is the engine, `rufler` is the ignition + dashboard + cruise control.

---

## Features

- **Single-file project config** — `rufler_flow.yml` describes project, memory, swarm, task and the agent team.
- **One-command boot** — `rufler run` runs checks → init → daemon → memory → swarm → hive-mind → spawn.
- **Docker-like run lifecycle** — `rufler run` (foreground, Ctrl+C kills) / `rufler run -d` (detached background), just like `docker run`. Every invocation gets an 8-char hex id.
- **Cross-project registry** — `~/.rufler/registry.json` tracks every run from every project. `rufler ps` / `rufler ps -a` / `rufler logs <id>` / `rufler follow <id>` / `rufler stop <id>` all work from any directory.
- **Project rollup** — `rufler projects` shows the last-run timestamp and total runs per project name, surviving `rufler rm` and pruning.
- **Rich status model** — `running` / `exited` / `failed(rc)` / `stopped` / `dead`, computed from pid liveness + log tail + finish marker.
- **Token usage tracking** — `rufler tokens` reports input/output/cache tokens per run, per project, and grand total across all projects. Per-project totals are cumulative and survive `rufler rm`.
- **Agent inspection** — `rufler agents` lists every agent in the flow with type, role, seniority, depends_on and a 150-char prompt preview (`--full` for the full body).
- **Soft agent DAG** — declare `depends_on: [architect]` on an agent and rufler injects GATE/HANDOFF blocks into the objective so downstream agents poll shared memory for an upstream brief and approval before starting work. Cycles, self-deps and unknown names are rejected at load time.
- **Task tracking** — every task gets a sub-id (`a1b2c3d4.01`), persistent status (queued / running / exited / failed / stopped / skipped), per-task token accounting, and timing. `rufler tasks` shows it all; `rufler tasks <sub-id> -v` prints a detailed card.
- **Resume on restart** — `rufler run` automatically detects the previous run for the same project, skips already-completed tasks, and reuses decomposed task files. No wasted claude calls. `--new` forces a clean start, `--from N` resumes from a specific task slot.
- **Soft resume via memory** — the composed objective tells agents to probe the shared memory namespace for prior progress before restarting work, so an interrupted run can pick up where it left off.
- **Mono, multi and decomposed tasks** — run one big task, an explicit list of subtasks, or let Claude decompose `main` into N subtasks.
- **Sequential and parallel run modes** for multi-task groups.
- **Task chaining** (`task.chain: true`) — in sequential multi-task mode, each new `claude -p` session receives a compressed retrospective of all previous tasks (body + report), so it has full context without sharing a session. Per-task override with `chain: false` on individual group items.
- **Deep Think** (`task.deep_think: true`) — before decomposing or executing, rufler spawns a read-only `claude -p` session (configurable model, default opus) that scans the project structure, reads key files, and writes a structured analysis to `.rufler/analysis.md`. The analysis is injected into the decomposer and/or the agent objective so every downstream step has project-aware context. Cached on re-run; `--new` forces rescan.
- **Iterative refinement** (`task.iterations: N`) — run the whole deep_think → decompose → execute → report cycle N times. Each iteration's reports are injected into the next iteration's deep_think so the plan refines instead of restarting blind. Optional **judge agent** (`iteration_judge: true`) evaluates the project at every iteration boundary and short-circuits the loop when a score threshold is reached. Per-iteration artifacts are namespaced under `.rufler/iter-NN/`, task logs get iter prefixes, no collisions.
- **Detached background mode** with a proper supervisor process and NDJSON logs.
- **Live dashboard** via `rufler follow` — 4-panel TUI with task list, session stats, AI conversation stream (thinking, text, tool calls), and system events. Tails all per-task logs in multi-task mode.
- **Custom decomposer prompt** — override the task-splitter prompt inline or from an `.md` file.
- **Typed dataclass config** — schema validation, role/seniority checks, unknown-key tolerance.
- **External prompts** — agents and tasks can be inline OR loaded from `.md` files, so prompts stay out of YAML.
- **[skills.sh](https://skills.sh) integration** — pull third-party Claude Code skills straight from the yml. Drop a repo URL, an `owner/repo` shorthand, or paste a full `skills add …` command into `skills.custom`; rufler shells out to `npx skills add` pre-run, verifies each `SKILL.md` landed, and reports results in the plan banner. Mix-and-match with local paths and ruflo's built-in packs in the same list.
- **MCP server management** — declare MCP servers in `mcp.servers` and rufler registers them with Claude Code via `claude mcp add` during init. Supports stdio, http, and sse transports with env vars and headers. `rufler mcp` inspects what's declared vs registered.
- **Automatic reports** — after each task (`on_task_complete`) and after all tasks (`on_complete`), rufler spawns a short claude session to write a markdown report. Enabled by default; custom prompts supported inline or from `.md` files.
- **Graceful shutdown** — `rufler stop` ends the session cleanly and writes post-task hooks.

---

## Installation

### Prerequisites

- **Python 3.9+**
- **Node.js 20+** and **npm 9+**
- **Claude Code CLI** (`claude` on `$PATH`) — see [claude.ai/code](https://claude.ai/code)
- **ruflo** — rufler will resolve it via `$RUFLER_RUFLO_BIN`, local `node_modules`, `$PATH`, npm global bin, or `npx -y ruflo@latest` as a last resort. You can also run `npm i -g ruflo`.

### Install rufler

From source (until it's on PyPI):

```bash
git clone https://github.com/lib4u/rufler.git
cd rufler
pip install -e .
```

Verify:

```bash
rufler --help
rufler check
```

`rufler check` will tell you exactly which dependency (node / claude / ruflo) is missing and how to install it.

---

## Quickstart

```bash
# 1. Create a sample flow file in the current directory
rufler init

# 2. Edit rufler_flow.yml — change project name, task, agents
$EDITOR rufler_flow.yml

# 3. Dry run — prints the composed objective and the plan, runs nothing
rufler run --dry-run

# 4. Launch the swarm (foreground, streams to terminal AND to NDJSON log)
rufler run

# 5. Or launch detached in the background
rufler run -d

# 6. Watch it work
rufler follow           # live dashboard
rufler status           # one-shot status
rufler progress         # autopilot task progress
rufler logs             # recent autopilot events

# 7. Clean shutdown
rufler stop
```

---

## Commands

| Command | What it does |
|---------|--------------|
| `rufler check` | Verify that `node`, `claude` and `ruflo` are available and report how each was resolved. |
| `rufler init` | Create a sample `rufler_flow.yml` in the current directory. |
| `rufler agents [ID]` | List agents declared in the flow (name, type, role, seniority, depends_on, 150-char prompt preview). With an id → reads the flow file from that run's registry entry. `--full` prints full prompt bodies. |
| `rufler run [FLOW_FILE]` | Validate config, run ruflo init/daemon/memory/swarm/hive-mind, spawn the Claude swarm. Foreground by default; Ctrl+C kills. Add `-d` to detach. Resumes from last completed task by default; `--new` to start fresh, `--from N` to resume from slot N. |
| `rufler build [FLOW_FILE]` | Same preparation pipeline as `rufler run` (checks → ruflo init → daemon → memory → swarm → hive-mind → skills install) **without** launching Claude. Useful to apply yml changes (skills, memory, swarm) to an existing project. `--skip-init` skips the ruflo init + daemon step for quick re-builds. |
| `rufler skills` | Inspect installed skills in `<project>/.claude/skills/` and show the yml snapshot. `--available` lists packs/skills in ruflo's bundled source tree. `--delete` wipes non-symlinked skill dirs from the project (with confirmation; `-y` to skip). |
| `rufler mcp` | List MCP servers declared in `rufler_flow.yml`. `--active` shows what's actually registered in `~/.claude.json` for this project. Servers are added via `claude mcp add` during `rufler run`/`build`. |
| `rufler ps [ID]` | Docker-style list of runs. No args → running only. `-a` → all. `--prune` / `--prune-older-than-days N` → clean stale entries. With an ID → detailed view of one run (status, tasks, claude procs, log tail). |
| `rufler tasks [ID]` | List tasks for a run with sub-ids, status, tokens, timing. Without id → latest run in cwd. `-a` → all runs. `--status running` → filter. Pass a task sub-id (e.g. `a1b2c3d4.01 -v`) for a detailed card with token breakdown and recent log events. `--rm` removes tasks from the registry; `--rm-all` removes all tasks in cwd; `--rm-files` also deletes task files and logs from disk. |
| `rufler projects` | Per-project rollup: last run id, when it last ran, total runs. Survives `rufler rm` and pruning. |
| `rufler logs [ID]` | Tail recent autopilot events. `--raw` prints the NDJSON log directly. `-f` / `--follow` streams new lines live. Pass a task sub-id (e.g. `a1b2c3d4.01`) to see only that task's log slice. |
| `rufler follow [ID]` | Live TUI dashboard: task progress (with status icons), AI conversation stream (thinking, text, tool calls), session stats, and system events. Tails all per-task logs automatically. |
| `rufler status [ID]` | System + swarm + hive-mind + autopilot status in one call. |
| `rufler watch [ID]` | Poll `rufler status` on a loop until Ctrl-C. |
| `rufler progress [ID]` | Autopilot task progress + recent iteration log. |
| `rufler stop [ID]` | Shutdown autopilot, hive-mind and daemon; record post-task hook; end session. `--kill` sends SIGTERM to the supervisor pids. |
| `rufler rm [ID]...` | Remove registry entries. `--all-finished` / `--older-than-days N` for bulk cleanup. Refuses running entries. |
| `rufler tokens [ID]` | Token usage report — without id: per-project table + grand total. With an id: detailed breakdown. `--by-task` shows per-task token breakdown. `--rescan` re-parses run logs from disk. |

Every run-scoped command (`logs`, `follow`, `status`, `watch`, `progress`, `stop`) accepts an optional run id prefix as a positional argument. Without an id it resolves to the run started from the current working directory.

### `rufler run` flags

```
rufler run [FLOW_FILE] [OPTIONS]

Arguments:
  FLOW_FILE                    Positional path to flow yml. Overrides -c.

Options:
  -c, --config PATH            Path to rufler_flow.yml  [default: rufler_flow.yml]
  --dry-run                    Print the plan and composed objective, don't execute.
  --skip-checks                Skip node/claude/ruflo preflight.
  --skip-init                  Skip ruflo init + daemon + memory init (useful for re-runs).
  --non-interactive/--interactive
                               Run Claude Code headless (-p stream).
  --yolo/--no-yolo             Pass --dangerously-skip-permissions.
  -d, --background/--foreground
                               Detach from terminal (implies --non-interactive --yolo).
  --log-file PATH              Override execution.log_file from yml.
  --new                        Start all tasks from scratch: re-decompose and ignore
                               previous progress.
  --from N                     Resume from task slot N (1-based), skipping tasks before it.
```

CLI flags always override the `execution:` section of `rufler_flow.yml`.

By default `rufler run` **resumes** from where a previous run stopped. It reuses existing decomposed task files (skipping the claude call) and skips tasks that already completed successfully (`rc=0`). Use `--new` to force a clean start.

---

## `rufler_flow.yml` reference

Minimal example:

```yaml
project:
  name: my-service
  description: Small Go HTTP service.

task:
  main: |
    Build a Go HTTP server on :8080 with a /health endpoint and unit tests.
  autonomous: true

agents:
  - name: coder
    type: coder
    role: worker
    seniority: senior
    prompt: Implement the server and tests.
```

Full schema:

```yaml
project:
  name: my-project                    # required-ish; drives objective header
  description: Free-form description.

memory:
  backend: hybrid                     # hybrid | sqlite | ...
  namespace: my-project
  init: true
  checkpoint_interval_minutes: 5      # how often agents flush state to memory (0 = disable timer, still on events)

swarm:
  topology: hierarchical-mesh         # hierarchical | mesh | hierarchical-mesh | adaptive
  max_agents: 8
  strategy: specialized
  consensus: raft                     # raft | byzantine | gossip | crdt | quorum

task:
  # --- mono mode ---
  main: |                             # inline main task
    Build X, test Y, verify Z.
  main_path: ./tasks/main.md          # OR load from file
  autonomous: true
  max_iterations: 100
  timeout_minutes: 180

  # --- multi mode ---
  multi: false                        # set true to enable group / decompose
  run_mode: sequential                # sequential | parallel
  group:                              # explicit list of subtasks
    - name: backend
      file_path: tasks/backend.md
    - name: frontend
      content: |                      # inline alternative to file_path
        Build the React frontend...

  # --- decompose mode (AI decomposition) ---
  decompose: false                    # true → call claude -p to split `main`
  decompose_count: 4                  # how many subtasks to generate
  decompose_dir: .rufler/tasks        # where to write generated .md files
  decompose_file: .rufler/tasks/decomposed_tasks.yml
  decompose_prompt: |                 # optional inline decomposer prompt override
    You split tasks. Placeholders: {n} and {main}.
    Output YAML: tasks: [ {name, title, content} ]
  decompose_prompt_path: ./prompts/decompose.md   # OR load decomposer prompt from file
  decompose_model: sonnet             # model for the decompose call (default sonnet)
  decompose_effort: high             # thinking effort for decomposer (low/medium/high/max)

  # --- deep think (project analysis before decompose/execute) ---
  deep_think: false                   # true → read-only claude session scans the project first
  deep_think_model: opus              # model for analysis (opus recommended for deep reasoning)
  deep_think_effort: max             # thinking effort for deep think (low/medium/high/max; default max)
  deep_think_output: .rufler/analysis.md  # cached; reused on re-run, --new to regenerate
  deep_think_timeout: 600             # seconds
  deep_think_budget: 1.50            # optional max spend in USD for the deep think session
  deep_think_allowed_tools: "Read,Glob,Grep,Bash"  # restrict --allowedTools (default: not set → ALL tools including MCP)
  deep_think_prompt: |                # optional inline override
    Analyze this project for: {main}
  deep_think_prompt_path: ./prompts/analyze.md    # OR load from file

  # --- task chaining (sequential multi-task only) ---
  chain: false                        # true → inject compressed retrospective of previous tasks into the next task's prompt
  chain_max_tokens: 2000              # word budget for the compressed retrospective (body + report combined)
  chain_include_report: true          # include the per-task report in the retrospective (requires on_task_complete.report)

  # --- iterative refinement (repeat the whole pipeline N times) ---
  iterations: 1                       # 1 = one-shot (default, unchanged behaviour); >1 = loop deep_think→decompose→execute×N
  iteration_scope: full               # full | decompose_only | tasks_only (what regenerates each iter)
  iteration_refine: true              # inject prior iterations' reports into next deep_think
  iteration_judge: false              # true → judge agent decides whether to stop early
  iteration_judge_model: opus         # model for the judge claude -p session
  iteration_judge_effort: max         # thinking effort for the judge (low/medium/high/max)
  iteration_judge_timeout: 600        # seconds
  iteration_judge_threshold: 0.9      # stop when verdict=done AND score >= threshold (0.0-1.0)
  iteration_judge_prompt: |           # optional custom judge prompt (placeholders: {main_task}, {project}, {iter_num}, {total_iters}, {threshold}, {accumulated_reports})
  # iteration_judge_prompt_path: ./prompts/judge.md   # OR load from file
  iteration_stop_on_success: false    # fallback when judge disabled: break when all tasks rc=0

  # --- reports (two levels) ---
  on_task_complete:                     # after EACH task completes
    report: true                        # default: enabled
    report_path: .rufler/reports/{task}.md  # {task} replaced with task name
    # report_prompt: |                  # optional custom prompt
    #   Summarize what this task accomplished.
    # report_prompt_path: ./prompts/task-report.md

  on_complete:                          # after ALL tasks complete
    report: true                        # default: enabled
    report_path: .rufler/report.md
    # report_prompt: |                  # optional custom prompt
    #   Write a final project completion report.
    # report_prompt_path: ./prompts/final-report.md

execution:
  non_interactive: false
  yolo: false                         # pass --dangerously-skip-permissions
  background: false                   # detach from terminal
  log_file: .rufler/run.log

# MCP servers — registered with Claude Code via `claude mcp add -s project`
mcp:
  servers:
    - name: my-db                       # server name (unique, required)
      command: npx                      # executable (required for stdio)
      args: ["-y", "@anthropic/mcp-postgres"]
      env:
        DATABASE_URL: "postgresql://localhost/mydb"

    - name: sentry                      # HTTP transport example
      transport: http                   # stdio (default) | http | sse
      url: "https://mcp.sentry.dev/mcp"

    - name: corridor                    # HTTP with auth headers
      transport: http
      url: "https://app.corridor.dev/api/mcp"
      headers:
        Authorization: "Bearer ${CORRIDOR_TOKEN}"

agents:
  - name: architect
    type: system-architect            # any ruflo agent type, or custom string
    role: specialist                  # queen | specialist | worker | scout
    seniority: lead                   # lead | senior | junior
    prompt: |                         # inline prompt
      Define package layout and interfaces.
    # OR
    prompt_path: ./prompts/architect.md

  - name: coder
    type: coder
    role: worker
    seniority: senior
    prompt: Implement the design.
    depends_on: [architect]           # soft DAG — see "Agent dependencies" below

# Per-task chain override (inside group items):
#   - name: review
#     file_path: tasks/review.md
#     chain: false                    # this task does NOT receive the retrospective (overrides task.chain)
```

### Validation rules

- `agent.role` ∈ `{queen, specialist, worker, scout}`
- `agent.seniority` ∈ `{lead, senior, junior}`
- Each agent needs `prompt` OR `prompt_path`.
- `agent.depends_on` must be a list of known agent names. Self-deps and cycles are rejected at load time. `null` and `[]` are equivalent (no deps). Duplicates are deduped.
- `task.run_mode` ∈ `{sequential, parallel}`.
- `mcp.servers[*].transport` ∈ `{stdio, http, sse}`. stdio requires `command`, http/sse require `url`. Duplicate names are rejected.
- `task.group` may be a list or a dict — both are accepted.
- Unknown keys inside `group` items are ignored (forward-compatible).

---

## Multi-task mode

### Explicit group

```yaml
task:
  multi: true
  run_mode: sequential
  group:
    - name: backend
      file_path: tasks/backend.md
    - name: frontend
      file_path: tasks/frontend.md
    - name: e2e
      file_path: tasks/e2e.md
```

Run:

```bash
rufler run
```

rufler spawns a hive for `backend`, waits for its NDJSON log to emit `log ended`, then spawns `frontend`, then `e2e`.

### Parallel group

```yaml
task:
  multi: true
  run_mode: parallel
  group:
    - name: service_a
      file_path: tasks/a.md
    - name: service_b
      file_path: tasks/b.md
```

```bash
rufler run -d            # parallel mode only makes sense detached
```

Each subtask gets its own background supervisor and its own NDJSON log under `.rufler/`.

### AI-decomposed subtasks

```yaml
task:
  multi: true
  decompose: true
  decompose_count: 4
  main: |
    Build a URL shortener: REST API, SQLite storage, CLI client, tests.
```

On `rufler run`, rufler calls `claude -p` with the decomposer prompt, parses the YAML response, writes `.tasks/task_{1..4}.md` and a companion `.tasks/decomposed_tasks.yml`, then runs the group sequentially.

You can override the decomposer prompt inline (`decompose_prompt`) or from a file (`decompose_prompt_path`). Templates may reference `{n}` (count) and `{main}` (main task) as placeholders — literal `{` / `}` in the template are safe.

### When to use which mode

| Situation | Recommended mode | Why |
|---|---|---|
| You already wrote the N subtasks as `.md` files and the order / scope matters for review | **mono → single task** OR **explicit `group`** | Reproducible, diffable in PRs, no LLM randomness. You control everything. |
| One focused goal, no real decomposition needed | **mono** (`multi: false`, `main` or `main_path`) | Lowest overhead — one hive, one log, one objective. |
| Hand-written skeleton with clear hand-off between stages (schema → api → tests → ci) | **`group`** + `run_mode: sequential` | Subsequent subtasks read prior agents' output from shared memory; order is load-bearing. |
| Independent subtasks that can race (`service_a`, `service_b`, `docs`) | **`group`** + `run_mode: parallel` + `rufler run -d` | Each subtask gets its own supervisor and NDJSON log; you only need `-d` because parallel needs detached supervisors. |
| Big fuzzy goal, you want Claude to propose a split before executing | **`decompose: true`** | rufler calls `claude -p` with the decomposer prompt, writes `.tasks/task_{1..N}.md` + a companion yml, then runs them as a group. |
| `decompose: true` but you want to pin the split style (always "schema → api → tests → ci") | **`decompose_prompt`** (inline) or **`decompose_prompt_path`** (md file) with `{n}` / `{main}` placeholders | The default prompt is generic; a custom template makes decomposition deterministic across runs of the same project. |
| You want to **review** the generated split before running the swarm | `decompose: true` + `rufler run --dry-run` | Decomposition runs, files are written, plan banner is printed, then rufler stops before spawning. Inspect `.tasks/*.md`, then re-run without `--dry-run`. |
| Subtasks are independent enough for parallel but you don't want to hand-write them | `decompose: true` + `run_mode: parallel` + `rufler run -d` | Works fine — decomposer writes the group, supervisor spawns them in parallel. |
| A previous run crashed halfway through a group | **`group`** (or already-decomposed `.tasks/decomposed_tasks.yml`) + re-run | Rufler's composed objective tells agents to probe shared memory for prior progress, so the swarm picks up where it left off instead of restarting from zero. |
| You need a soft DAG between agents inside each subtask (architect → coder → tester) | Either mode + `depends_on:` on the agents | Orthogonal to `group` vs `decompose` — `depends_on` injects GATE/HANDOFF clauses into the objective regardless of how the subtask came to be. |
| CI or one-shot experiment where reproducibility matters more than convenience | **`group`** with committed `.md` files | Avoid `decompose` — every run would re-ask the LLM and risk a different split. |

Rule of thumb: **write `group` when the split is part of your spec; use `decompose` when the split itself is the question you're asking Claude.**

### Deep Think (project analysis)

By default, `decompose: true` splits a task "blindly" — the decomposer LLM doesn't see the project's actual files, structure, or existing code. This leads to subtasks that may duplicate existing work, target wrong files, or miss dependencies.

**`task.deep_think: true`** fixes this by adding a read-only analysis phase before everything else:

```
0a. deep think   → claude -p --model opus (Read, Glob, Grep, Bash only) → .rufler/analysis.md
0b. decompose    → claude -p --model sonnet (analysis injected as context) → subtasks
1-N. execute     → hive spawn (analysis injected into every objective)
```

```yaml
task:
  main: "Add /users CRUD endpoint to the REST API"
  deep_think: true
  deep_think_model: opus         # deep reasoning model (default opus)
  deep_think_effort: max         # thinking effort (low/medium/high/max)
  deep_think_budget: 1.50       # optional max spend in USD
  decompose: true
  decompose_count: 4
  decompose_effort: high        # thinking effort for the decomposer
```

The analysis agent scans the project and writes a structured report:

1. **Project Overview** — language, framework, dependencies
2. **Directory Structure** — layout with descriptions
3. **Existing Implementation** — what's already built (with file paths)
4. **Gaps & Missing Pieces** — what's NOT implemented relative to the task
5. **Dependencies & Impact** — which files will be affected, risk areas
6. **Recommended Approach** — step-by-step plan informed by the analysis

This report is then:
- **Injected into the decomposer prompt** (when `decompose: true`) — so subtasks are project-aware
- **Injected into every agent objective** — so agents don't waste time re-discovering what exists

**Caching.** The analysis is saved to `deep_think_output` (default `.rufler/analysis.md`). On re-run, the cached file is reused. Pass `--new` to force a fresh scan.

**Model selection.** Use `deep_think_model` to pick the model for analysis — both aliases (`opus`, `sonnet`) and full model IDs (`claude-opus-4-6`) are accepted. Opus is recommended for thorough reasoning; Sonnet is faster and cheaper for simpler projects.

**Effort & budget.** `deep_think_effort` controls reasoning depth (`low`/`medium`/`high`/`max`; default `max`). `deep_think_budget` sets an optional USD spending cap for the analysis session. Similarly, `decompose_effort` controls the decomposer's reasoning depth (default `high`).

**MCP access.** By default deep think has access to **all tools** — file tools, Bash, and every registered MCP server. To restrict the session to specific tools only, set `deep_think_allowed_tools`:

```yaml
task:
  deep_think: true
  deep_think_allowed_tools: "Read,Glob,Grep,Bash"  # lock down to read-only file tools
```

When set, this passes `--allowedTools` to `claude -p`, limiting the session to the listed tools. When omitted (default), no restriction is applied — deep think can use everything available, including MCP.

**Without decompose.** Deep Think is also useful in mono mode — the analysis is injected directly into the single objective:

```yaml
task:
  main_path: tasks/feature.md
  deep_think: true
  deep_think_model: sonnet      # cheaper for simple analysis
  # decompose: false (default)
```

### Task chaining

In sequential multi-task mode each subtask runs in its own `claude -p` session — a fresh process with no memory of the previous session's conversation. By default the only link between tasks is the shared memory namespace (agents are _asked_ to read/write checkpoints, but it's a soft contract).

**`task.chain: true`** makes the link explicit and hard: after each task completes, rufler compresses its body and report into a compact text block and injects it into the next task's prompt as a `PREVIOUS TASK RETROSPECTIVE` section. The new Claude session sees exactly what was asked, what was done, and what the report said — without relying on memory polling.

```yaml
task:
  multi: true
  run_mode: sequential
  chain: true
  chain_max_tokens: 2000        # word budget for the retrospective (default 2000)
  chain_include_report: true    # include per-task report in the retrospective (default true)
  group:
    - name: design
      file_path: tasks/design.md
    - name: implement
      file_path: tasks/implement.md
    - name: review
      file_path: tasks/review.md
      chain: false              # this task does NOT receive the retrospective
```

**How compression works.** The compressor is deterministic (no AI call) and fast:

1. Strips HTML tags, horizontal rules, bold/italic markers
2. Flattens fenced code blocks into one-line summaries (`[code:python: def hello()…]`)
3. Downgrades markdown headers to `[Header]` form
4. Collapses blank lines and whitespace
5. Truncates to `chain_max_tokens` words

The token budget is split between the task body (~2/3) and the report (~1/3). If `chain_include_report` is false or no report exists, the full budget goes to the body.

**Per-task override.** Set `chain: false` on any group item to skip retrospective injection for that specific task, or `chain: true` on a single item when the global `task.chain` is false.

**When to use chaining:**

| Scenario | Use chain? |
|---|---|
| Tasks build on each other (design → implement → review) | Yes |
| Tasks are independent (service_a, service_b) | No — use parallel mode instead |
| You already rely on shared memory and it works well | Optional — chain adds redundancy |
| Prompt budget is tight (very long task bodies + many tasks) | Tune `chain_max_tokens` down |

### Iterative refinement

A single `rufler run` normally executes the pipeline once: deep_think → decompose → execute → report. For complex tasks that benefit from a "polish pass" — filling gaps, hardening tests, fixing what broke in review — set `task.iterations: N` and the **entire pipeline repeats N times**. Each iteration's accumulated reports are fed into the next iteration's deep_think so the analyzer sees what's already done and targets the remaining gaps instead of restarting blind.

```yaml
task:
  main: "Build a production-ready X with tests and graceful shutdown"
  deep_think: true
  multi: true
  decompose: true
  decompose_count: 4

  iterations: 5                   # run the full cycle up to 5 times
  iteration_scope: full           # full | decompose_only | tasks_only
  iteration_refine: true          # prior reports → next deep_think
  iteration_judge: true           # let a judge agent decide when to stop
  iteration_judge_threshold: 0.9
```

**What each iteration actually does:**

```
iter 1:  deep_think → decompose (4 subtasks) → execute → per-iter report → judge
iter 2:  deep_think (sees iter 1 report) → decompose (4 fresh gap-focused subtasks) → execute → report → judge
iter 3:  ...
```

From iteration 2 onwards, the deep_think prompt is prepended with a `PRIOR ITERATIONS` block containing every per-iteration final report and every per-task report written so far. The framing tells the analyzer: *this is iteration N of M, prior iterations already built X, verify what's actually on disk, and produce a refinement plan — not a fresh analysis.*

#### `iteration_scope` — what regenerates

| Scope | deep_think | decompose | execute |
|---|---|---|---|
| `full` (default) | every iter | every iter | every iter |
| `decompose_only` | iter 1 only | every iter | every iter |
| `tasks_only` | iter 1 only | iter 1 only | every iter |

`full` is the usual choice when you want the analyzer to re-scan the repo each pass. Use `decompose_only` when the project analysis is stable (short run, no architectural drift between iterations) but you want Claude to re-propose subtasks after seeing what was built. Use `tasks_only` when you've hand-written the `group` and just want repeated execution passes over the same subtasks (useful for flaky / long-running tasks that sometimes finish incomplete).

#### Judge agent — automatic early stopping

With `iteration_judge: true`, rufler spawns a short **read-only** `claude -p` session at every iteration boundary. The judge reads the original TASK, the current state of the repo (Read/Glob/Grep/Bash), and every accumulated report, then emits a strict JSON verdict:

```json
{
  "verdict": "done",
  "score": 0.92,
  "reasoning": "All acceptance criteria met; tests pass at src/api/users_test.go:48",
  "remaining_work": ""
}
```

The loop breaks when `verdict == "done"` AND `score >= iteration_judge_threshold`. Otherwise it continues to the next iteration.

Failure modes are handled conservatively:

- Judge claude missing, timeout, subprocess error, non-zero rc → **loop continues** (fail open, don't stop thinking you're done when you aren't)
- JSON parse failure → verdict derived from score if present, else "continue"
- Score clamped to `[0.0, 1.0]`
- Every verdict is saved to `.rufler/iter-NN/judge.md` with full reasoning, raw output, and parse diagnostics

Custom judge prompt via `iteration_judge_prompt` (inline) or `iteration_judge_prompt_path` (file). Placeholders: `{main_task}`, `{project}`, `{iter_num}`, `{total_iters}`, `{threshold}`, `{accumulated_reports}`.

Fallback for runs without a judge: `iteration_stop_on_success: true` breaks the loop as soon as every real task in an iteration returned `rc=0`. Cheaper than a judge but will stop early even when the code technically runs but doesn't actually satisfy the task.

#### On-disk layout for `iterations > 1`

Per-iteration artifacts are namespaced under `.rufler/iter-NN/` so no file is ever overwritten between passes:

```
.rufler/
├── iter-01/
│   ├── analysis.md              # deep_think output for this iteration
│   ├── decomposed_tasks.yml     # companion yml for iter 1's subtasks
│   ├── tasks/task_1.md          # decomposer-written subtask bodies
│   ├── tasks/task_2.md
│   ├── reports/task_1.md        # per-task completion reports
│   ├── reports/task_2.md
│   ├── report.md                # per-iter final report
│   └── judge.md                 # judge verdict + reasoning + raw output
├── iter-02/
│   └── …same shape…
├── iter-03/
│   └── …
├── run.i01-task_1.log           # claude stdout per task (iter-prefixed)
├── run.i02-task_1.log           # iter 2's task_1 → separate log, no overwrite
├── run.judge.i01.log            # judge-agent claude stdout
└── run.judge.i02.log
```

Task names in the registry are iter-prefixed (`i01-task_1`, `i02-task_1`, …) so `rufler tasks` and `rufler follow` show every iteration's work distinctly. Slots are global across iterations.

When `iterations: 1` (the default), none of this namespacing kicks in — paths, log names, and registry entries are identical to pre-iteration behaviour. Single-pass runs see zero change.

#### Caveats and cost control

- **Cost scales linearly.** Five iterations = 5× deep_think + 5× decompose + 5× full swarm spawn + 5× report + 5× judge. On Opus this can easily be hours and tens of dollars — run `rufler run --dry-run` first, and keep the judge enabled for early exit.
- **Foreground parallel is still foreground.** `run_mode: parallel` with `iterations > 1` runs each iteration's parallel batch (which in foreground blocks sequentially anyway) and waits for the batch to finish before moving on. For true parallelism across tasks within an iteration, use `rufler run -d`.
- **Resume is iter-1 only.** The resume logic (`find_resumable_run`, `completed_task_names`) applies on iteration 1. From iteration 2 onwards, every task in the new iteration runs unconditionally — that's the point of refinement. Use `--new` to also force iter 1 to start clean.
- **No per-iter `Ctrl+C` semantics yet.** Ctrl+C / `rufler stop` kill everything, including a potentially healthy iteration that was about to finish. A graceful "stop after this iteration" mechanism isn't implemented — tell the judge to set a tight threshold, or edit `iterations` before the next run.

#### When to use iterations

| Situation | Use iterations? |
|---|---|
| One focused task, you trust a single pass | No — `iterations: 1` |
| "Build X and polish it to prod quality" | Yes — 3-5 iterations with judge |
| Fixing flaky output by retrying the same group | Yes — `iteration_scope: tasks_only`, no judge needed |
| Analysis is expensive and the repo is stable | `iteration_scope: decompose_only` + judge |
| Budget-sensitive | Either skip iterations or rely heavily on `iteration_judge_threshold: 0.85` to exit early |
| CI / reproducibility-critical | No — iterations introduce judge / LLM variance between runs |

---

## Runs, projects and statuses

rufler keeps a central docker-like registry at `~/.rufler/registry.json` so you can manage every run from any directory.

### The run lifecycle

```bash
rufler run             # foreground: Ctrl+C kills the swarm, exits 130
rufler run -d          # detached: supervisor survives terminal close
# => rufler id: a1b2c3d4
```

Every `rufler run` gets an **8-char hex id** (like `a1b2c3d4`). The id is printed at startup and is how every other command addresses the run.

### `rufler ps` — list runs

```bash
rufler ps                          # currently running (like `docker ps`)
rufler ps -a                       # everything ever run (like `docker ps -a`)
rufler ps a1b2                     # detail view of one run by id prefix
rufler ps --prune                  # drop entries for projects whose base dir is gone
rufler ps --prune-older-than-days 7
```

Columns: `ID | PROJECT | MODE | STATUS | TASKS | CREATED | LAST RUN | BASE DIR`.

`STATUS` is docker-style — `Up 2m`, `Exited (0) 1h ago`, `Failed (2) 23h ago`, `Stopped 5m ago`, `Dead`. `CREATED` is relative time since the run started; `LAST RUN` is relative time since it last changed state (finished_at for completed runs, started_at for live ones).

### Run statuses

| Status | Meaning |
|---|---|
| `running` | At least one registered pid is alive |
| `exited` | Process ended cleanly with `rc=0` |
| `failed (N)` | Process ended with non-zero exit code |
| `stopped` | User ended the run cleanly (`rufler stop` or Ctrl+C on foreground) |
| `dead` | Supervisor vanished without leaving a finish marker (e.g. killed with `-9`, crashed, power loss) |

Status is recomputed on every read from three signals: pid liveness, the `log ended rc=N` marker in the NDJSON log tail, and the `finished_at` timestamp written by rufler itself. Nothing is cached — what you see is always the current truth.

### `rufler projects` — project rollup

```bash
rufler projects
```

Columns: `PROJECT | LAST RUN | AGE | RUNS | BASE DIR`. One row per unique `project.name`, sorted by last launch time. This rollup is stored separately from individual runs and **survives `rufler rm`, `rufler ps --prune` and age-based pruning** — so you can always see when a project was last touched, even if all its individual run entries have been cleaned up.

### `rufler rm` — cleanup

```bash
rufler rm a1b2 e5f6            # remove specific runs
rufler rm --all-finished       # remove everything that's not running
rufler rm --older-than-days 30 # bulk prune
```

`rm` refuses to delete runs that are currently `running` — use `rufler stop <id>` first.

### `rufler tasks` — task tracking

```bash
rufler tasks                       # tasks of the latest run in cwd
rufler tasks a1b2                  # tasks of a specific run
rufler tasks -a                    # all tasks across all runs
rufler tasks --status running      # filter by status
rufler tasks a1b2c3d4.01 -v       # detailed card for one task
```

**Delete tasks:**

```bash
rufler tasks a1b2c3d4.05 --rm         # remove one task from the registry
rufler tasks a1b2c3d4 --rm            # remove ALL tasks in that run
rufler tasks --rm-all                  # remove all tasks across all runs in cwd
rufler tasks --rm-all --rm-files       # also delete .rufler/tasks/*.md and log files
rufler tasks a1b2c3d4 --rm --rm-files  # delete one run's tasks + files
```

`--rm` / `--rm-all` only touch the registry bookkeeping by default. Add `--rm-files` to also delete on-disk task files (`.rufler/tasks/*.md`) and their logs.

Table columns: `TASK ID | SLOT | NAME | STATUS | SOURCE | STARTED | DURATION | TOKENS | LOG`.

Every task gets a **sub-id** in the format `<run_id>.<slot>` (e.g. `a1b2c3d4.01`). Status is derived lazily from log markers + pid liveness + registry data — never cached.

| Task status | Meaning |
|-------------|---------|
| `queued` | Run is still alive, task hasn't started yet |
| `running` | `task_start` marker found, no `task_end`, pid alive |
| `exited` | `task_end` with `rc=0` |
| `failed` | `task_end` with `rc != 0` |
| `stopped` | Run stopped before task finished |
| `skipped` | Run finished but task never started |

The **detail view** (`-v`) shows full metadata, per-task token breakdown (input / output / cache_read / cache_creation), and the last 10 log events for that task.

### `rufler tokens --by-task` — per-task token breakdown

```bash
rufler tokens --by-task              # per-task tokens for the latest run
rufler tokens --by-task a1b2c3d4     # per-task tokens for a specific run
```

### Task resume on restart

When you stop a run mid-way and restart:

```bash
rufler run -d            # starts 4 tasks, task_1 and task_2 complete
# Ctrl+C / rufler stop
rufler run -d            # auto-resumes from task_3
# => resuming: skipping 2 completed tasks, starting from task_3
```

Resume works at two levels:

1. **Decomposed task files** — if `.rufler/tasks/decomposed_tasks.yml` already exists, rufler loads tasks from it instead of calling claude again. No wasted tokens on re-decomposition.
2. **Completed tasks** — rufler finds the previous run for this project/directory in the registry, checks which tasks finished with `rc=0`, and skips them. Comparison is by task **name** (not slot), so adding/removing tasks in the yml correctly triggers re-execution.

Flags:
- `rufler run` — resume by default
- `rufler run --new` — ignore previous progress, re-decompose, start all tasks from scratch
- `rufler run --from 3` — skip tasks 1-2 explicitly, start from slot 3

### `rufler follow` — live TUI dashboard

```bash
rufler follow              # auto-picks the running (or latest) run in cwd
rufler follow a1b2         # follow a specific run by id
```

Four-panel dashboard:

```
╭─ rufler follow ─────────────────────── [running] ── 00:04:23 ─╮
│ model: claude-opus-4-6    swarm: hive-17762684    workers: 4   │
╰───────────────────────────────────────────────────────────────╯
╭─ Tasks  2/4 ────────────╮╭─ Session ─────────────────────────╮
│ ✓ task_1     done  1m12s││ model   claude-opus-4-6           │
│ ▶ task_2     running 52s││ tokens  in=98 out=829             │
│   task_3     queued     ││ turns   14                        │
│   task_4     queued     ││ last    Write                     │
╰─────────────────────────╯╰───────────────────────────────────╯
╭─ Conversation (task_2) ──────────────────────────────────────╮
│ 14:23  think  Planning the API routes — need UserList...     │
│ 14:23  text   I'll create the handler files with types...    │
│ 14:24  tool   Write(src/api/routes.go)                       │
│ 14:24  result File created successfully                      │
╰──────────────────────────────────────────────────────────────╯
╭─ Log ────────────────────────────────────────────────────────╮
│ 14:22  OK    task_end task_1 done                            │
│ 14:22  INFO  task_start task_2                               │
│ 14:23  INFO  session init (claude-opus-4-6)                  │
╰──────────────────────────────────────────────────────────────╯
```

- **Tasks** — task list with status icons (✓ ▶ ○ ✗), duration, per-task tokens
- **Session** — model, token totals, turns, last tool
- **Conversation** — AI stream for the active task: thinking (3-5 lines), text (full), tool calls with params, tool results
- **Log** — system events: task markers, hooks, errors, rate limits

In multi-task mode, follow tails **all per-task log files** simultaneously (not just the primary `run.log`).

### Soft resume via shared memory (periodic checkpointing)

In addition to the task-level resume above, every composed objective ends with two auto-injected sections that force agents to continuously persist state to AgentDB:

**`# RESUME AWARENESS`** — tells agents, before they start, to search the shared memory namespace (from `memory.namespace`) for prior progress using standard keys (`checkpoint:latest`, `progress`, `decisions`, `blockers`, `completed`, `last_step`). If they find state from a prior interrupted run, they continue from there instead of redoing work.

**`# CHECKPOINT DISCIPLINE`** — tells agents to write a checkpoint to shared memory:

- **Every `memory.checkpoint_interval_minutes` minutes** of wall-clock work (default: 5, `0` disables the timer).
- **Immediately after** every file write, test run, build, sub-task, or design decision.

The objective prescribes exact key names so recovery is deterministic:

| Key | Contents |
|---|---|
| `checkpoint:latest` | Rolling pointer — compact JSON of `{current_step, done_steps[], next_step, open_questions[], last_ts}` |
| `checkpoint:<unix_ts>` | Timestamped snapshot for history |
| `progress` | One-line human summary: what is done, what is next |
| `decisions` | Append-only list of design decisions |
| `blockers` | Anything stuck on; cleared when unstuck |

So an interrupted run leaves behind: its NDJSON log, any files it wrote to disk, and a constantly-refreshed checkpoint in AgentDB memory. The next `rufler run` picks up that memory state and reconstructs context from it. Rule of thumb baked into the prompt: *if you just spent >2 minutes on something you could not reconstruct from the repo alone, it MUST go into memory before your next tool call.*

---

## Inspecting agents

`rufler agents` reads the flow file and prints one row per declared agent — name, type, role, seniority, and a single-line 150-character preview of the prompt (whether it was inline or loaded from `prompt_path`).

```bash
# Agents from ./rufler_flow.yml in the current directory
rufler agents

# Agents from a specific past run (looks up its flow_file via the registry)
rufler agents a1b2c3d4

# Print full prompt bodies instead of the 150-char preview
rufler agents --full
```

Use this to sanity-check that every agent's `prompt_path` actually resolves and to eyeball the prompts you wired up before launching `rufler run`.

---

## Agent dependencies (soft DAG)

You can declare that an agent must wait for other agents to finish before it starts:

```yaml
agents:
  - name: architect
    type: system-architect
    role: specialist
    seniority: lead
    prompt: Design the system.

  - name: coder
    type: coder
    role: worker
    seniority: senior
    prompt: Implement the design.
    depends_on: [architect]

  - name: qa
    type: tester
    role: worker
    seniority: junior
    prompt: Write tests.
    depends_on: [coder]
```

This builds a soft DAG: `architect → coder → qa`.

### How it's enforced

rufler does **not** spawn agents in separate processes — the whole team lives inside one hive-mind. Instead, `build_objective` injects two prompt sections per agent:

- **`### GATE`** — appended to every agent that has `depends_on`. Tells the agent: before doing **any** work, read these keys from the shared memory namespace; if any are missing, poll `memory_search` every ~30s; never bypass a gate even if you "know what to do":
  - `instructions:<task>:<upstream>-><self>` — the work brief from upstream
  - `approval:<task>:<upstream>-><self>` — must equal `approved`
- **`### HANDOFF`** — appended to every agent that has downstream agents waiting on it. Tells the agent it must publish a brief and an `approved` flag for each downstream agent, in that order, before its own work is considered done. If it must reject a downstream, it writes `value='rejected: <reason>'`.

Memory keys are scoped per task name (`<task>` segment), so multi-task runs sharing the same memory namespace can't pick up each other's briefs or approvals.

### What's validated at load time

- `depends_on` referencing an unknown agent name → `ValueError`.
- An agent depending on itself → `ValueError`.
- Cycles (e.g. `a → b → a`) → `ValueError` printing the cycle path.
- `depends_on: null` is normalised to `[]`. Duplicates are deduped while preserving order.

### Caveats

This is **soft** enforcement — a contract written in the prompt, not a process scheduler. Claude Code with `autonomous: true` and the checkpoint discipline section above respects it well in practice, but there is no OS-level guarantee that a downstream agent won't peek ahead. If you need hard ordering, split the work into multi-task `sequential` mode — each task runs in its own `claude -p` process, and rufler only spawns the next task after the previous task's NDJSON log emits `log ended`.

You can verify the dependency graph at any time:

```bash
rufler agents             # DEPENDS ON column shows each agent's upstreams
rufler run --dry-run      # prints the full composed objective with GATE/HANDOFF blocks
```

---

## Global skills (`.claude/skills/`)

Claude Code reads skill definitions from `.claude/skills/<name>/SKILL.md` at session start. rufler knows about three sources of skills and lets you mix them in one yml block:

| Source | yml field | Where it comes from |
|---|---|---|
| ruflo bundled packs | `packs:` / `all:` | `core`, `agentdb`, `github`, `flowNexus`, `browser`, `v3`, `dualMode` packs shipped inside the ruflo npm package |
| ruflo standalone skills | `extra:` | Individual skill dir names under ruflo's bundled `.claude/skills/` source tree |
| **`custom:` (unified)** | `custom:` | Local paths **and** [skills.sh](https://skills.sh) installs — one list, path-first resolution |

```yaml
skills:
  enabled: true
  clean: false              # false (default) = keep ruflo init's ~30 defaults
                            # true            = wipe non-symlinked skill dirs after
                            #                   `ruflo init` so yml is the single
                            #                   source of truth
  all: false                # true = every pack (overrides `packs`)
  packs: []                 # subset of {core, agentdb, github, flowNexus, browser, v3, dualMode}
  extra: []                 # individual skill dir names from ruflo's bundled source
                            # tree (e.g. ["my-bundled-skill"])
  custom:                   # Unified list — local paths + skills.sh installs.
                            # rufler tries the filesystem first; unknown strings
                            # fall back to `npx skills add <source>`. See below.
    - ./skills/my-skill                    # local dir
    - ~/shared/claude-skills/reviewer      # local dir
    - npx skills add https://github.com/samber/cc-skills-golang --skill golang-error-handling
    - source: vercel-labs/skills           # dict form (explicit skills.sh)
      skill: azure-ai
```

### The default behavior (empty yml `skills:` section)

`rufler init` creates a flow file where `skills:` is enabled but empty: `packs: []`, `extra: []`, `custom: []`, `clean: false`. On `rufler run` / `rufler build`, this means:

- `ruflo init --force` plants its standard bundle of ~30 default skills (core Claude Code + ruflo skills).
- rufler **adds nothing on top** (no packs declared, no extras, no custom).
- `clean: false` tells rufler **not** to touch what ruflo just planted.

Net result: a fresh project gets ruflo's full default skill set out of the box, and you only need to edit `skills:` if you want to restrict the set (`clean: true`) or add your own (`custom:`) or pull in extra packs (`packs:`).

### How each field is resolved

- **`packs`** — `core`, `agentdb`, `github`, `v3` are proxied to `ruflo init skills --<pack> --force` (reuses ruflo's own installer). `flowNexus`, `browser`, `dualMode` are copied directly from ruflo's bundled `.claude/skills/` source tree.
- **`extra`** — individual skill directory names under ruflo's bundled source tree (copied, not symlinked).
- **`custom`** — unified list mixing local paths and [skills.sh](https://skills.sh) installs. Each string entry is resolved **path-first**: absolute → used as-is, `~` → expanded, relative → resolved against the directory containing the yml file. If the resolved path is an existing directory it's copied into `<project>/.claude/skills/<basename>`. If the string isn't a directory, rufler falls back to a skills.sh install via `npx skills add <source>`. You can also embed a full `npx skills add …` command line or a dict (`{source, skill?, agent?, copy?}`) — see the next section.
- **Where ruflo is found (for `extra` and manual packs)** — `$RUFLER_RUFLO_BIN` → local `node_modules/.bin/ruflo` → `$PATH` → npm global bin. If ruflo is only reachable via `npx` (no stable on-disk path), `extra` and manual packs print a warning and skip — install ruflo locally or globally for those to work. `custom` is unaffected — it doesn't need ruflo's source tree.

### `clean: true` — yml as the single source of truth

By default, `ruflo init` always plants ~30 default skills regardless of yml. If you want the installed skill set to exactly match what your yml declares, set `clean: true`. rufler will then, after `ruflo init` runs and before installing from yml, wipe every non-symlinked directory under `<project>/.claude/skills/`. Symlinks (e.g. ones you created manually for dev work) are preserved.

```yaml
skills:
  enabled: true
  clean: true             # wipe ruflo's defaults
  packs: [core]           # ...and install only what's listed here
  custom:
    - ./skills/my-skill
```

### Native [skills.sh](https://skills.sh) integration (inside `custom:`)

[skills.sh](https://skills.sh) is an open ecosystem of reusable agent skills hosted on GitHub and installed via the `skills` CLI (`npx skills add <source>`). rufler has native support — drop the repo reference straight into `custom:` and rufler runs the CLI for you before the swarm launches. No second section to maintain.

rufler resolves each `custom:` string **path-first**: if the string points at a real directory on disk, it's copied locally; otherwise it's treated as a skills.sh install. Three forms are accepted per entry — pick whichever is most convenient:

```yaml
skills:
  enabled: true
  custom:
    # Local paths (absolute / ~ / relative to this yml)
    - ./skills/my-skill
    - ~/shared/claude-skills/reviewer

    # 1. Pasted command — copy the exact line from https://skills.sh and
    #    drop it in. rufler parses `npx skills add …` / `skills add …`
    #    with the real CLI flags.
    - npx skills add https://github.com/samber/cc-skills-golang --skill golang-error-handling
    - skills add owner/repo -s azure-ai -a claude-code --no-copy

    # 2. Dict form — most explicit, good for code-reviewed configs.
    - source: vercel-labs/skills
      skill: azure-ai         # passed as `-s azure-ai` to the skills CLI
      agent: claude-code      # target agent (default: claude-code)
      copy: true              # pass --copy instead of symlink (default: true)

    # 3. Bare string with no filesystem match — falls back to skills.sh
    #    as if you'd written `npx skills add owner/repo`. Use the dict
    #    or command form for explicit skills.sh installs to avoid
    #    accidental ambiguity with local paths.
```

> **Legacy**: earlier versions of rufler had a separate `skills_sh:` section. It's still accepted on load (entries are transparently merged into `custom:`), but new configs should use `custom:` exclusively.

Recognised flags in the pasted-command form: `-s` / `--skill` / `--skill=…`, `-a` / `--agent` / `--agent=…`, `--copy`, `--no-copy`, `-y` / `--yes` (tolerated — rufler always passes it anyway). A leading `npx` is stripped. `-g` / `--global` is rejected at load time because rufler installs skills per-project only.

Under the hood, each entry becomes:

```bash
cd <project> && npx -y skills add <source> -a <agent> -y [--copy] [-s <skill>]
```

Installs land in `<project>/.claude/skills/<skill-name>/` — the same directory ruflo/rufler use for everything else, so Claude Code picks them up on session start alongside packs/extras/custom.

#### Why `--copy` is the default

skills.sh supports both symlink and copy installs. rufler defaults to `--copy` because:

- Symlinks point at a shared cache outside the project and break when the project is cloned, zipped, or committed.
- Copies are self-contained, diffable, and review-friendly.
- You can opt into symlinks per entry with `copy: false`.

#### Preflight check — fail fast

When any entry in `custom:` resolves to a skills.sh install, `rufler check` adds a `skills.sh` row that probes the CLI:

```bash
rufler check
```

```
rufler dependency check
┌───────────┬──────┬────────┬────────────────────────────┐
│ Tool      │ OK   │ Source │ Version / Hint             │
├───────────┼──────┼────────┼────────────────────────────┤
│ node      │ OK   │ -      │ v20.x                      │
│ claude    │ OK   │ path   │ 1.0.x                      │
│ ruflo     │ OK   │ local  │ 3.5.x                      │
│ skills.sh │ OK   │ npx    │ skills CLI reachable       │
└───────────┴──────┴────────┴────────────────────────────┘
```

If `npx` is missing or the `skills` package won't resolve, this check fails before anything touches your project — no more finding out mid-run that a skill didn't install.

#### Post-install verification — SKILL.md required

Every directory that appears under `.claude/skills/` as a result of a skills.sh install is inspected for a `SKILL.md` file. If one is missing, rufler warns:

```
skills.sh: installed dirs with NO SKILL.md — Claude Code may not discover them: some-dir
```

This catches broken or partial packages in the upstream repo before the swarm launches with a half-installed skill.

#### Validation

- `source` is required and must be a non-empty string.
- `skill`, `agent`, `copy` are optional. Unknown fields in a dict entry → `ValueError` at load time.
- Entries are deduped by `(source, skill)` pair, preserving order.
- `npx skills add` failures (non-zero exit, timeout 180s, `npx` missing) are surfaced as warnings and the run continues — use `rufler check` to catch them upfront.

### `rufler skills` — inspect and reset

```bash
# Show what's currently installed in <project>/.claude/skills/
# plus a snapshot of the yml config (enabled / clean / packs / extra / custom)
rufler skills

# List every skill pack / standalone skill available in ruflo's bundled source
rufler skills --available

# Wipe every non-symlinked dir under <project>/.claude/skills/
# (prompts for confirmation; -y / --yes to skip the prompt)
rufler skills --delete
rufler skills --delete -y
```

Use `rufler skills --delete` when you've been iterating on your yml and want a clean slate before `rufler build` / `rufler run`.

### Validation

- Unknown pack name → `ValueError` with the list of known packs.
- `packs`, `extra` must be lists of strings; blank entries are dropped; duplicates are deduped while preserving order.
- `custom` is a mixed list: strings (local path / skills.sh shorthand / pasted `npx skills add …` command) OR dicts (`{source, skill?, agent?, copy?}`). Unknown dict fields → `ValueError`. Local-path strings are deduped by resolved path; skills.sh entries are deduped by `(source, skill)`. Order is preserved.
- `skills.enabled: false` skips the install step entirely (including `clean`).
- Omitting the section defaults to `enabled=true`, `clean=false`, empty lists — i.e. whatever `ruflo init` installs, nothing added, nothing removed.

### Caveat: skills are session-global

Claude Code discovers skills at session start — all agents in the hive-mind see the same `.claude/skills/` directory. rufler cannot bind a specific skill to a specific agent at the process level; per-agent scoping would have to be enforced in each agent's prompt.

---

## Token usage tracking

rufler parses every NDJSON run log it writes, sums Anthropic API token usage across all `claude` assistant turns, and persists the totals in two places:

1. **Per run** — `RunEntry` carries `input_tokens / output_tokens / cache_read / cache_creation`. Refreshed automatically when a foreground run finishes and on `rufler stop`.
2. **Per project** — `ProjectEntry` carries cumulative `total_input_tokens / total_output_tokens / total_cache_read / total_cache_creation`. **These survive `rufler rm` and `--prune`**, so you keep your project's lifetime token spend even after individual run entries are gone.

Updates are idempotent: re-running token accounting on the same log never double-counts the rollup (only the delta vs the previously-recorded entry total is added).

### `rufler tokens`

```bash
# Per-project rollup + grand total across all projects
rufler tokens

# Detailed breakdown of one run
rufler tokens a1b2c3d4

# Force re-parse of run logs from disk (slower, accurate)
rufler tokens --rescan
rufler tokens a1b2c3d4 --rescan
```

Without an id you get a per-project table and a grand total:

```
        token usage by project
┏━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ PROJECT    ┃  INPUT ┃ OUTPUT ┃ CACHE READ ┃ CACHE CREATION ┃   TOTAL ┃
┡━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ my-app     │  3,210 │  1,845 │   142,330  │          1,200 │ 148,585 │
│ scraper    │    980 │    412 │    18,422  │            210 │  20,024 │
└────────────┴────────┴────────┴────────────┴────────────────┴─────────┘
grand total: 168,609 (168.6K) — in=4.2K out=2.3K cache_read=160.8K cache_creation=1.4K
```

With an id you get the breakdown for that single run, with raw counts. `rufler ps` now also shows a `TOKENS` column for every run, and `rufler projects` shows project-cumulative totals so you can spot which projects are the most expensive at a glance.

### How counts are derived

The parser walks NDJSON log files, looking for `src=claude, type=assistant` records. Claude stream-json emits **multiple `assistant` events per turn** (one per content block), all sharing the same `message.id`. The parser deduplicates by `message.id`, keeping only the last event per turn.

Token semantics:
- `input_tokens` / `output_tokens` — **per-turn deltas** → summed across turns
- `cache_read_input_tokens` / `cache_creation_input_tokens` — **session-cumulative** → max taken

Multi-task runs scan per-task log files automatically (with deduplication if a task points at the same log). `rufler tasks` uses byte-range slicing (`task_start.offset` → `task_end.offset`) to attribute tokens to individual tasks within a shared sequential log. Missing logs contribute 0.

---

## Background runs and the NDJSON log

rufler always writes an NDJSON run log. In foreground mode it uses `logwriter --tee` (streams to your terminal AND the log). In `-d` mode it spawns a detached supervisor:

```bash
rufler run -d
# => PID 12345, log: .rufler/run.log
```

Every line in the log is a JSON object:

```json
{"ts": 1776190000.12, "src": "rufler", "level": "info", "text": "log started: ruflo hive-mind spawn --count=5 ..."}
{"ts": 1776190001.44, "src": "claude", "type": "assistant", "message": {...}}
{"ts": 1776190002.10, "src": "ruflo", "level": "info", "text": "hive-mind: session initialized"}
{"ts": 1776190500.77, "src": "rufler", "level": "ok", "text": "log ended rc=0 elapsed=500.6s"}
```

- `src=claude` — raw Claude Code stream-json (preserved as-is, envelope added)
- `src=ruflo` — normalized ruflo/stderr output, ANSI + box-drawing stripped, `level` auto-detected
- `src=rufler` — rufler's own supervisor markers (start, end, elapsed, exit code)

Follow live:

```bash
rufler follow               # pretty dashboard
tail -f .rufler/run.log     # raw NDJSON
jq 'select(.level=="error")' .rufler/run.log    # grep errors
```

---

## Examples

The `examples/` directory ships several ready-to-run flows:

| Example | What it demonstrates |
|---------|----------------------|
| `examples/rufler_flow.yml` | Basic Go WebSocket server with a 5-agent team |
| `examples/rust-cli` | Rust CLI project flow |
| `examples/python-fastapi` | FastAPI backend flow |
| `examples/nextjs-saas` | Next.js SaaS project flow |
| `examples/md-prompts` | Prompts loaded from `.md` files instead of inline |
| `examples/multi-task-group` | Explicit multi-task group (backend / frontend / e2e) |
| `examples/multi-task-decompose` | AI-decomposed multi-task flow |
| `examples/autonomous-background` | Detached background run with NDJSON log |

Run any of them:

```bash
cd examples/multi-task-decompose
rufler run --dry-run      # inspect plan first
rufler run -d             # launch detached
rufler follow               # watch it work
```

Or point at a flow from anywhere:

```bash
rufler run ./examples/rust-cli/rufler_flow.yml
```

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RUFLER_RUFLO_BIN` | Absolute path to a `ruflo` binary. Highest priority in resolution. |
| `RUFLER_RUFLO_SPEC` | npm spec for the `npx -y` fallback. Default: `ruflo@latest`. |

---

## How it works (under the hood)

1. **`rufler check`** — resolves `node`, `claude`, `ruflo` (local → PATH → npm global → `npx`) and reports what it found.
2. **`rufler run`** —
   1. Loads and validates `rufler_flow.yml` into typed dataclasses.
   2. If `task.deep_think`, spawns a read-only `claude -p` session that writes project analysis to `.rufler/analysis.md` (or `.rufler/iter-NN/analysis.md` in iteration mode).
   3. If `task.decompose`, calls `claude -p` to decompose `main` into N subtasks and writes `.tasks/*.md` + companion yml.
   4. Writes `.claude/settings.local.json` with `permissions.defaultMode=bypassPermissions` as a safety net.
   5. Runs `ruflo init --force` (unless `--skip-init`), `daemon start`, `memory init`, `swarm init`, `hive-mind init`.
   6. For each task in the group (or the single mono task), composes an objective prompt — project header, task body, all agents sorted lead→senior→junior, autonomy footer.
   7. Spawns `ruflo hive-mind spawn --count=N --role=... --claude --dangerously-skip-permissions=true --objective=<composed>`.
   8. In foreground: streams to terminal via `python -m rufler.logwriter --tee`. In `-d`: detaches via `Popen(start_new_session=True)` with stdio → `/dev/null` and stdout/stderr piped into the supervisor.
   9. Sequential multi-task mode polls the log tail for the `log ended` marker (scanning only bytes added after spawn, to avoid stale markers).
   10. If `task.iterations > 1`, steps 2–9 are wrapped in a loop. Each iteration's reports are collected and injected into the next iteration's deep_think. After every iteration an optional judge agent evaluates stop conditions; the loop breaks early on `verdict=done AND score >= threshold`.
3. **`rufler stop`** — post-task hook + autopilot disable + hive-mind shutdown + daemon stop + session-end.

---

## Development

```bash
git clone https://github.com/lib4u/rufler.git
cd rufler
pip install -e .

# Run from source
python -m rufler.cli --help
python -m rufler.cli run --dry-run --skip-checks
```

Project layout:

```
rufler/
├── rufler/
│   ├── cli.py           # Typer app: run / ps / tasks / logs / follow / stop / rm / tokens
│   ├── config.py        # dataclass schema + YAML loader + objective composer
│   ├── registry.py      # ~/.rufler/registry.json — RunEntry + TaskEntry with fcntl lock
│   ├── runner.py        # thin wrapper around ruflo subcommands
│   ├── checks.py        # node/claude/ruflo resolution
│   ├── decomposer.py    # AI task decomposition via claude -p
│   ├── logwriter.py     # NDJSON supervisor (foreground tee + detached)
│   ├── follow.py        # live 4-panel TUI dashboard (multi-log tailing)
│   ├── task_markers.py  # task_start/task_end NDJSON markers + boundary scanner
│   ├── tokens.py        # per-turn token parser with message.id dedup
│   ├── run_steps.py     # decompose / plan / finalize helpers
│   ├── templates.py     # `rufler init` sample
│   ├── tasks/           # task tracking subpackage
│   │   ├── resolve.py   # status derivation, resume logic, per-task tokens
│   │   ├── display.py   # Rich tables, detail cards, log tail rendering
│   │   ├── deep_think.py # project analysis phase (iter-aware prompt)
│   │   ├── judge.py     # iteration judge — read-only verdict on stop/continue
│   │   └── report.py    # per-task + final report agents
│   ├── skills/          # skill install/display subpackage
│   └── process/         # daemonization, pid management, log paths
├── tests/
│   ├── test_basics.py      # registry, tokens, tasks, resume, CLI
│   ├── test_chain.py       # task chain retrospective compression
│   ├── test_deep_think.py  # deep_think prompt + subprocess wiring
│   └── test_iterations.py  # iteration config, per-iter paths, judge JSON parsing
└── examples/            # ready-to-run rufler_flow.yml files
```

---

## License

MIT.