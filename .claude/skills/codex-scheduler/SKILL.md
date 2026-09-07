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

**Always submit, then launch `wait --drain` via the Bash tool with `run_in_background: true`.**
That's what makes this different from raw `codex exec`/`codex_session.py`: Claude Code natively
delivers a `<task-notification>` to the session that started a backgrounded Bash command when it
exits. `wait` blocks on the shared DB until your jobs finish, then exits — so you get told,
unprompted, the moment Codex is done or breaks, instead of having to remember to check.

```bash
python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py submit \
  --session <this-claude-session-id> --slug fix-lint --workspace <abs-repo> \
  --prompt "Fix the lint errors in src/" --effort xhigh

# THEN, via Bash with run_in_background: true:
python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py wait \
  --session <this-claude-session-id> --drain
```

### Nothing can arm the watcher for you — so `submit` tells you whether one is armed

A `<task-notification>` is produced *only* when a Bash command **you** backgrounded exits. The
scheduler cannot self-arm that from inside the daemon, which makes "forgot to arm a watcher" a
silent failure: jobs run, finish, write their results to the DB, and nobody is ever told. This has
bitten real sessions — six jobs once sat finished and unreported because a dispatch went out with
no watcher behind it.

So every `submit`/`submit-batch` now prints your notification status:

```
  watcher: armed for this session
  watcher: NONE ARMED -- you will NOT be notified when this finishes.
           Background this via the Bash tool with run_in_background: true:
             python3 .../scheduler_cli.py wait --session <id> --drain
```

If you see `NONE ARMED`, arm one before you walk away.

### Use `--drain`, not a re-arm treadmill

Plain `wait` returns as soon as **one** job settles — `--follow` only changes how *messages* are
streamed, not that. Dispatch five jobs and you must re-arm five times, and every gap between them
is a window where a finishing job notifies nobody. That is precisely how sessions lose results.

`--drain` reports each job as it settles and keeps going, exiting only once the session has had
nothing queued or running for `--idle-grace` seconds (default 30). One watcher covers an entire
dispatch, including jobs submitted *after* it was armed — the grace period exists because an early
job often settles in the gap before the next submit lands, and exiting there would strand the rest.

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

For a batch, prefer one `wait --drain` over `--slugs ... --all`: `--all` reports the set in a
single burst only once every slug is terminal, whereas `--drain` reports each job the moment it
lands and still covers anything you add later.

## Codex can message you back mid-run

Every job's prompt automatically gets a short preamble telling Codex it can send you an ad-hoc
message *without* ending its turn, by running:

```bash
python3 .../scheduler_cli.py notify <its-own-slug> "<message>"
```

This goes through the same channel as job completion — the next `wait` for that session returns
it immediately (before the job itself finishes), tagged as a message rather than a result, and it
also appears live in the dashboard's output panel. Use it for jobs where you want an early
heads-up, a question, or a checkpoint instead of waiting for the whole turn — no need to explain
the mechanism yourself, Codex is already told about it.

**Correction (2026-09-01):** the earlier note here blamed Codex for ignoring this preamble. That
was wrong, or at least incomplete. `notify` writes to the scheduler's SQLite DB, which is outside
the job's sandbox, so from inside a `read-only` or `workspace-write` job the command was *rejected
by the sandbox* — indistinguishable, from the outside, from Codex declining to call it. Codex was
often trying and failing. Use the `[[NOTE]]` marker instead (see "Checkpoints" below): it travels
in the message stream, needs no filesystem access, and works in every sandbox. Instruction-following
still varies with effort, but that is now the only variable rather than the second of two.

## Command reference

All commands: `python3 .claude/skills/codex-scheduler/scripts/scheduler_cli.py <cmd> ...`

