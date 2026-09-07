#!/usr/bin/env python3
"""SQLite schema + shared query helpers for the codex-scheduler skill.

All runtime state lives under STATE_DIR (~/.claude/codex-scheduler/ by default, override with
CODEX_SCHEDULER_HOME). Every script (cli, daemon, dashboard) imports this module rather than
touching the DB file directly, so schema/locking rules live in exactly one place.
"""
import os
import sqlite3
import time

STATE_DIR = os.environ.get(
    "CODEX_SCHEDULER_HOME",
    os.path.expanduser("~/.claude/codex-scheduler"),
)
DB_PATH = os.path.join(STATE_DIR, "db.sqlite3")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
DAEMON_PIDFILE = os.path.join(STATE_DIR, "daemon.pid")
DAEMON_LOCKFILE = os.path.join(STATE_DIR, "daemon.lock")
DAEMON_LOG = os.path.join(STATE_DIR, "daemon.log")
DRAIN_MARKER = os.path.join(STATE_DIR, "drain")

# 'blocked' is a goal-mode terminal status: the thread's goal reported `blocked`, so the job is
# finished but explicitly NOT successful -- it needs a human decision, not a retry.
TERMINAL_STATUSES = ("done", "failed", "stopped", "blocked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL,
  prompt TEXT NOT NULL,
  workspace TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
  effort TEXT NOT NULL DEFAULT 'medium',
  fast_mode INTEGER NOT NULL DEFAULT 0,
  sandbox TEXT NOT NULL DEFAULT 'workspace-write',
  claude_session_id TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  notified INTEGER NOT NULL DEFAULT 0,
  session_dir TEXT,
  pid INTEGER,
  error TEXT,
  result TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at TEXT,
  finished_at TEXT,
  working_on TEXT,
  working_on_updated_at TEXT,
  thread_id TEXT,
  result_schema TEXT,
  result_json TEXT,
  schema_error TEXT,
  cite_mode INTEGER NOT NULL DEFAULT 0,
  max_seconds INTEGER,
  checkpoint TEXT,
  checkpoint_at TEXT,
  tokens_input INTEGER,
  tokens_cached INTEGER,
  tokens_output INTEGER,
  tokens_reasoning INTEGER,
  tokens_total INTEGER,
  context_window INTEGER,
  engine TEXT NOT NULL DEFAULT 'codex',
  resume_thread_id TEXT,
  resume_from_slug TEXT,
  goal_objective TEXT,
  goal_budget INTEGER,
  goal_max_turns INTEGER,
  goal_set_on_start INTEGER NOT NULL DEFAULT 1,
  goal_status TEXT,
  goal_tokens_used INTEGER,
  goal_time_used_seconds INTEGER,
  goal_turns INTEGER NOT NULL DEFAULT 0,
  worktree_path TEXT,
  worktree_branch TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_slug ON jobs(slug) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS jobs_session ON jobs(claude_session_id);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_deps (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  depends_on_job_id INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS job_deps_job ON job_deps(job_id);

CREATE TABLE IF NOT EXISTS job_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  text TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS job_messages_job ON job_messages(job_id);

CREATE TABLE IF NOT EXISTS job_asks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  question TEXT NOT NULL,
  answer TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  answered_at TEXT
);
CREATE INDEX IF NOT EXISTS job_asks_job ON job_asks(job_id);

CREATE TABLE IF NOT EXISTS job_working_on_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS job_working_on_log_job ON job_working_on_log(job_id);

CREATE TABLE IF NOT EXISTS config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  parallel_limit INTEGER NOT NULL DEFAULT 3,
  hang_timeout_minutes INTEGER NOT NULL DEFAULT 30
);
INSERT OR IGNORE INTO config(id) VALUES (1);
"""

# Codex's own reasoning-effort vocabulary, low to high (verified against
# ~/.codex/models_cache.json and a live `codex exec` call for every level, 2026-09-04).
VALID_EFFORT = ("low", "medium", "high", "xhigh", "max", "ultra")
VALID_SANDBOX = ("read-only", "workspace-write", "danger-full-access")
VALID_STATUS = ("queued", "running", "done", "failed", "stopped", "blocked")
# Which agent CLI runs the job. 'codex' drives `codex app-server` over JSON-RPC;
# 'cursor' drives `cursor-agent -p --output-format stream-json` as a one-shot subprocess;
# 'opencode' drives `opencode run --format json` as a one-shot subprocess (resumable via
# --session, no sandbox flag of any kind).
VALID_ENGINE = ("codex", "cursor", "opencode")
# The opencode default is the OpenCode Zen FREE tier. The `opencode-go/...-contributor` ids are a
# paid plan and must never become the default.
DEFAULT_MODEL = {"codex": "gpt-5.6-sol", "cursor": "composer-2.5",
                 "opencode": "opencode/muse-spark-1.3-contributor-free"}


def ensure_state_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)


# Columns added after the initial release -- CREATE TABLE IF NOT EXISTS alone won't add these to
# a DB that already exists on disk, so migrate() adds any that are missing.
_ADDED_COLUMNS = [
    ("jobs", "working_on", "TEXT"),
    ("jobs", "working_on_updated_at", "TEXT"),
    ("jobs", "thread_id", "TEXT"),
    ("jobs", "result_schema", "TEXT"),
    ("jobs", "result_json", "TEXT"),
    ("jobs", "schema_error", "TEXT"),
    ("jobs", "cite_mode", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "max_seconds", "INTEGER"),
    ("jobs", "checkpoint", "TEXT"),
    ("jobs", "checkpoint_at", "TEXT"),
    ("jobs", "tokens_input", "INTEGER"),
    ("jobs", "tokens_cached", "INTEGER"),
    ("jobs", "tokens_output", "INTEGER"),
    ("jobs", "tokens_reasoning", "INTEGER"),
    ("jobs", "tokens_total", "INTEGER"),
    ("jobs", "context_window", "INTEGER"),
    ("jobs", "engine", "TEXT NOT NULL DEFAULT 'codex'"),
    # `submit --resume-from/--resume-thread`: the Codex thread this job continues, and the slug of
    # the job that thread came from (display only -- the thread id is what the client resumes).
    ("jobs", "resume_thread_id", "TEXT"),
    ("jobs", "resume_from_slug", "TEXT"),
    # `submit --goal-file/--goal`: thread-level goal mode (thread/goal/set + thread/goal/updated).
    # goal_set_on_start=0 means "this job resumes a thread that already carries a goal; read it
    # with thread/goal/get instead of overwriting it".
    ("jobs", "goal_objective", "TEXT"),
    ("jobs", "goal_budget", "INTEGER"),
    ("jobs", "goal_max_turns", "INTEGER"),
    ("jobs", "goal_set_on_start", "INTEGER NOT NULL DEFAULT 1"),
    ("jobs", "goal_status", "TEXT"),
    ("jobs", "goal_tokens_used", "INTEGER"),
    ("jobs", "goal_time_used_seconds", "INTEGER"),
    ("jobs", "goal_turns", "INTEGER NOT NULL DEFAULT 0"),
    # `submit --worktree`: the git worktree this job runs in (cwd) and the branch it sits on.
    ("jobs", "worktree_path", "TEXT"),
    ("jobs", "worktree_branch", "TEXT"),
]


def _migrate(conn):
    """Additive column migration. Two processes can race here (audit finding #11): both see the
    column missing and both ALTER. The loser gets "duplicate column name", which is benign --
    the column exists either way, so swallow exactly that error and keep going."""
    for table, col, coltype in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.commit()


def connect(timeout=30.0):
    ensure_state_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_config(conn):
    row = conn.execute("SELECT parallel_limit, hang_timeout_minutes FROM config WHERE id=1").fetchone()
    return dict(row)


class ConfigError(ValueError):
    pass


def set_config(conn, parallel_limit=None, hang_timeout_minutes=None):
    """Range-checked (audit finding #10): parallel_limit < 1 starves every job forever, and
    hang_timeout_minutes < 1 fails running jobs on the next tick."""
    if parallel_limit is not None:
        if parallel_limit < 1:
            raise ConfigError("parallel must be >= 1 (0 or negative would starve every job)")
        conn.execute("UPDATE config SET parallel_limit=? WHERE id=1", (parallel_limit,))
    if hang_timeout_minutes is not None:
        if hang_timeout_minutes < 1:
            raise ConfigError("hang-timeout must be >= 1 minute (0 or negative fails running jobs immediately)")
        conn.execute("UPDATE config SET hang_timeout_minutes=? WHERE id=1", (hang_timeout_minutes,))
    conn.commit()


def find_job_by_slug(conn, slug, active_only=False):
    if active_only:
        row = conn.execute(
            "SELECT * FROM jobs WHERE slug=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
            (slug,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM jobs WHERE slug=? ORDER BY id DESC LIMIT 1", (slug,)
        ).fetchone()
    return dict(row) if row else None


def add_working_on_entry(conn, job_id, text):
    conn.execute("INSERT INTO job_working_on_log (job_id, text) VALUES (?, ?)", (job_id, text))
    conn.commit()


def get_working_on_history(conn, job_id):
    rows = conn.execute(
        "SELECT text, created_at FROM job_working_on_log WHERE job_id=? ORDER BY id ASC", (job_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_deps(conn, job_id):
    rows = conn.execute(
        """SELECT j.slug AS slug, j.status AS status
           FROM job_deps d JOIN jobs j ON j.id = d.depends_on_job_id
           WHERE d.job_id=?""",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def job_dir(job_id, slug):
    return os.path.join(JOBS_DIR, f"{job_id}-{slug}")


def delete_job_and_deps(conn, job_id):
    """Delete a job row along with dependency edges in BOTH directions.

    Audit finding #2: only outgoing edges (job_deps.job_id = me) used to be cleaned up. Incoming
    edges (job_deps.depends_on_job_id = me) survived as dangling rows, and because ready_jobs()
    joined job_deps to jobs, a dangling row matched nothing and the dependent silently became
    "ready" -- launching without the dependency it was supposed to wait for. Foreign keys are not
    enforced (PRAGMA foreign_keys defaults off and enabling it now would fail on pre-existing
    dangling rows), so integrity is maintained here in application code instead.

    Returns the number of job rows actually deleted, so callers can tell a real delete from a
    lost race."""
    conn.execute("DELETE FROM job_deps WHERE job_id=? OR depends_on_job_id=?", (job_id, job_id))
    n = conn.execute("DELETE FROM jobs WHERE id=? AND status='queued'", (job_id,)).rowcount
    return n


def queued_dependents(conn, job_id):
    rows = conn.execute(
        """SELECT j.id AS id, j.slug AS slug FROM job_deps d JOIN jobs j ON j.id = d.job_id
           WHERE d.depends_on_job_id=? AND j.status='queued'""",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fail_queued_dependents(conn, job_id, cause):
    """Deleting a queued job leaves its dependents with nothing to wait for. Removing their edge
    would let them launch immediately -- which is exactly the bug audit finding #2 describes, just
    reached a different way. They are failed instead, transitively, matching how the daemon
    already cascades a genuinely failed dependency. Returns the slugs it failed."""
    failed = []
    frontier = [job_id]
    seen = {job_id}
    while frontier:
        nxt = []
        for jid in frontier:
            for dep in queued_dependents(conn, jid):
                if dep["id"] in seen:
                    continue
                seen.add(dep["id"])
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='queued'",
                    (cause, now_iso(), dep["id"]),
                )
                failed.append(dep["slug"])
                nxt.append(dep["id"])
        frontier = nxt
    conn.commit()
    return failed


def stop_job(conn, job, cause):
    """Shared by scheduler_cli.py's `stop` and the dashboard's cancel button, so both go through
    identical logic. Deletes a still-queued job outright; interrupt+quits a running one and
    records `cause` in its error field (surfaced to the owning session via the next `wait`).

    Returns "removed" / "stopped" only when a row actually changed; None when the daemon won the
    race (audit finding #6 -- this used to report success unconditionally)."""
    if job["status"] == "queued":
        dependents = fail_queued_dependents(
            conn, job["id"], f"dependency '{job['slug']}' was removed before it ran"
        )
        n = delete_job_and_deps(conn, job["id"])
        conn.commit()
        if not n:
            return None
        stop_job.last_cascaded = dependents
        return "removed"
    updated = conn.execute(
        "UPDATE jobs SET status='stopped', error=?, finished_at=? WHERE id=? AND status='running'",
        (cause, now_iso(), job["id"]),
    ).rowcount
    conn.commit()
    if updated and job.get("session_dir"):
        try:
            with open(os.path.join(job["session_dir"], "control"), "a") as f:
                f.write("interrupt\nquit\n")
        except OSError:
            pass
    return "stopped" if updated else None


def job_cwd(job):
    """The directory a job actually runs in: its worktree when it has one, else its workspace.

    `workspace` always stays the main checkout so `list`, `--resume-from` and the worktree
    subcommands can find the repo the lane belongs to."""
    return (job.get("worktree_path") or "").strip() or job["workspace"]


def record_goal(conn, job_id, goal):
    """Persist a ThreadGoal payload (from thread/goal/set, /get or the updated notification)."""
    goal = goal or {}
    conn.execute(
        """UPDATE jobs SET goal_status=?, goal_tokens_used=?, goal_time_used_seconds=?,
                            goal_objective=COALESCE(?, goal_objective),
                            goal_budget=COALESCE(?, goal_budget)
           WHERE id=?""",
        (goal.get("status"), goal.get("tokensUsed"), goal.get("timeUsedSeconds"),
         goal.get("objective"), goal.get("tokenBudget"), job_id),
    )
    conn.commit()


def record_goal_turns(conn, job_id, turns):
    conn.execute("UPDATE jobs SET goal_turns=? WHERE id=?", (turns, job_id))
    conn.commit()


def record_tokens(conn, job_id, usage):
    """Persist a thread/tokenUsage/updated snapshot (cumulative `total` for the thread)."""
    tot = (usage or {}).get("total") or {}
    conn.execute(
        """UPDATE jobs SET tokens_input=?, tokens_cached=?, tokens_output=?,
                            tokens_reasoning=?, tokens_total=?, context_window=?
           WHERE id=?""",
        (
            tot.get("inputTokens"), tot.get("cachedInputTokens"), tot.get("outputTokens"),
            tot.get("reasoningOutputTokens"), tot.get("totalTokens"),
            (usage or {}).get("modelContextWindow"), job_id,
        ),
    )
    conn.commit()


def set_checkpoint(conn, job_id, text):
    conn.execute(
        "UPDATE jobs SET checkpoint=?, checkpoint_at=? WHERE id=?", (text, now_iso(), job_id)
    )
    conn.commit()


def add_ask(conn, job_id, question):
    cur = conn.execute("INSERT INTO job_asks (job_id, question) VALUES (?, ?)", (job_id, question))
    conn.commit()
    return cur.lastrowid


def answer_ask(conn, ask_id, answer):
    conn.execute(
        "UPDATE job_asks SET answer=?, answered_at=? WHERE id=? AND answer IS NULL",
        (answer, now_iso(), ask_id),
    )
    conn.commit()


def get_ask(conn, ask_id):
    row = conn.execute("SELECT * FROM job_asks WHERE id=?", (ask_id,)).fetchone()
    return dict(row) if row else None


def dep_results(conn, job_id):
    """{slug: job-row} for every dependency of job_id -- used to interpolate upstream results
    into a dependent's prompt at launch time."""
    rows = conn.execute(
        """SELECT j.* FROM job_deps d JOIN jobs j ON j.id = d.depends_on_job_id
           WHERE d.job_id=?""",
        (job_id,),
    ).fetchall()
    return {r["slug"]: dict(r) for r in rows}


def list_jobs(conn, session_id=None, status=None):
    q = "SELECT * FROM jobs WHERE 1=1"
    args = []
    if session_id:
        q += " AND claude_session_id=?"
        args.append(session_id)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id ASC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def daemon_pid():
    try:
        with open(DAEMON_PIDFILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def is_pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
