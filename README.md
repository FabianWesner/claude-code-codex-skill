# Claude Code Codex Skill

Claude Code skills for running headless Codex as an orchestration sub-agent. Two skills:

- **`codex-subagent`** — the primitives: one-shot Codex runs and steerable Codex app-server
  sessions, driven directly.
- **`codex-scheduler`** — a job system built on top of those primitives: submit Codex jobs with a
  slug, dependencies, and per-job model/effort/fast-mode; a global daemon runs them with a
  configurable parallelism limit and notifies the submitting Claude session natively (via
  Claude Code's background-task mechanism) when a job finishes or fails, instead of requiring
  Claude to keep polling. Includes a live local dashboard.

## What Is Included

- `.claude/skills/codex-subagent/SKILL.md` documents the base skill.
- `.claude/skills/codex-subagent/scripts/codex_appserver.py` runs a single Codex app-server turn.
- `.claude/skills/codex-subagent/scripts/codex_session.py` runs an interactive file-controlled Codex session.
- `.claude/skills/codex-scheduler/SKILL.md` documents the job-scheduler skill.
- `.claude/skills/codex-scheduler/scripts/db.py` — SQLite schema + query helpers (WAL mode).
- `.claude/skills/codex-scheduler/scripts/appserver_client.py` — the app-server client each job runs on, embeddable in the daemon.
- `.claude/skills/codex-scheduler/scripts/scheduler_daemon.py` — the global orchestrator (dependency graph, parallelism limit, hang/crash recovery).
- `.claude/skills/codex-scheduler/scripts/scheduler_cli.py` — the CLI Claude actually calls (submit/list/show/wait/steer/stop/rm/edit/reorder/config/ui).
- `.claude/skills/codex-scheduler/scripts/dashboard.py` + `scripts/static/index.html` — the live local dashboard.

## Requirements

- Claude Code with local skill support.
- OpenAI Codex CLI installed and authenticated.
- Python 3.9 or newer (stdlib only — no extra dependencies for either skill).

The bundled recipes assume the Codex CLI is available as `codex` and that authentication is already configured for the local user.

## Installation

Symlink both skill directories into your Claude skills directory:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/.claude/skills/codex-subagent" ~/.claude/skills/codex-subagent
ln -s "$(pwd)/.claude/skills/codex-scheduler" ~/.claude/skills/codex-scheduler
```

Restart Claude Code after installing so it can discover the new skill metadata. Use a real symlink
(not a copy) — both skills are meant to be edited in place here and picked up live.

## Usage

Open each skill file for its full operating rules and verified command patterns:

```bash
cat .claude/skills/codex-subagent/SKILL.md
cat .claude/skills/codex-scheduler/SKILL.md
```

`codex-subagent` covers:

- read-only Codex second opinions
- bounded workspace-write coding tasks
- resuming Codex sessions
- steerable app-server sessions with a file-based control plane

`codex-scheduler` covers:

- submitting single jobs or a dependency DAG in one batch
- a global daemon (one shared instance across every workspace/session) with a configurable
  parallelism limit, at `~/.claude/codex-scheduler/`
- getting notified natively (no polling) when a job finishes or fails, via `wait` launched as a
  backgrounded Bash command
- steering or stopping a running job, and removing/editing/reordering queued ones
- a live dashboard (`scheduler_cli.py ui`) showing every job's status and a live log tail
- safe concurrent use from multiple Claude sessions and sub-agents at once

## License

MIT License. See `LICENSE`.
