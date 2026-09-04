# Claude Code Codex Skill

Claude Code skills. Four skills:

- **`codex-subagent`** — the primitives: one-shot Codex runs and steerable Codex app-server
  sessions, driven directly.
- **`codex-scheduler`** — a job system built on top of those primitives: submit Codex jobs with a
  slug, dependencies, and per-job model/effort/fast-mode; a global daemon runs them with a
  configurable parallelism limit and notifies the submitting Claude session natively (via
  Claude Code's background-task mechanism) when a job finishes or fails, instead of requiring
  Claude to keep polling. Includes a live local dashboard.
- **`issue-tracker`** — a per-project issue tracker with a live kanban board: Open, Planned,
  In Progress, Deployed, Done, Cancelled. Claude files, moves, and flags issues from the CLI;
  the board is a read-only view at `localhost:2345`, live over SSE.
- **`logbook`** — a template for a Twitter-style status feed, published as an Artifact, where
  Claude reports outcomes to its user on a loop (hourly, plus ad-hoc alerts and questions). Ships
  as an empty starter — fill in the project name and artifact URL, and adjust the writing rules
  to your user, before using it for real.

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
- `.claude/skills/issue-tracker/SKILL.md` documents the issue-tracker skill.
- `.claude/skills/issue-tracker/issues.js` — the CLI Claude calls (add/start/deploy/done/cancel/flag/list/show/...).
- `.claude/skills/issue-tracker/store.js` — the file-backed store (`~/.claude/issues/<project>/<n>.json`, one file per issue).
- `.claude/skills/issue-tracker/server.js` + `board.html` — the live kanban board, autostarted by the CLI.
- `.claude/skills/logbook/SKILL.md` documents the logbook skill — a template, fill in `PROJECT_NAME` and `ARTIFACT_URL` before using it.
- `.claude/skills/logbook/logbook.html` — an empty starter feed with one entry; publish it once with the Artifact tool to get your `ARTIFACT_URL`.

## Requirements

- Claude Code with local skill support.
- OpenAI Codex CLI installed and authenticated (for `codex-subagent` / `codex-scheduler` only).
- Python 3.9 or newer (stdlib only) for `codex-subagent` / `codex-scheduler`.
- Node.js (no dependencies) for `issue-tracker`.

The bundled recipes assume the Codex CLI is available as `codex` and that authentication is already configured for the local user.

## Installation

Symlink the skill directories you want into your Claude skills directory:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/.claude/skills/codex-subagent" ~/.claude/skills/codex-subagent
ln -s "$(pwd)/.claude/skills/codex-scheduler" ~/.claude/skills/codex-scheduler
ln -s "$(pwd)/.claude/skills/issue-tracker" ~/.claude/skills/issue-tracker
ln -s "$(pwd)/.claude/skills/logbook" ~/.claude/skills/logbook
```

Restart Claude Code after installing so it can discover the new skill metadata. Use a real symlink
(not a copy) — each skill is meant to be edited in place here and picked up live.

## Usage

Open each skill file for its full operating rules and verified command patterns:

```bash
cat .claude/skills/codex-subagent/SKILL.md
cat .claude/skills/codex-scheduler/SKILL.md
cat .claude/skills/issue-tracker/SKILL.md
cat .claude/skills/logbook/SKILL.md
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
- a live dashboard, auto-started at **http://localhost:1234** the first time the daemon starts in
  a session (no need to run `scheduler_cli.py ui` yourself unless you want a different port or it
  isn't already up) — showing every job's status, a "Working On" summary, and a live log tail,
  with a Cancel button per running job
- safe concurrent use from multiple Claude sessions and sub-agents at once
- Codex can message the submitting Claude session mid-run (`notify`), delivered through the same
  `wait` channel as job completion
- a daily `npm install -g @openai/codex` before the first job launches each day, so Codex stays
  current

`issue-tracker` covers:

- filing bugs and features, and moving them through Open → Planned → In Progress → Deployed →
  Done → Cancelled — none of the steps are mandatory
- flagging any active issue for the user's attention, with a required reason shown right on the
  card; clears only when the issue is actually resolved, not just moved
- per-project boards, numbered independently (`#01`, `#02`, ...), auto-detected from the git repo
  containing the working directory
- a live kanban board at **http://localhost:2345**, autostarted by the CLI on first use, updating
  over SSE with no reload — six columns side by side, each independently collapsible
- Done and Cancelled show only the last 12 hours; every other column is never filtered
- every column sorts newest-created first
- one JSON file per issue under `~/.claude/issues/<project>/`, atomic writes, safe for concurrent
  sessions

`logbook` covers:

- a Twitter-style feed, published as a Claude Artifact, that Claude writes to on a `/loop` tick
  or ad-hoc — reports, alerts, and questions, each its own entry type
- writing rules aimed at outcomes over process: short entries, screenshots for visual changes,
  never editing a published entry after the fact
- a pinned "open questions" box, separate from the main timeline, so anything waiting on the user
  doesn't get lost in the feed
- ships as a **template** — an empty starter feed with one entry, `PROJECT_NAME` and
  `ARTIFACT_URL` placeholders to fill in, and writing rules meant to be adjusted to the user
  rather than used as-is

## License

MIT License. See `LICENSE`.
