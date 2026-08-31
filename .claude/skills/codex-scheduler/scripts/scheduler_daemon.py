#!/usr/bin/env python3
"""codex-scheduler daemon: the single global orchestrator.

Ticks every ~2s: recovers from any prior crash, cascades dependency failures, launches ready jobs
up to the configured parallelism limit (one `codex app-server` subprocess per job, driven
in-process via appserver_client.JobSession), watches for hangs, and applies queued
steer/interrupt/quit commands. See the codex-scheduler SKILL.md for the full design rationale.

Singleton-enforced via an flock on daemon.lock so a race between two `ensure_daemon()` callers
never produces two daemons. Not meant to be run by hand — scheduler_cli.py starts it detached.
"""
import calendar
import datetime
import fcntl
import os
import subprocess
import sys
import threading
import time

import db
import appserver_client

TICK_SECONDS = 2.0
CODEX_UPDATE_MARKER = os.path.join(db.STATE_DIR, "last_codex_update_date")
SUMMARY_INTERVAL_SECONDS = 60
SUMMARY_TAIL_LINES = 60


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # job_id -> JobSession

    def add(self, job_id, js):
        with self._lock:
            self._sessions[job_id] = js

    def remove(self, job_id):
        with self._lock:
            return self._sessions.pop(job_id, None)

    def items(self):
        with self._lock:
            return list(self._sessions.items())


def log(msg):
    print(f"[{db.now_iso()}] {msg}", flush=True)


def recover_crashed_jobs(conn):
    """On startup: any 'running' row belongs to a JobSession from a PREVIOUS daemon process we
    have no handle to (its stdio pipes died with that process). Best-effort kill the orphaned pid,
    then mark the job failed -- never silently resume/redo it (matches the chosen no-auto-retry
    policy for job failures)."""
    rows = conn.execute("SELECT id, slug, pid FROM jobs WHERE status='running'").fetchall()
    for r in rows:
        if r["pid"]:
            try:
                os.kill(r["pid"], 15)
            except OSError:
                pass
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
            ("daemon restarted while job was running; state unknown, resubmit if needed", db.now_iso(), r["id"]),
        )
        log(f"recovered: job #{r['id']} ({r['slug']}) marked failed (daemon restart)")
    conn.commit()


def cascade_failed_deps(conn):
    while True:
        rows = conn.execute(
            """SELECT DISTINCT j.id AS id, j.slug AS slug, dj.slug AS dep_slug, dj.status AS dep_status
               FROM jobs j
               JOIN job_deps d ON d.job_id = j.id
               JOIN jobs dj ON dj.id = d.depends_on_job_id
               WHERE j.status = 'queued' AND dj.status IN ('failed', 'stopped')"""
        ).fetchall()
        if not rows:
            return
        for r in rows:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='queued'",
                (f"dependency '{r['dep_slug']}' {r['dep_status']}", db.now_iso(), r["id"]),
            )
            log(f"cascaded failure: job #{r['id']} ({r['slug']}) <- dep '{r['dep_slug']}' {r['dep_status']}")
        conn.commit()


def reap_dead_processes(conn, state):
    for job_id, js in state.items():
        if js.alive():
            continue
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] == "running":
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
                ("app-server process exited unexpectedly", db.now_iso(), job_id),
            )
            conn.commit()
            log(f"reaped dead process: job #{job_id} marked failed")
        state.remove(job_id)


