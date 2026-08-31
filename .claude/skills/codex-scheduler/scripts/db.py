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

TERMINAL_STATUSES = ("done", "failed", "stopped")

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
  working_on_updated_at TEXT
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

CREATE TABLE IF NOT EXISTS config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  parallel_limit INTEGER NOT NULL DEFAULT 3,
  hang_timeout_minutes INTEGER NOT NULL DEFAULT 30
);
INSERT OR IGNORE INTO config(id) VALUES (1);
"""

VALID_EFFORT = ("low", "medium", "xhigh", "ultra")
VALID_SANDBOX = ("read-only", "workspace-write", "danger-full-access")
VALID_STATUS = ("queued", "running", "done", "failed", "stopped")


def ensure_state_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)


# Columns added after the initial release -- CREATE TABLE IF NOT EXISTS alone won't add these to
# a DB that already exists on disk, so migrate() adds any that are missing.
_ADDED_COLUMNS = [
    ("jobs", "working_on", "TEXT"),
    ("jobs", "working_on_updated_at", "TEXT"),
]


def _migrate(conn):
    for table, col, coltype in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
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


def set_config(conn, parallel_limit=None, hang_timeout_minutes=None):
    if parallel_limit is not None:
        conn.execute("UPDATE config SET parallel_limit=? WHERE id=1", (parallel_limit,))
    if hang_timeout_minutes is not None:
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
