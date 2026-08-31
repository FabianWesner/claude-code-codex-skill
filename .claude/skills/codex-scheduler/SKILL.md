---
name: codex-scheduler
description: Queue, run, watch, and steer headless Codex jobs with dependencies and a parallelism limit, built on codex-subagent. Notifies the submitting Claude session natively when a job finishes or fails.
---

# codex-scheduler

A job system for Codex, layered on top of the `codex-subagent` skill's proven `codex app-server`
plumbing. Use this instead of driving `codex exec`/`codex_session.py` by hand whenever you have
more than one Codex job, jobs that depend on each other, or you want to keep working while a job
runs instead of babysitting it.

One global daemon (auto-started on first use) runs jobs from every workspace and every concurrent
Claude session against a shared SQLite DB at `~/.claude/codex-scheduler/db.sqlite3`. It enforces a
parallelism limit (default 3), respects a dependency graph between jobs, and never silently
retries a failed job.

## The core loop: submit, then get notified — don't poll

**Always submit, then launch `wait` via the Bash tool with `run_in_background: true`.** That's
what makes this different from raw `codex exec`/`codex_session.py`: Claude Code natively delivers
a `<task-notification>` to the session that started a backgrounded Bash command when it exits.
`wait` blocks on the shared DB until one of your jobs finishes, then exits — so you get told,
unprompted, the moment Codex is done or breaks, instead of having to remember to check.

```bash
python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py submit \
  --session <this-claude-session-id> --slug fix-lint --workspace <abs-repo> \
  --prompt "Fix the lint errors in src/" --effort xhigh

# THEN, via Bash with run_in_background: true:
python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py wait --session <this-claude-session-id>
```

You'll get a `<task-notification>` with `wait`'s stdout: the job's slug, final status, and its
result text (or error). If your session ends/restarts before that happens,
`scheduler_cli.py list --session <id>` catches you up on anything you missed.

Get `<this-claude-session-id>` from your own session id (visible in your environment/context);
pass it consistently on every submit/wait for that job so jobs are attributed correctly.

## Submitting a batch with dependencies

For a DAG of jobs, submit them all at once as JSON and reference dependencies by slug:

Each job spec takes either an inline `"prompt"` or a `"prompt_file"` (path to a file with the
full brief — relative paths resolve against the directory containing `--file`, handy for keeping
long briefs out of the JSON):

```bash
cat > /tmp/jobs.json <<'EOF'
[
  {"slug": "gen-a", "prompt": "...", "workspace": "/abs/repo", "effort": "xhigh"},
  {"slug": "gen-b", "prompt_file": "gen-b-brief.md", "workspace": "/abs/repo", "effort": "xhigh"},
  {"slug": "merge", "prompt": "...", "workspace": "/abs/repo", "deps": ["gen-a", "gen-b"]}
]
EOF
python3 .../scheduler_cli.py submit-batch --session <id> --file /tmp/jobs.json
python3 .../scheduler_cli.py wait --session <id> --slugs gen-a,gen-b,merge --all   # (backgrounded)
```

`merge` only starts once both `gen-a` and `gen-b` are `done`. If a dependency fails, its
dependents are automatically marked `failed` too (never retried, never left stuck queued) — check
`show <slug>` for the `error` field explaining why.

## Command reference

All commands: `python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py <cmd> ...`

| command | purpose |
|---|---|
| `submit --session --slug --workspace (--prompt \| --prompt-file) [--deps a,b] [--effort] [--model] [--fast] [--sandbox] [--priority]` | queue one job |
| `submit-batch --session --file jobs.json` | queue a DAG of jobs in one call |
| `list [--session] [--status] [--json]` | full summary of all jobs |
| `show <slug>` | one job's full detail + last 40 lines of its live log |
| `wait --session [--slugs a,b --all] [--timeout secs]` | block until a job settles (see above) |
| `steer <slug> "<text>"` | mid-flight correction into a *running* job |
| `stop <slug> [--reason "..."]` | interrupt+quit a running job (or delete if still queued); records why in the job's `error` field so `show`/the dashboard can distinguish an intentional stop from an actual failure |
| `rm <slug> [--cascade]` | remove a *queued* job (refuses if queued dependents exist, unless `--cascade`) |
| `edit <slug> [--prompt\|--prompt-file] [--deps] [--priority] [--effort]` | edit a *queued* job |
| `reorder <slug1> <slug2> ...` | set priority = list order among queued jobs |
| `config [--parallel N] [--hang-timeout MIN]` | read/set scheduler-wide settings (live, no restart) |
| `ui [--port 8787]` | start/reuse the live dashboard, then `open http://localhost:8787` |
| `daemon start\|stop\|status` | manual daemon control (usually unnecessary — auto-started) |

`steer`/`stop`/`rm`/`edit`/`reorder` only act on the job's most recent **active** (queued/running)
row for that slug — a slug can be reused once its earlier job is terminal.

## Job parameters

- **effort**: `low` | `medium` | `xhigh` | `ultra` — scale to the job like the base skill's
  recipes (`low` for quick lookups, `xhigh`/`ultra` for heavy analysis or hard code tasks).
- **model**: defaults to `gpt-5.6-sol`. Codex also exposes cheaper/faster siblings in the same
  family for lower-stakes jobs — e.g. `gpt-5.6-luna` (verified working via `codex exec -m
  gpt-5.6-luna`) — pass `--model gpt-5.6-luna` when you don't need the default tier.
- **fast_mode** (`--fast`, default off): sets Codex's `service_tier=fast` +
  `features.fast_mode=true` for that job's app-server process — ~1.5x speed at a higher credit
  rate (2.5x standard on GPT-5.6/5.5, 2x on GPT-5.4). It's a speed/cost tradeoff, independent of
  `effort` — use it when wall-clock time matters more than credit spend for that specific job.
- **sandbox**: `read-only` | `workspace-write` (default) | `danger-full-access`, same semantics as
  `codex exec -s`.
- **No auto-retry**: a failed/hung job is marked `failed` and reported as-is — the scheduler never
  silently resubmits it. Decide whether to `submit` it again yourself after seeing why it failed
  (`show <slug>`).
- **Invalid model names don't fail cleanly** (verified 2026-08-31): unlike `codex exec`, which
  rejects an unknown `-m` with a clear 400 error, `codex app-server` accepts it, runs the turn, and
  reports `turn/completed` with an **empty** result — the job shows `status: done`,
  `result: ""`. If a job comes back `done` with an empty/near-empty result, suspect a typo'd
  `--model` first.

## Steering and stopping

`steer`/`stop` reuse `codex_session.py`'s already-verified file control plane — the daemon drives
each running job's `codex app-server` process itself and polls that job's `<jobdir>/control` file
for `steer:`/`interrupt`/`quit` lines, exactly like the base skill's session daemon.

## Dashboard

`scheduler_cli.py ui` starts a small local, stdlib-only HTTP server (no external dependencies)
showing a live-updating job table; click a row to expand it and live-tail that job's log. Re-run
`ui` any time — it detects and reuses an already-running instance on that port instead of
double-starting.

## Notes

- One global daemon and DB serve every workspace — job `workspace` is just each job's own `cwd`
  for Codex, independent of where the daemon itself runs.
- Runtime state (DB, pidfile, per-job logs) lives at `~/.claude/codex-scheduler/`, separate from
  this skill's git-tracked code.
- Multiple Claude sessions (including Task-tool sub-agents, which inherit the same filesystem and
  Python) can submit/list/wait concurrently — the daemon is a singleton (flock-guarded) and all
  writes go through short SQLite transactions in WAL mode.