| command | purpose |
|---|---|
| `submit --session --slug --workspace (--prompt \| --prompt-file) [--engine codex\|cursor\|opencode] [--deps a,b] [--effort] [--model] [--fast] [--sandbox] [--priority] [--schema] [--cite] [--max-seconds] [--resume-from <slug> \| --resume-thread <id>] [--goal "..." \| --goal-file f] [--goal-budget N] [--goal-max-turns N] [--worktree] [--no-symlinks]` | queue one job |
| `submit-batch --session --file jobs.json` | queue a DAG of jobs in one call |
| `list [--session] [--status] [--json]` | full summary of all jobs |
| `show <slug>` | one job's full detail + last 40 lines of its live log |
| `wait --session [--slugs a,b --all] [--follow] [--timeout secs] [--json]` | block until a job settles or sends a message (see above); `--follow` keeps streaming further messages instead of returning after the first batch, still stopping as soon as the job settles; `--json` emits the machine-readable payload (result, `result_json`, tokens, checkpoint) |
| `ask <slug> "<question>" [--timeout secs]` | **ask a running job a question and block for its answer** -- request/response, unlike write-only `steer` |
| `checkpoint <slug> "<text>"` | called *by Codex* to save recoverable progress (prefer the `[[CHECKPOINT]]` marker -- see below) |
| `watch <slug> [--match REGEX] [--flat-for secs] [--timeout secs] [--since-now]` | block until something worth waking for happens, then exit -- background this instead of polling on a timer |
| `wait --session --drain [--idle-grace secs] [--slugs a,b] [--json]` | **the recommended watcher**: report every job as it settles and keep waiting; exit only after the session is quiet for `--idle-grace` seconds (default 30). Covers a whole dispatch with one backgrounded command |
| `usage [--json] [--days N]` | live Codex **account** rate limits, plan, credits and token usage (talks to Codex directly; needs no daemon and no job) |
| `notify <slug> "<text>"` | called *by Codex itself* from inside a running job to message you without ending its turn |
| `steer <slug> "<text>"` | mid-flight correction into a *running* job |
| `stop <slug> [--reason "..."]` | interrupt+quit a running job (or delete if still queued); records why in the job's `error` field so `show`/the dashboard can distinguish an intentional stop from an actual failure |
| `rm <slug> [--cascade]` | remove a *queued* job (refuses if queued dependents exist, unless `--cascade`) |
| `edit <slug> [--prompt\|--prompt-file] [--deps] [--priority] [--effort]` | edit a *queued* job |
| `reorder <slug1> <slug2> ...` | set priority = list order among queued jobs |
| `config [--parallel N] [--hang-timeout MIN]` | read/set scheduler-wide settings (live, no restart) |
| `ui [--port 1234]` | start/reuse the live dashboard (auto-started already, see below) |
| `daemon start\|stop\|status` | manual daemon control (usually unnecessary — auto-started) |
| `daemon stop --graceful [--timeout SECS]` | wait for running jobs to finish before stopping, instead of killing them (see Notes) |
| `worktree-list --workspace <repo>` | lane worktrees, as git sees them and as the scheduler recorded them |
| `worktree-remove <slug> --workspace <repo> [--force]` | remove one lane worktree (never automatic; refuses while a job still uses it) |

`steer`/`stop`/`rm`/`edit`/`reorder` only act on the job's most recent **active** (queued/running)
row for that slug — a slug can be reused once its earlier job is terminal.

## Continuing a finished job

**Rule (Fabian, 2026-09-07): a restarted, continued or corrected epic reuses the same Codex session.** Every follow-up turn on an epic is submitted with `--resume-from <previous slug of that epic>` (or `--resume-thread <id>`), never as a fresh job. A fresh job is only for different work, or when a second, independent opinion is wanted on purpose. `list` shows the lineage in the THREAD column: `= e16-epic` means the job runs in the same Codex session as `e16-epic`; `new` means a fresh session. The job id and slug change per turn, the thread does not.


`submit --resume-from <slug>` starts a new job **inside the finished job's Codex thread** instead
of a fresh one, so the model still has everything from that turn: what it read, what it decided,
what it already wrote, plus the prompt cache behind it (the verification run's second job billed
14.3k input tokens of which 13.1k were cached).