def make_on_done(state):
    def on_done(job, status, text):
        conn = db.connect()
        try:
            if status == "done":
                conn.execute(
                    "UPDATE jobs SET status='done', result=?, finished_at=? WHERE id=? AND status='running'",
                    (text, db.now_iso(), job["id"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
                    (text, db.now_iso(), job["id"]),
                )
            conn.commit()
        finally:
            conn.close()
        log(f"job #{job['id']} ({job['slug']}) settled: {status}")
        js = state.remove(job["id"])
        if js:
            threading.Thread(target=js.quit, daemon=True).start()

    return on_done


def ready_jobs(conn):
    rows = conn.execute(
        """SELECT j.* FROM jobs j
           WHERE j.status = 'queued'
             AND NOT EXISTS (
               SELECT 1 FROM job_deps d JOIN jobs dj ON dj.id = d.depends_on_job_id
               WHERE d.job_id = j.id AND dj.status != 'done'
             )
           ORDER BY j.priority ASC, j.id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def launch_ready_jobs(conn, state, on_done):
    cfg = db.get_config(conn)
    running_count = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='running'").fetchone()["c"]
    for job in ready_jobs(conn):
        if running_count >= cfg["parallel_limit"]:
            break
        updated = conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (db.now_iso(), job["id"]),
        ).rowcount
        conn.commit()
        if not updated:
            continue  # raced with something else; skip, will be picked up next tick if still queued
        js = appserver_client.JobSession(job, on_done)
        state.add(job["id"], js)
        conn.execute(
            "UPDATE jobs SET session_dir=?, pid=? WHERE id=?", (js.dir, js.pid, job["id"])
        )
        conn.commit()
        log(f"launched job #{job['id']} ({job['slug']}) pid={js.pid}")
        threading.Thread(target=js.start_turn, args=(job["prompt"],), daemon=True).start()
        running_count += 1


def check_hangs(conn, state):
    cfg = db.get_config(conn)
    timeout_s = cfg["hang_timeout_minutes"] * 60
    now = time.time()
    for job_id, js in state.items():
        if now - js.last_activity() <= timeout_s:
            continue
        updated = conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
            (f"hang timeout: no activity for {cfg['hang_timeout_minutes']} minutes", db.now_iso(), job_id),
        ).rowcount
        conn.commit()
        if updated:
            log(f"hang timeout: job #{job_id} marked failed")
            js2 = state.remove(job_id)
            if js2:
                threading.Thread(target=js2.quit, daemon=True).start()


# ---------------------------------------------------------------- daily Codex CLI auto-update

_update_state = {"done_date": None, "in_progress": False}


def _run_codex_update():
    today = datetime.date.today().isoformat()
    log(f"updating codex CLI (npm install -g @openai/codex) -- first run today ({today})")
    try:
        result = subprocess.run(
            ["npm", "install", "-g", "@openai/codex"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=180, text=True,
        )
        tail = "\n".join((result.stdout or "").splitlines()[-5:])
        log(f"codex update finished (exit={result.returncode}): {tail}")
    except Exception as e:  # noqa - a failed/slow update must never take the daemon down
        log(f"codex update failed: {e}")
    _update_state["done_date"] = today
    _update_state["in_progress"] = False
    try:
        with open(CODEX_UPDATE_MARKER, "w") as f:
            f.write(today)
    except OSError:
        pass


def ensure_codex_updated_today():
    """Gates new job launches (not already-running jobs) until the first update attempt of the
    day completes. Runs in a background thread so it never blocks reap/hang-check/steer polling
    for jobs already in flight -- only launch_ready_jobs waits on it."""
    today = datetime.date.today().isoformat()
    if _update_state["done_date"] is None:
        try:
            with open(CODEX_UPDATE_MARKER) as f:
                _update_state["done_date"] = f.read().strip()
        except OSError:
            _update_state["done_date"] = None
    if _update_state["done_date"] == today or _update_state["in_progress"]:
        return
    _update_state["in_progress"] = True
    threading.Thread(target=_run_codex_update, daemon=True).start()


def codex_update_due_today():
    return _update_state["done_date"] != datetime.date.today().isoformat()


# ---------------------------------------------------------------- "working on" summaries

_SHOWN_ITEM_TYPES = {"agentMessage", "commandExecution"}
_summarizing = set()  # job ids with a summary call currently in flight


def _filter_log_for_summary(text):
    """Mirrors static/index.html's filterLog() in Python: keep only agentMessage/commandExecution
    blocks (plus non-item marker lines) so the cheap summarizer sees the same signal a human
    reading the filtered dashboard view would."""
    current = None
    out = []
    for line in text.split("\n"):
        if line.startswith("[item "):
            end = line.find("]")
            current = line[6:end] if end > 6 else None
            if current in _SHOWN_ITEM_TYPES:
                out.append(line)
            continue
        if line.startswith("["):
            current = None
            out.append(line)
            continue
        if current is None or current in _SHOWN_ITEM_TYPES:
            out.append(line)
    return "\n".join(out)


def _parse_iso_epoch(s):
    if not s:
        return 0
    s = s.rstrip("Z").split(".")[0]
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _run_summary(job_id, workspace, log_text):
    try:
        tail = "\n".join(_filter_log_for_summary(log_text).splitlines()[-SUMMARY_TAIL_LINES:]).strip()
        if tail:
            prompt = (
                "You are looking at a recent excerpt of Codex agent activity (tool calls and "
                "messages) from an in-progress coding task. Write ONE short, user-friendly "
                "sentence (under 12 words), present tense, describing what it is currently doing "
                '-- e.g. "Currently testing the Android mobile app" or "Fixing a failing test in '
                'the payment module". No preamble, no quotes, just the sentence.\n\n'
                f"--- recent activity ---\n{tail}"
            )
            out_dir = os.path.join(db.STATE_DIR, "summaries")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{job_id}.txt")
            subprocess.run(
                ["codex", "exec", "-C", workspace, "-m", "gpt-5.6-luna",
                 "-c", "model_reasoning_effort=low", "-s", "read-only", "-o", out_path, prompt],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=45,
            )
            summary = ""
            if os.path.exists(out_path):
                with open(out_path) as f:
                    summary = (f.read().strip().splitlines() or [""])[0][:140]
            if summary:
                conn = db.connect()
                try:
                    conn.execute(
                        "UPDATE jobs SET working_on=?, working_on_updated_at=? WHERE id=? AND status='running'",
                        (summary, db.now_iso(), job_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
    except Exception as e:  # noqa - a summary is a nice-to-have, never worth crashing the daemon
        log(f"summary failed for job #{job_id}: {e}")
    finally:
        _summarizing.discard(job_id)


def maybe_summarize(conn, state):
    now = time.time()
    for job_id, js in state.items():
        if job_id in _summarizing:
            continue
        row = conn.execute(
            "SELECT working_on_updated_at, workspace FROM jobs WHERE id=? AND status='running'", (job_id,)
        ).fetchone()
        if not row:
            continue
        if row["working_on_updated_at"] and now - _parse_iso_epoch(row["working_on_updated_at"]) < SUMMARY_INTERVAL_SECONDS:
            continue
        try:
            with open(os.path.join(js.dir, "progress.log")) as f:
                log_text = f.read()
        except OSError:
            continue
        if not log_text.strip():
            continue
        _summarizing.add(job_id)
        threading.Thread(target=_run_summary, args=(job_id, row["workspace"], log_text), daemon=True).start()


def tick(state, on_done):
    conn = db.connect()
    try:
        reap_dead_processes(conn, state)
        cascade_failed_deps(conn)
        ensure_codex_updated_today()
        if not codex_update_due_today():
            launch_ready_jobs(conn, state, on_done)
        check_hangs(conn, state)
        maybe_summarize(conn, state)
    finally:
        conn.close()
    for _job_id, js in state.items():
        js.poll_control()


def main():
    db.ensure_state_dirs()
    lockf = open(db.DAEMON_LOCKFILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another daemon instance already holds the lock, exiting")
        return 0
    with open(db.DAEMON_PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log(f"daemon started pid={os.getpid()}")

    state = State()
    on_done = make_on_done(state)
    conn = db.connect()
    try:
        recover_crashed_jobs(conn)
    finally:
        conn.close()

    try:
        while True:
            try:
                tick(state, on_done)
            except Exception as e:  # noqa - never let one bad tick kill the daemon
                log(f"tick error: {e}")
            time.sleep(TICK_SECONDS)
    finally:
        try:
            os.remove(db.DAEMON_PIDFILE)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)
