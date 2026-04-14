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

# How to launch Claude Code — set once here, `rufler start` honors it.
# CLI flags (--background / --yolo / --non-interactive) override these.
execution:
  non_interactive: false    # true  → claude -p --output-format stream-json
  yolo: false               # true  → --dangerously-skip-permissions (no approval prompts)
  background: false         # true  → detach from terminal, log to file below
                            #         (background also forces non_interactive + yolo)
  log_file: .rufler/run.log

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