```bash
python3 $CLI submit --session <sid> --slug e12-s2 --workspace /path/to/repo \
  --prompt-file slice2.md --resume-from e12-s1
```

Use it for the next chunk of the same epic, a correction to work a job just did, or a follow-up
question about what it found. Reach for a fresh job instead when the task is unrelated — a long
thread carries irrelevant context and costs tokens on every turn.

- The source job must belong to the **same Claude session** and be **terminal** (`done`, `failed`
  or `stopped`), and must have a recorded `thread_id`. Anything else is refused with the reason.
- `--resume-thread <thread id>` does the same from a raw Codex thread id, for a thread this
  scheduler's DB doesn't know about. The two flags are mutually exclusive.
- The resumed job is a **normal job**: its own row, slug, log and result, so `list`, `show`,
  `wait`, `steer` and `stop` behave exactly as usual. `show <slug>` prints
  `resumed from: <slug> (thread …)`.
- **The sandbox, model and cwd come from the thread you resume.** Codex re-applies the resumed
  thread's own settings, so passing a different `--sandbox`/`--model` on the resuming job is not a
  reliable way to change them — submit a fresh job if you need different ones.
- **Context window limits still apply.** A resumed thread keeps growing and compacts itself when
  it fills, so a very long chain gradually loses its earliest detail. For a long epic, prefer a
  few resumed chunks over dozens.
- Codex and OpenCode only — `--engine cursor` jobs have no resumable thread. An OpenCode job
  resumes its **OpenCode session** (`opencode run --session <id>`) with the same flags and the
  same lineage display; a thread/session can only be continued on the engine that created it.
- In a `submit-batch` file the same thing is `"resume_from": "<slug>"` (or
  `"resume_thread": "<id>"`) on a job spec. The source must already be terminal at submit time, so
  it cannot be another job in the same batch.

Protocol note: this issues `thread/resume` (`{threadId, cwd, approvalPolicy}`) before the usual
`turn/start`, confirmed against `codex app-server generate-json-schema` (`ThreadResumeParams`,
codex-cli 0.153.4). If the resume fails the job fails with that error rather than quietly starting
a fresh thread.

## Goal mode

**The goal is the finish line; the prompt file is the requirements.** A normal job ends when its
one turn ends, whether or not the work is actually finished. A goal job carries a thread-level
goal, keeps taking turns in the same thread, and ends when the *goal* is reached.

```bash
python3 $CLI submit --session <sid> --slug e12-epic --workspace /path/to/repo \
  --prompt-file specs/e12/brief.md \
  --goal-file specs/e12/goal.md --goal-budget 2000000 --goal-max-turns 12 \
  --model gpt-6-astra --effort medium --sandbox danger-full-access
```

- `--goal-file <path>` (or `--goal "<text>"`, mutually exclusive) is the objective. Write it as a
  *verifiable end state*, not a task list: "every slice of the spec is implemented and
  `PAO_DISABLE=1 php artisan test tests/Feature tests/Unit` is back at the known baseline".
- `--goal-budget <tokens>` is the thread's token budget. Running out settles the job rather than
  letting it grind on.
- `--goal-max-turns N` (default 12) caps how many turns the scheduler will let the goal run for.
- Codex engine only. `--engine cursor` and `--engine opencode` have no thread goal and the submit is refused.

**What the status column means.** `list` shows `goal:<status>/<n>t` — the goal's status and the
number of turns taken. The status vocabulary is the protocol's own
(`ThreadGoalStatus`: `active`, `paused`, `blocked`, `usageLimited`, `budgetLimited`, `complete`)
and it maps to a job outcome like this:

| goal status | job ends as | meaning |
|---|---|---|
| `active` | keeps running | another turn starts automatically in the same thread |
| `complete` | `done` | the goal was reached; the last turn's message is the result |
| `blocked` | **`blocked`** | it needs a human decision. Not a failure and not a success — read the result and either answer it with a `--resume-from` follow-up or change the plan |
| `usageLimited` | `done` (with a note) | the account hit its usage limit before the goal was met |
| `budgetLimited` | `done` (with a note) | `--goal-budget` was exhausted before the goal was met |
| `paused` | `done` (with a note) | the goal was paused; the scheduler does not un-pause it |

`blocked` is a real job status: `wait` returns on it like any other terminal status, and a queued
dependent of a blocked job is failed the same way it would be for a failed dependency.

`show <slug>` prints the objective, status, tokens used against the budget, wall time and turn
count. `wait --json` carries the same under a `goal` key.

**Who drives the turns.** Verified live (2026-09-07, codex-cli 0.153.4): the app-server *itself*
starts the next turn while a goal is `active`. The scheduler waits 8 seconds after `turn/completed`
and only starts a turn of its own if the app-server did not — the log line
`[goal: app-server started the next turn itself; not double-starting]` is that guard firing.
Turns the scheduler starts itself open with one fixed nudge,
"Continue toward the goal. Report what is verified so far."

**Resuming keeps the goal.** `--resume-from <slug>` on a goal job reads the goal already on the
thread (`thread/goal/get`) instead of re-setting it, so the tokens and time already spent carry
over. Passing a new `--goal-file`/`--goal` on the resuming job replaces the objective deliberately.

Protocol: `thread/goal/set {threadId, objective, status, tokenBudget}` after `thread/start` /
`thread/resume` and before `turn/start`; `thread/goal/updated` is persisted on every change.

## Worktrees per epic

`submit --worktree` runs the job in its own git worktree instead of the shared checkout, so two
lanes on the same repo cannot overwrite each other's files.

```bash
python3 $CLI submit --session <sid> --slug e12-epic --workspace /Users/you/repo \
  --prompt-file brief.md --worktree --sandbox danger-full-access
```

- The worktree is `<workspace>/.claude/worktrees/<slug>` on a new branch `lane/<slug>`, cut from
  the current `main` HEAD (from `HEAD` if the repo has no `main`). An existing worktree or branch
  of that name is reused, never recreated.
- `workspace` on the job row stays the main checkout; `worktree_path` is what the job actually
  runs in. `list` shows the worktree path in the WORKSPACE column and `show` prints the branch.
- A fresh worktree is bare, so submit prepares it and prints what it did: `vendor` and
  `node_modules` are **symlinked** from the main checkout when they exist there, `.env` is copied
  with a sqlite `DB_DATABASE=` rewritten to `<worktree>/database/database.sqlite`, and the main
  checkout's `database/database.sqlite` is copied to that path. `--no-symlinks` skips all of that.
- `--resume-from` **inherits the source job's worktree**, so every turn of an epic lands in the
  same tree. If that worktree has since been removed the submit is refused rather than silently
  running in the main checkout.
- **Nothing is ever removed automatically.** `worktree-list --workspace <repo>` shows what exists;
  `worktree-remove <slug> --workspace <repo>` removes one (refusing while a queued/running job
  still uses it, unless `--force`). The `lane/<slug>` branch is always kept — delete it yourself.
- In a `submit-batch` file: `"worktree": true` (and `"no_symlinks": true`) on a job spec.
- Merging is yours: the lane commits on `lane/<slug>`, and you review and merge it into `main`.

## Getting output you can act on

### `--schema`: make the result machine-readable

Pass a JSON Schema (inline, or a path to a `.json` file) and Codex is told its final message must
be exactly one JSON value matching it:

```bash
python3 .../scheduler_cli.py submit --session <id> --slug audit --workspace <repo> \
  --schema '{"type":"object","required":["findings"],"properties":{"findings":{"type":"array"}}}' \
  --prompt "Audit X. Return findings as JSON."
python3 .../scheduler_cli.py wait --session <id> --slugs audit --all --json
```

