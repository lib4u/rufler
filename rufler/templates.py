SAMPLE_FLOW_YML = """# rufler_flow.yml — agent orchestration for this project
# Run with:  rufler run

project:
  name: my-project
  description: Brief description of what we're building.

memory:
  backend: hybrid                 # hybrid | sqlite | agentdb
  namespace: my-project
  init: true
  checkpoint_interval_minutes: 5  # agents flush state to memory every N min (0 = only on events)

swarm:
  topology: hierarchical   # hierarchical | hierarchical-mesh | mesh | adaptive
  max_agents: 8
  strategy: specialized    # specialized | balanced | adaptive
  consensus: raft          # raft | byzantine | gossip | crdt | quorum

task:
  # Inline task body:
  main: |
    Describe the main goal here. Be specific:
    - What to build
    - Key requirements
    - Constraints
  # OR point to a markdown file (relative to this yml):
  # main_path: ./TASK.md
  autonomous: true          # enable autopilot persistent completion loop
  max_iterations: 100       # autopilot: max re-engagement iterations (1-1000)
  timeout_minutes: 180      # autopilot: total timeout in minutes (1-1440)

  # Report after each individual task completes (multi-task mode):
  # on_task_complete:
  #   report: true                            # default: true
  #   report_path: .rufler/reports/{task}.md   # {task} replaced with task name
  #   report_prompt: |                        # optional custom prompt
  #     Summarize what this task accomplished.

  # Report after ALL tasks complete:
  # on_complete:
  #   report: true                            # default: true
  #   report_path: .rufler/report.md
  #   report_prompt: |                        # optional custom prompt
  #     Write a final project summary.
  #   # report_prompt_path: ./prompts/final-report.md  # OR load from file

# How to launch Claude Code — set once here, `rufler start` honors it.
# CLI flags (--background / --yolo / --non-interactive) override these.
execution:
  non_interactive: false    # true  → claude -p --output-format stream-json
  yolo: false               # true  → --dangerously-skip-permissions (no approval prompts)
  background: false         # true  → detach from terminal, log to file below
                            #         (background also forces non_interactive + yolo)
  log_file: .rufler/run.log

# Global Claude Code skills installed into .claude/skills/ before the swarm runs.
# Proxied to `ruflo init skills --<pack>` for core/agentdb/github/v3; other
# packs + `extra` are copied from ruflo's bundled source tree.
skills:
  enabled: true
  clean: false              # false = keep ruflo init's ~30 default skills alongside
                            # anything declared here (default). Set true to wipe
                            # non-symlinked skill dirs after `ruflo init` so
                            # .claude/skills/ matches this yml exactly.
  all: false                # true = install every pack (overrides `packs`)
  packs: []                 # core | agentdb | github | flowNexus | browser | v3 | dualMode
  extra: []                 # individual skill dir names under ruflo's .claude/skills/
  custom: []                # Unified list — each entry is one of:
                            #   1. local path     — abs, ~, or relative to this yml,
                            #                       copied to .claude/skills/<basename>
                            #   2. pasted command — full `npx skills add <source> [flags]`
                            #                       line from https://skills.sh (easiest)
                            #   3. dict form      — {source, skill?, agent?, copy?} for
                            #                       explicit control over skills.sh install
                            # Resolution: rufler tries the filesystem first; if the
                            # string isn't an existing directory it falls back to a
                            # skills.sh install via `npx skills add`.
                            # Example:
                            #   custom:
                            #     - ./skills/my-skill                     # local dir
                            #     - ~/shared/claude-skills/awesome        # local dir
                            #     - npx skills add https://github.com/samber/cc-skills-golang --skill golang-error-handling
                            #     - source: vercel-labs/skills            # dict form
                            #       skill: azure-ai
                            #       agent: claude-code
                            #       copy: true
                            # `rufler check` probes `npx skills --help` when any
                            # entry resolves to a skills.sh install so missing
                            # npx/network fails fast before `rufler run`.

# MCP servers to register with Claude Code before the swarm runs.
# Each server is added via `claude mcp add -s project` so it stays
# local to this project. ruflo's own MCP is added by `ruflo init`
# automatically — only declare additional servers here.
# mcp:
#   servers:
#     - name: my-db
#       command: npx
#       args: ["-y", "@anthropic/mcp-postgres"]
#       env:
#         DATABASE_URL: "postgresql://localhost/mydb"
#
#     - name: filesystem
#       command: npx
#       args: ["-y", "@anthropic/mcp-filesystem", "/home/user/docs"]
#
#     # HTTP transport (e.g. remote MCP)
#     - name: sentry
#       transport: http
#       url: "https://mcp.sentry.dev/mcp"

agents:
  - name: architect
    type: system-architect
    role: specialist          # queen | specialist | worker | scout
    seniority: lead           # lead | senior | junior
    prompt: |
      Design the overall architecture. Document decisions in memory.

  - name: backend
    type: coder
    role: worker
    seniority: senior
    # prompt inline OR prompt_path to a markdown file (relative to this yml)
    prompt_path: ./agents/backend.md
    # Soft DAG: this agent waits for `architect` to publish a brief and
    # 'approved' to shared memory before doing any work. Enforced via prompt.
    depends_on: [architect]

  - name: frontend
    type: coder
    role: worker
    seniority: senior
    prompt: Build the UI following the architect's design. Use modern patterns.
    depends_on: [architect]

  - name: qa
    type: tester
    role: worker
    seniority: junior
    prompt: Write integration and unit tests for all new code.
    depends_on: [backend, frontend]

  - name: reviewer
    type: reviewer
    role: specialist
    seniority: senior
    prompt: Review code for quality, security, and best practices.
    depends_on: [qa]
"""