The daemon parses the final message (tolerating a ```json fence or surrounding prose), checks it,
and stores it in `result_json`, which `wait --json` returns as real JSON. On a mismatch the job
still completes -- `result` keeps the raw text and `schema_error` says what was wrong, so you
never silently lose the work.

**The check is deliberately shallow: top-level `type` plus `required` keys.** It catches the
common failures (answered in prose, dropped a field) and nothing subtler. It is not a JSON Schema
validator, so do not treat a pass as full validation of nested structure.

### `--cite`: make claims checkable

Adds a preamble requiring every factual claim in the result to carry an anchor you can verify
independently -- `file.py:120-134` for code, URL plus the quoted sentence for a web source, the
exact command and its output for observed behaviour -- and to mark anything it cannot anchor as
UNVERIFIED rather than dropping it or dressing it up.

Use it whenever you intend to *act* on the result. Verification is the real bottleneck on
delegation: a claim you can spot-check in seconds is worth far more than a confident paragraph you
would have to redo the work to trust.

### `{{deps.<slug>.result}}`: pass work down the graph

Dependencies sequence jobs; interpolation lets them actually *compose*. In any prompt:

- `{{deps.<slug>.result}}` -- that dependency's final result
- `{{deps.<slug>.result_json}}` -- its validated structured result
- `{{deps.<slug>.checkpoint}}` -- its last checkpoint

Substitution happens at launch, once the dependency is `done`, so the downstream job sees the real
text instead of re-deriving it. An unknown slug is left in place verbatim and logged rather than
silently blanked -- a visible `{{deps...}}` in a prompt is a bug you can see; an empty string is
not.

## Supervising a job while it runs

### `ask`: a real question, and an answer

```bash
python3 .../scheduler_cli.py ask my-job "Which file are you on, and what have you ruled out?"
```

Blocks (default 120s) and prints Codex's reply. The question is steered into the running turn and
the daemon captures the next agent message as the answer; **Codex keeps working, the turn does not
end, and the answer is not part of the job's result.** Verified round-trip in testing: ~7s.

This is the tool for "is it on the right track?", because the progress log shows you what a job
*did* and never what it *concluded*.

### `watch`: get woken only when it matters

```bash
# (background this) wake on a dangerous command, or on a genuine stall
python3 .../scheduler_cli.py watch my-job --match "rm -rf|git push" --flat-for 90
```

Exits on the first of: `--match` hitting new log output, `--flat-for` seconds without the log
growing (a real stall), the job settling, or `--timeout`. Because Claude Code notifies the session
when a backgrounded command exits, this replaces polling the log on a timer -- which costs a full
model invocation per tick, usually just to learn that nothing changed.

**What the log can and cannot tell you:** every shell command is visible (truncated to 200 chars),
and so is every streamed message. Reasoning is *not* -- it appears only as a bare
`[item reasoning]` marker, and web searches log neither query nor results. So a job reading the
wrong repo is obvious within seconds, while a job reasoning its way to a wrong conclusion looks
identical to one reasoning correctly. A stalled job shows a log that stops growing entirely; a
thinking job keeps emitting markers at a slow, steady rate. Use `--flat-for`, not marker counts.

### Checkpoints: surviving a stop

Jobs with `--max-seconds`, or at `xhigh`/`ultra` effort, are told to save progress periodically by
writing a marker into their own message text:

```
[[CHECKPOINT]] read db.py and daemon.py; state machine mapped; still to do: cli, client
```

The marker must **start a line** (leading whitespace is fine) and its content runs to the next
blank line. The scheduler stores it (replacing the previous checkpoint), strips it from the result,
and surfaces it automatically when the job is stopped, times out, or exceeds its budget -- turning
"all work lost" into "here is where it got to". `[[NOTE]] <text>` works the same way for an ad-hoc
message to you, delivered through `wait` like `notify`.

The line anchor matters: a job that merely *mentions* the syntax mid-sentence -- "write a line
starting with the `[[NOTE]]` marker" -- keeps that text in its result instead of having it parsed
as a real message and cut out. (A marker starting a line inside a fenced code block is still
treated as real; that case is rare enough to live with.)

> **Why markers rather than a CLI call.** `notify`/`checkpoint`/`answer` all write to the
> scheduler's SQLite DB, which lives outside the job's sandbox. Under `read-only` -- and under
> `workspace-write` before this was fixed -- those writes are rejected, so the callback fails
> silently and looks exactly like Codex ignoring the instruction. (This was a real, long-standing
> bug: `workspace-write` jobs now get the scheduler state dir added as a `writableRoots` entry, but
> `read-only` has no writable path at all in the protocol.) **The markers need no filesystem access
> whatsoever, so they work identically in every sandbox** -- prefer them; the CLI forms remain for
> `danger-full-access` jobs and external callers.

### Budgets and token accounting

`--max-seconds N` stops a job once it has run that long, recording `budget exceeded` as the cause
and preserving its checkpoint. This is deliberately distinct from the hang timeout: a job can be
perfectly healthy and still not worth more time.

Every job's cumulative token usage is captured live from the app-server
(`thread/tokenUsage/updated`) and reported by `wait` and `wait --json`: input, cached, output,
reasoning, total, and the model's context window.

## Checking the Codex account

```bash
python3 .../scheduler_cli.py usage          # human-readable
python3 .../scheduler_cli.py usage --json   # full payload
```

Shows the plan, each rate-limit bucket with a used-percent bar and when it resets, credit balance,
available reset credits, and lifetime/daily token usage. It queries Codex directly
(`account/rateLimits/read`, `account/usage/read`) and deliberately does **not** start the scheduler
daemon, so it is safe to run at any time -- including before deciding whether to fan out a batch of
expensive jobs.

## Choosing the engine: Codex, Cursor or OpenCode

Every job runs under one of three agent CLIs, selected with `--engine` (default `codex`). All are
driven through the same scheduler: dependencies, `--drain`, budgets, checkpoints, markers,
structured output and token accounting all work identically, and a batch can mix them freely --
including passing a Codex job's result into a Cursor job with `{{deps.<slug>.result}}`.

```bash
# Cursor with Composer (no reasoning levels -- one tier)
... submit --engine cursor --model composer-2.5 --slug build --workspace <repo> --prompt "..."

# Cursor with Grok 4.6, non-fast, choosing the reasoning level
... submit --engine cursor --model cursor-grok-4.6 --effort xhigh --slug review ...
```

**Model and reasoning level.** Cursor bakes the reasoning level into the model id, so `--effort`
selects the suffix rather than a separate parameter:

| `--effort` | `cursor-grok-4.6` | `composer-2.5` |
|---|---|---|
| `low` / `medium` / `high` / `xhigh` | `-low` / `-medium` / `-high` / `-xhigh` | `composer-2.5` (no levels) |
| `max` / `ultra` | `-xhigh` (no higher tier exists) | `composer-2.5` |

Families differ -- Grok 4.6 stops at `xhigh` while Luna and Sol also offer `max` -- so resolution
is checked against `cursor-agent --list-models` rather than assumed, and **submit fails
immediately** with the available ids if a pairing does not exist. The resolved id is echoed on
submit (`model=cursor-grok-4.6-xhigh`) so there is never doubt about what a job actually ran.
Jobs are **non-fast** unless you pass `--fast`. `--effort max` and `ultra` are equivalent for
Cursor (there is no tier above `max`, so both just reach for the ceiling a family actually has).

**Differences worth knowing before choosing Cursor:**

- **No mid-turn control.** `cursor-agent -p` is a one-shot process with no channel to inject into
  a running turn, so `steer` and `ask` are rejected with a clear message rather than silently
  dropped. `stop` still works (it terminates the process).
- **Reasoning is visible.** Cursor streams `thinking` text into the log; Codex emits only a
  marker. Cursor jobs are genuinely easier to supervise with `watch`.
- **Sandbox mapping.** `read-only` runs in Cursor's plan mode (read-only by construction);
  `workspace-write` and `danger-full-access` pass `--force` so a headless run never stalls waiting
  for an approval nobody is there to give.
- **Tokens** arrive once at the end rather than incrementally, and Cursor reports no context-window
  figure, so that field stays null.

### OpenCode engine

`--engine opencode` drives `opencode run --format json --model <provider/model> --dir <workspace>
"<prompt>"` — a one-shot subprocess streaming newline-delimited JSON events, like Cursor.

```bash
... submit --engine opencode --slug spike --workspace <repo> \
    --sandbox danger-full-access --prompt-file brief.md
# follow-up in the SAME OpenCode session
... submit --engine opencode --slug spike-2 --workspace <repo> \
    --resume-from spike --prompt "..."
```

**When to use it (owner):** only when Codex is exhausted (rate limit or credit) and the Claude
budget is gone too, or when a deliberately different model is wanted for a second opinion. It is
not a routine lane; the routing table above still decides normal work.

- **Model:** defaults to `opencode/muse-spark-1.3-contributor-free` — the OpenCode Zen free tier
  the UI labels "Muse Spark 1.3 Free". The `opencode-go/…-contributor` ids are a **paid plan** and
  must never be the default; pass one explicitly only if the owner asked for it. `--model` is
  validated against `opencode models` at submit time, so a typo fails immediately with the ids
  that do exist. `--effort` is recorded but means nothing here (no reasoning-level suffixes), and
  `--fast` is rejected as the Codex-only service tier it is.
- **No sandbox at all.** `opencode run` has no sandbox flag: tools run against the real filesystem
  with whatever the user's OpenCode config permits. `danger-full-access` is the only honest label,
  and submitting with anything else prints a note saying the value is recorded but not enforced.
  Do not send an OpenCode job work you would only trust to `read-only`.
- **Resumable.** Every event carries a `sessionID`; the driver stores it on the job like a Codex
  thread id, so `--resume-from` continues the session with its context and prompt cache (verified:
  the follow-up answered from memory with 13.5k of 13.9k input tokens served from cache).
- **No mid-turn control**, same as Cursor: `steer` and `ask` are rejected with a clear message,
  `stop` terminates the process.
- **No goal mode** (`--goal`/`--goal-file` is refused; only Codex has a thread goal).
- **Markers work identically:** `[[NOTE]]`/`[[CHECKPOINT]]` lines are dispatched and stripped, the
  final assistant message becomes the result, tool calls are logged one line each, and tokens are
  reported from the last `step_finish` event (input/cached/output/reasoning; no context-window
  figure, so that field stays null). A nonzero exit or an `error` event fails the job with the
  message OpenCode gave.

## Job parameters

- **effort**: `low` | `medium` | `high` | `xhigh` | `max` | `ultra` — Codex's own reasoning-effort
  vocabulary, low to high (verified against `~/.codex/models_cache.json` and live `codex exec`
  calls at every level, 2026-09-04). Scale to the job like the base skill's recipes: `low` for
  quick lookups, `high`/`xhigh` for real analysis, `max`/`ultra` for the hardest code tasks —
  `ultra` additionally triggers Codex's own automatic task delegation.
- **model**: defaults to `gpt-5.6-sol`. For the hardest jobs, `--model gpt-6-astra` (a full
  generation up, not a 5.6-family sibling — "our most capable model for complex, demanding work"
  per Codex's own listing) is worth the extra cost/latency; pair it with `--effort
  high`/`xhigh`/`max`/`ultra`. Verified working end to end 2026-09-04. At the other end, Codex
  also exposes cheaper/faster 5.6-family siblings for lower-stakes jobs — e.g. `gpt-5.6-luna`
  (verified working via `codex exec -m gpt-5.6-luna`) — pass `--model gpt-5.6-luna` when you don't
  need the default tier.
- **fast_mode** (`--fast`, default off): sets Codex's `service_tier=fast` +
  `features.fast_mode=true` for that job's app-server process — ~1.5x speed at a higher credit
  rate (2.5x standard on GPT-5.6/5.5, 2x on GPT-5.4). It's a speed/cost tradeoff, independent of
  `effort` — use it when wall-clock time matters more than credit spend for that specific job.
- **sandbox**: `read-only` | `workspace-write` (default) | `danger-full-access`, same semantics as
  `codex exec -s`. **Which one do you need?** `workspace-write` blocks network access, TCP/Unix
  sockets, and writes to `.git` — it's for straightforward file edits only. If the job stages
  files (`git add`), drives a browser or emulator, runs anything socket-based (a local server, a
  test harness that binds a port), or fetches something over the network, use
  `--sandbox danger-full-access` instead. A `workspace-write` job that silently hits one of these
  restrictions usually doesn't error cleanly — it just produces a wrong/empty result with no
  obvious cause, so get the sandbox right up front rather than debugging it after the fact. The
  submit confirmation and `list`/`show` always echo the sandbox actually used, specifically so a
  wrong choice is visible immediately instead of discovered hours later.
- **schema / cite / max_seconds**: see "Getting output you can act on" above.
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

Auto-started at **http://localhost:1234** the first time the daemon (re)starts in a session — you
don't need to run anything to get it. `scheduler_cli.py ui [--port N]` starts/reuses it explicitly
(e.g. for a different port); it detects and reuses an already-running instance on that port
instead of double-starting. A small, stdlib-only HTTP server (no external dependencies) showing a
live-updating job table; click a row to expand it and live-tail that job's log. Each running job's
row has a **Cancel** button — it stops that job exactly like `scheduler_cli.py stop` would, and
records `"stopped by the user via the dashboard"` as the cause, so the next `wait` for that job
tells you plainly that the user did it, not that it failed.

The table's "Working On" column (running jobs only) is a one-line, plain-language summary of what
a job is doing right now — e.g. "Currently testing the Android mobile app" — regenerated about
once a minute per running job by a separate, cheap `codex exec -m gpt-5.6-luna -c
model_reasoning_effort=low` call fed the job's own recent (filtered) output. It's a nice-to-have,
not something to depend on for anything besides a glance at the table.

**The dashboard is a static page that polls for data, not one that hot-reloads its own code** — if
this skill's `static/index.html` changes (e.g. after an update to this skill), your browser tab
needs a manual reload to pick up the new JavaScript; the live-updating table/log you already have
open won't do that on its own.

## Notes

- One global daemon and DB serve every workspace — job `workspace` is just each job's own `cwd`
  for Codex, independent of where the daemon itself runs.
- Runtime state (DB, pidfile, per-job logs) lives at `~/.claude/codex-scheduler/`, separate from
  this skill's git-tracked code.
- Multiple Claude sessions (including Task-tool sub-agents, which inherit the same filesystem and
  Python) can submit/list/wait concurrently — the daemon is a singleton (flock-guarded) and all
  writes go through short SQLite transactions in WAL mode.
- **A hard `daemon stop` kills every currently-running job** (recovered as `failed`, per the
  no-auto-retry policy — see above). This mostly matters when *editing this skill's own scripts* —
  the daemon doesn't hot-reload, so trying out a code change means stopping and letting the next
  command respawn it. **Before doing that, always check `list --status running` first, and prefer
  `daemon stop --graceful [--timeout SECS]`** over a plain `stop` — it stops accepting new job
  launches and waits (indefinitely by default) for whatever's already running to finish naturally,
  only then sending the actual stop signal; with `--timeout` it gives up and leaves the daemon
  running rather than killing anything if jobs are still going. A plain `daemon stop` is fine when
  `list --status running` is already empty.
- The daemon checks once per calendar day (on whichever tick first notices the date changed) and
  runs `npm install -g @openai/codex` before launching any new jobs that day, so Codex itself stays
  current — this only gates new job launches, not jobs already running.
