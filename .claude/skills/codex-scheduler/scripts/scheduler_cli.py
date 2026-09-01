#!/usr/bin/env python3
"""codex-scheduler CLI -- the entrypoint Claude actually calls. See SKILL.md for the recipes.

Subcommands: submit, submit-batch, list, show, wait, ask, answer, checkpoint, watch, usage,
notify, steer, stop, rm, edit, reorder, config, ui, daemon (start|stop|status).
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

import db

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def err(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- daemon lifecycle

DASHBOARD_PORT = 1234


def ensure_dashboard(port=DASHBOARD_PORT):
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=1)
        return False  # already running
    except Exception:
        pass
    db.ensure_state_dirs()
    script = os.path.join(SCRIPT_DIR, "dashboard.py")
    logf = open(os.path.join(db.STATE_DIR, "dashboard.log"), "a")
    subprocess.Popen(
        [sys.executable, script, "--port", str(port)],
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=SCRIPT_DIR,
    )
    return True


def ensure_daemon():
    pid = db.daemon_pid()
    if pid and db.is_pid_alive(pid):
        return
    db.ensure_state_dirs()
    script = os.path.join(SCRIPT_DIR, "scheduler_daemon.py")
    logf = open(db.DAEMON_LOG, "a")
    subprocess.Popen(
        [sys.executable, script],
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
        start_new_session=True, cwd=SCRIPT_DIR,
    )
    _wait_for_daemon(timeout=5)
    # the daemon only gets (re)spawned here, so this is naturally "first use" of the skill in
    # this run -- piggyback the dashboard's auto-start on it rather than checking on every call.
    if ensure_dashboard():
        print(f"dashboard started at http://localhost:{DASHBOARD_PORT}", file=sys.stderr)


def _wait_for_daemon(timeout):
    start = time.time()
    while time.time() - start < timeout:
        pid = db.daemon_pid()
        if pid and db.is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def cmd_daemon(args):
    if args.action == "status":
        pid = db.daemon_pid()
        alive = db.is_pid_alive(pid)
        print(json.dumps({"pid": pid, "alive": alive}))
    elif args.action == "start":
        ensure_daemon()
        pid = db.daemon_pid()
        print(f"daemon running, pid={pid}" if db.is_pid_alive(pid) else "failed to start daemon")
    elif args.action == "stop":
        pid = db.daemon_pid()
        if not (pid and db.is_pid_alive(pid)):
            print("daemon not running")
            return
        if args.graceful:
            with open(db.DRAIN_MARKER, "w") as f:
                f.write(db.now_iso())
            print("draining: no new jobs will launch; waiting for running ones to finish naturally...")
            start = time.time()
            while True:
                conn = db.connect()
                running = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='running'").fetchone()["c"]
                conn.close()
                if running == 0:
                    break
                if args.timeout and time.time() - start > args.timeout:
                    print(f"gave up after {args.timeout}s with {running} job(s) still running -- "
                          f"NOT stopping (rerun without --graceful to force, or wait and retry)")
                    try:
                        os.remove(db.DRAIN_MARKER)
                    except OSError:
                        pass
                    return
                time.sleep(2)
        os.kill(pid, 15)
        try:
            os.remove(db.DRAIN_MARKER)
        except OSError:
            pass
        print(f"sent SIGTERM to daemon pid={pid}")


# ---------------------------------------------------------------- validation / helpers

def validate_slug(slug):
    if not slug or not SLUG_RE.match(slug):
        err(f"invalid slug '{slug}': only letters, digits, '-', '_' allowed")
    return slug


def validate_job_spec(spec):
    validate_slug(spec["slug"])
    if not spec.get("prompt", "").strip():
        err(f"job '{spec['slug']}': prompt is required")
    workspace = spec.get("workspace")
    if not workspace or not os.path.isabs(workspace):
        err(f"job '{spec['slug']}': workspace must be an absolute path")
    if not os.path.isdir(workspace):
        err(f"job '{spec['slug']}': workspace '{workspace}' is not a directory")
    effort = spec.get("effort", "medium")
    if effort not in db.VALID_EFFORT:
        err(f"job '{spec['slug']}': effort must be one of {db.VALID_EFFORT}")
    sandbox = spec.get("sandbox", "workspace-write")
    if sandbox not in db.VALID_SANDBOX:
        err(f"job '{spec['slug']}': sandbox must be one of {db.VALID_SANDBOX}")
    if spec.get("max_seconds") is not None and spec["max_seconds"] < 1:
        err(f"job '{spec['slug']}': max-seconds must be >= 1")
    if spec.get("result_schema"):
        try:
            json.loads(spec["result_schema"])
        except json.JSONDecodeError as e:
            err(f"job '{spec['slug']}': --schema is not valid JSON ({e})")


def check_no_cycles(edges, label_of):
    """edges: {job_id: [dep_id, ...]} for the jobs being submitted/edited, merged with what is
    already in the DB. Audit finding #3: cycles (and self-dependencies) were accepted silently and
    left every job in the cycle queued forever, since nothing was ever 'done' to unblock them and
    the failure cascade only triggers on a failed dependency."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {}
    stack = []

    def visit(n):
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, []):
            if color.get(m, WHITE) == GREY:
                cyc = stack[stack.index(m):] + [m]
                err("dependency cycle: " + " -> ".join(label_of(x) for x in cyc))
            if color.get(m, WHITE) == WHITE:
                visit(m)
        stack.pop()
        color[n] = BLACK

    for n in list(edges):
        if color.get(n, WHITE) == WHITE:
            visit(n)


def all_edges(conn, extra=None):
    """Every dependency edge currently in the DB, plus any not-yet-committed ones."""
    edges = {}
    for r in conn.execute("SELECT job_id, depends_on_job_id FROM job_deps").fetchall():
        edges.setdefault(r["job_id"], []).append(r["depends_on_job_id"])
    for jid, deps in (extra or {}).items():
        edges.setdefault(jid, []).extend(deps)
    return edges


def slug_of(conn):
    cache = {}

    def get(jid):
        if jid not in cache:
            row = conn.execute("SELECT slug FROM jobs WHERE id=?", (jid,)).fetchone()
            cache[jid] = row["slug"] if row else f"#{jid}"
        return cache[jid]

    return get


def resolve_dep(conn, slug, batch_map):
    if slug in batch_map:
        return batch_map[slug]
    row = db.find_job_by_slug(conn, slug, active_only=True) or db.find_job_by_slug(conn, slug)
    if not row:
        err(f"dependency slug '{slug}' not found (not in this batch and no job with that slug exists)")
    return row["id"]


def insert_job(conn, session_id, spec):
    cur = conn.execute(
        """INSERT INTO jobs (slug, prompt, workspace, model, effort, fast_mode, sandbox,
                              claude_session_id, priority, result_schema, cite_mode, max_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec["slug"], spec["prompt"], spec["workspace"],
            spec.get("model") or "gpt-5.6-sol", spec.get("effort") or "medium",
            1 if spec.get("fast_mode") else 0, spec.get("sandbox") or "workspace-write",
            session_id, spec.get("priority", 0),
            spec.get("result_schema"), 1 if spec.get("cite") else 0, spec.get("max_seconds"),
        ),
    )
    return cur.lastrowid


def load_schema_arg(val):
    """--schema takes either inline JSON or a path to a .json file."""
    if not val:
        return None
    if os.path.exists(val):
        with open(val) as f:
            return f.read()
    return val


# ---------------------------------------------------------------- submit / submit-batch

def cmd_submit(args):
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file) as f:
            prompt = f.read()
    spec = {
        "slug": args.slug, "prompt": prompt, "workspace": os.path.abspath(args.workspace),
        "model": args.model, "effort": args.effort, "fast_mode": args.fast,
        "sandbox": args.sandbox, "priority": args.priority,
        "result_schema": load_schema_arg(args.schema), "cite": args.cite,
        "max_seconds": args.max_seconds,
    }
    validate_job_spec(spec)
    conn = db.connect()
    try:
        try:
            existing = db.find_job_by_slug(conn, spec["slug"], active_only=True)
            if existing:
                err(f"slug '{spec['slug']}' is already queued/running (job #{existing['id']})")
            job_id = insert_job(conn, args.session, spec)
            for dep_slug in (args.deps.split(",") if args.deps else []):
                dep_slug = dep_slug.strip()
                if not dep_slug:
                    continue
                dep_id = resolve_dep(conn, dep_slug, {})
                if dep_id == job_id:
                    err(f"job '{spec['slug']}' cannot depend on itself")
                conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job_id, dep_id))
            check_no_cycles(all_edges(conn), slug_of(conn))
            conn.commit()
        except sqlite3.IntegrityError:
            # audit finding #15: the unique active-slug index is the real guard; the lookup above
            # is only advisory, so a concurrent submit of the same slug lands here. Report it the
            # same way rather than dumping a traceback.
            conn.rollback()
            err(f"slug '{spec['slug']}' is already queued/running (lost a race with another submit)")
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    extras = [f"sandbox={spec['sandbox']}"]
    if spec.get("result_schema"):
        extras.append("schema")
    if spec.get("cite"):
        extras.append("cite")
    if spec.get("max_seconds"):
        extras.append(f"budget={spec['max_seconds']}s")
    print(f"submitted job #{job_id} ({spec['slug']}) [{', '.join(extras)}]")


def cmd_submit_batch(args):
    with open(args.file) as f:
        specs = json.load(f)
    if not isinstance(specs, list) or not specs:
        err("--file must contain a non-empty JSON array of job specs")
    base_dir = os.path.dirname(os.path.abspath(args.file))
    for spec in specs:
        if not spec.get("prompt") and spec.get("prompt_file"):
            pf = spec["prompt_file"]
            if not os.path.isabs(pf):
                pf = os.path.join(base_dir, pf)
            if not os.path.exists(pf):
                err(f"job '{spec.get('slug')}': prompt_file '{pf}' not found")
            with open(pf) as pff:
                spec["prompt"] = pff.read()
        spec["workspace"] = os.path.abspath(spec["workspace"])
        spec.setdefault("priority", 0)
        spec.setdefault("sandbox", "workspace-write")
        if spec.get("schema") and not spec.get("result_schema"):
            # a batch spec may give `schema` as an inline object, a JSON string, or a file path
            spec["result_schema"] = (
                json.dumps(spec["schema"]) if isinstance(spec["schema"], (dict, list))
                else load_schema_arg(spec["schema"])
            )
        validate_job_spec(spec)
    slugs = [s["slug"] for s in specs]
    if len(slugs) != len(set(slugs)):
        err("duplicate slugs within this batch")

    conn = db.connect()
    try:
        try:
            for slug in slugs:
                existing = db.find_job_by_slug(conn, slug, active_only=True)
                if existing:
                    err(f"slug '{slug}' is already queued/running (job #{existing['id']})")
            batch_map = {}
            for spec in specs:
                batch_map[spec["slug"]] = insert_job(conn, args.session, spec)
            for spec in specs:
                job_id = batch_map[spec["slug"]]
                for dep_slug in spec.get("deps", []) or []:
                    dep_id = resolve_dep(conn, dep_slug, batch_map)
                    if dep_id == job_id:
                        err(f"job '{spec['slug']}' cannot depend on itself")
                    conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job_id, dep_id))
            check_no_cycles(all_edges(conn), slug_of(conn))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    for spec in specs:
        print(f"submitted job #{batch_map[spec['slug']]} ({spec['slug']}) [sandbox={spec['sandbox']}]")


# ---------------------------------------------------------------- list / show

def cmd_list(args):
    conn = db.connect()
    try:
        jobs = db.list_jobs(conn, session_id=args.session, status=args.status)
        if args.json:
            print(json.dumps(jobs, indent=2))
            return
        if not jobs:
            print("(no jobs)")
            return
        widths = {"id": 4, "slug": 20, "status": 9, "effort": 6, "sandbox": 18, "session": 12, "workspace": 30}
        header = f"{'ID':<{widths['id']}} {'SLUG':<{widths['slug']}} {'STATUS':<{widths['status']}} " \
                 f"{'EFFORT':<{widths['effort']}} {'SANDBOX':<{widths['sandbox']}} " \
                 f"{'SESSION':<{widths['session']}} WORKSPACE"
        print(header)
        for j in jobs:
            deps = db.get_deps(conn, j["id"])
            dep_str = "" if not deps else f" deps=[{','.join(d['slug'] for d in deps)}]"
            print(
                f"{j['id']:<{widths['id']}} {j['slug']:<{widths['slug']}} {j['status']:<{widths['status']}} "
                f"{j['effort']:<{widths['effort']}} {j['sandbox']:<{widths['sandbox']}} "
                f"{j['claude_session_id'][:12]:<{widths['session']}} "
                f"{j['workspace']}{dep_str}"
            )
    finally:
        conn.close()


def _tail(path, n=40):
    if not path or not os.path.exists(path):
        return "(no log yet)"
    with open(path, errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


def cmd_show(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug)
        if not job:
            err(f"no job with slug '{args.slug}'")
        deps = db.get_deps(conn, job["id"])
    finally:
        conn.close()
    print(json.dumps(job, indent=2))
    if deps:
        print("\ndeps:")
        for d in deps:
            print(f"  - {d['slug']} ({d['status']})")
    if job.get("session_dir"):
        print(f"\n--- progress.log (last 40 lines) ---")
        print(_tail(os.path.join(job["session_dir"], "progress.log")))


# ---------------------------------------------------------------- wait

def cmd_wait(args):
    slugs = [s.strip() for s in args.slugs.split(",")] if args.slugs else None
    start = time.time()
    seen = {}
    # Per-process message dedup. `notified` is a single global flag, so with several waiters
    # interested in the same job it cannot also serve as this process's "already printed" record.
    seen_msgs = set()
    while True:
        conn = db.connect()
        try:
            mq = """SELECT m.id AS mid, m.text AS text, m.created_at AS created_at,
                           j.id AS job_id, j.slug AS slug
                    FROM job_messages m JOIN jobs j ON j.id = m.job_id
                    WHERE j.claude_session_id=?"""
            margs = [args.session]
            if slugs:
                # Explicit slugs: deliver every message for those jobs to THIS waiter, deduped
                # locally. Filtering on the shared `notified` flag here means whichever waiter
                # reads first consumes the message and every other waiter watching the same job
                # never sees it.
                mq += f" AND j.slug IN ({','.join('?' * len(slugs))})"
                margs += slugs
                if seen_msgs:
                    mq += f" AND m.id NOT IN ({','.join('?' * len(seen_msgs))})"
                    margs += sorted(seen_msgs)
            else:
                mq += " AND m.notified=0"
            mrows = [dict(r) for r in conn.execute(mq, margs).fetchall()]
            if mrows:
                ids = [r["mid"] for r in mrows]
                conn.execute(f"UPDATE job_messages SET notified=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
                conn.commit()
                seen_msgs.update(r["mid"] for r in mrows)
                for r in mrows:
                    print(f"--- message from job #{r['job_id']} ({r['slug']}) at {r['created_at']} ---")
                    print(r["text"])
                    print()
                if not args.follow:
                    return
                # --follow: keep the connection loop going -- fall through to the terminal-status
                # check below so a job settling right after a message still ends the wait.

            # The `notified` flag means "this session has been told about it at least once". It is
            # the right filter for a catch-all wait, but NOT when specific slugs were requested:
            # with --all a waiter holds finished slugs in `seen` until the whole set lands, and if
            # another waiter marks one notified in the meantime it can never reappear here, so the
            # set never completes and the wait blocks forever. Naming a slug means "tell me about
            # this job", regardless of who else was told.
            q = "SELECT * FROM jobs WHERE claude_session_id=? AND status IN ('done','failed','stopped')"
            args_l = [args.session]
            if slugs:
                q += f" AND slug IN ({','.join('?' * len(slugs))})"
                args_l += slugs
            else:
                q += " AND notified=0"
            rows = [dict(r) for r in conn.execute(q, args_l).fetchall()]
            if rows:
                if slugs and args.all:
                    for r in rows:
                        seen[r["slug"]] = r
                    if not all(s in seen for s in slugs):
                        conn.close()
                        if args.timeout and time.time() - start > args.timeout:
                            err(f"timeout waiting for {slugs}; got so far: {list(seen)}")
                        time.sleep(1)
                        continue
                    ids = [seen[s]["id"] for s in slugs]
                    conn2 = db.connect()
                    conn2.execute(f"UPDATE jobs SET notified=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
                    conn2.commit()
                    conn2.close()
                    for s in slugs:
                        _print_result(seen[s], as_json=args.json)
                    return
                else:
                    ids = [r["id"] for r in rows]
                    conn.execute(f"UPDATE jobs SET notified=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
                    conn.commit()
                    for r in rows:
                        _print_result(r, as_json=args.json)
                    return
        finally:
            conn.close()
        if args.timeout and time.time() - start > args.timeout:
            err("timeout waiting for job(s) to finish")
        time.sleep(1)


# ---------------------------------------------------------------- notify (called BY Codex)

def cmd_notify(args):
    """Called from *inside* a running job's own shell (Codex invokes this itself -- see the
    notify preamble every job prompt gets, in appserver_client.py) to send Claude an ad-hoc
    message without ending the turn. Delivered through the same channel as job completion:
    the next `wait` for that job's session returns it immediately."""
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active job with slug '{args.slug}'")
        conn.execute("INSERT INTO job_messages (job_id, text) VALUES (?, ?)", (job["id"], args.text))
        conn.commit()
    finally:
        conn.close()
    if job.get("session_dir"):
        try:
            with open(os.path.join(job["session_dir"], "progress.log"), "a") as f:
                f.write(f"\n[message] {args.text}\n")
        except OSError:
            pass
    print(f"message queued for job #{job['id']} ({args.slug})")


def _job_payload(job):
    """Machine-readable form of a settled job -- what `wait --json` emits."""
    out = {
        "id": job["id"], "slug": job["slug"], "status": job["status"],
        "model": job.get("model"), "effort": job.get("effort"), "sandbox": job.get("sandbox"),
        "result": job.get("result"), "error": job.get("error"),
        "checkpoint": job.get("checkpoint"),
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
        "tokens": {
            "input": job.get("tokens_input"), "cached": job.get("tokens_cached"),
            "output": job.get("tokens_output"), "reasoning": job.get("tokens_reasoning"),
            "total": job.get("tokens_total"), "context_window": job.get("context_window"),
        },
    }
    if job.get("result_schema"):
        out["schema_error"] = job.get("schema_error")
        try:
            out["result_json"] = json.loads(job["result_json"]) if job.get("result_json") else None
        except (json.JSONDecodeError, TypeError):
            out["result_json"] = None
    return out


def _fmt_tokens(job):
    t = job.get("tokens_total")
    if not t:
        return ""
    bits = [f"total={t:,}"]
    if job.get("tokens_input"):
        bits.append(f"in={job['tokens_input']:,}")
    if job.get("tokens_cached"):
        bits.append(f"cached={job['tokens_cached']:,}")
    if job.get("tokens_output"):
        bits.append(f"out={job['tokens_output']:,}")
    if job.get("tokens_reasoning"):
        bits.append(f"reasoning={job['tokens_reasoning']:,}")
    return "  ".join(bits)


# ---------------------------------------------------------------- ask / answer / checkpoint

def cmd_ask(args):
    """Put a question to a RUNNING job and block for its reply.

    Unlike `notify` (which depends on Codex volunteering something) and `steer` (which is
    write-only), this is a request/response: the question is steered in, and the daemon captures
    the next agent message as the answer. Codex keeps working afterwards -- the turn is not
    ended and the answer is NOT part of the job's final result."""
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active (queued/running) job with slug '{args.slug}'")
        if job["status"] != "running":
            err(f"job '{args.slug}' is '{job['status']}', not running -- nothing to ask")
        if not job.get("session_dir"):
            err(f"job '{args.slug}' has no session dir yet; try again in a moment")
        ask_id = db.add_ask(conn, job["id"], args.text)
    finally:
        conn.close()

    one_line = " ".join(args.text.split())
    with open(os.path.join(job["session_dir"], "control"), "a") as f:
        f.write(f"ask:{ask_id}:{one_line}\n")

    deadline = time.time() + (args.timeout or 120)
    while time.time() < deadline:
        conn = db.connect()
        try:
            row = db.get_ask(conn, ask_id)
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        finally:
            conn.close()
        if row and row["answer"]:
            print(f"=== answer from job #{job['id']} ({args.slug}) ===")
            print(row["answer"])
            return
        if status and status["status"] != "running":
            err(f"job '{args.slug}' settled ({status['status']}) before answering -- see `show {args.slug}`")
        time.sleep(1)
    err(f"no answer within {args.timeout or 120}s (question #{ask_id} is still pending; "
        f"Codex may answer it later -- it is recorded in the job's log)")


def cmd_answer(args):
    """Explicit path for Codex to answer an `ask` (the daemon also captures the next agent
    message automatically, so this is a belt-and-braces fallback)."""
    conn = db.connect()
    try:
        row = db.get_ask(conn, args.ask_id)
        if not row:
            err(f"no such question id {args.ask_id}")
        db.answer_ask(conn, args.ask_id, args.text)
    finally:
        conn.close()
    print(f"answered question #{args.ask_id}")


def cmd_checkpoint(args):
    """Called by Codex from inside a running job to save recoverable progress. If the job is
    later stopped, times out, or blows its budget, this is what survives."""
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active job with slug '{args.slug}'")
        db.set_checkpoint(conn, job["id"], args.text)
    finally:
        conn.close()
    if job.get("session_dir"):
        try:
            with open(os.path.join(job["session_dir"], "progress.log"), "a") as f:
                f.write(f"\n[checkpoint] {args.text[:200]}\n")
        except OSError:
            pass
    print(f"checkpoint saved for job #{job['id']} ({args.slug}) ({len(args.text)} chars)")


# ---------------------------------------------------------------- watch

def cmd_watch(args):
    """Block until something worth waking up for happens, then exit.

    Built for backgrounding: Claude Code notifies the submitting session when a backgrounded Bash
    command exits, so this replaces polling the log on a timer (which costs one full model
    invocation per tick, mostly to learn that nothing changed). Exits on the FIRST of:
      --match REGEX   new log content matches
      --flat-for N    the log has not grown for N seconds (a real stall, not deep thinking)
      job settles
      --timeout N
    """
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug)
        if not job:
            err(f"no job with slug '{args.slug}'")
    finally:
        conn.close()
    pattern = re.compile(args.match) if args.match else None

    def resolve_log(j):
        return os.path.join(j["session_dir"], "progress.log") if j.get("session_dir") else None

    # A job watched while it is still QUEUED has no session_dir yet. Resolving it only once here
    # left the watcher permanently blind (log always 0B, no --match could ever fire), so it is
    # re-resolved on every tick until the daemon assigns one.
    log_path = resolve_log(job)

    def size():
        try:
            return os.path.getsize(log_path) if log_path else 0
        except OSError:
            return 0

    start = time.time()
    offset = size() if args.since_now else 0
    last_size, last_growth = size(), time.time()
    matched = None

    while True:
        time.sleep(args.interval)
        if log_path is None:
            c0 = db.connect()
            try:
                row0 = c0.execute("SELECT session_dir FROM jobs WHERE id=?", (job["id"],)).fetchone()
            finally:
                c0.close()
            if row0 and row0["session_dir"]:
                log_path = resolve_log({"session_dir": row0["session_dir"]})
                if args.since_now:
                    offset = 0  # the job started after we did; its whole log is new to us
                last_size, last_growth = 0, time.time()
        cur = size()
        if cur > last_size:
            last_growth = time.time()
            if pattern and log_path:
                with open(log_path, errors="replace") as f:
                    f.seek(last_size)
                    chunk = f.read()
                m = pattern.search(chunk)
                if m:
                    matched = m.group(0)
            last_size = cur

        conn = db.connect()
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        finally:
            conn.close()
        status = row["status"] if row else "gone"

        reason = None
        if matched:
            reason = f"match: {matched[:200]!r}"
        elif status not in ("queued", "running"):
            reason = f"job settled: {status}"
        elif args.flat_for and time.time() - last_growth >= args.flat_for:
            reason = f"stalled: no log growth for {int(time.time() - last_growth)}s"
        elif args.timeout and time.time() - start > args.timeout:
            reason = f"timeout after {int(time.time() - start)}s"

        if reason:
            print(f"=== watch {args.slug} woke: {reason} ===")
            print(f"status={status} elapsed={int(time.time() - start)}s log={cur}B")
            if log_path and cur > offset:
                with open(log_path, errors="replace") as f:
                    f.seek(offset)
                    tail = f.read()
                print("--- new output ---")
                print(tail[-args.max_bytes:])
            return


# ---------------------------------------------------------------- usage

def _fmt_reset(ts):
    if not ts:
        return "unknown"
    delta = ts - time.time()
    if delta <= 0:
        return "now"
    d, h = int(delta // 86400), int((delta % 86400) // 3600)
    m = int((delta % 3600) // 60)
    return f"in {d}d {h}h" if d else (f"in {h}h {m}m" if h else f"in {m}m")


def fetch_usage(timeout=45):
    """Query the Codex account's live rate limits + token usage via the app-server protocol
    (methods account/rateLimits/read and account/usage/read). Spins up a short-lived app-server;
    it does not touch or depend on any running job."""
    sys.path.insert(0, SCRIPT_DIR)
    import appserver_client

    conn = appserver_client.Conn(lambda m, p, i: None)
    try:
        conn.request("initialize", {"clientInfo": {"name": "codex-scheduler", "version": "0.1.0"}},
                     timeout=timeout)
        conn.notify("initialized")
        out = {}
        for key, method in (("rate_limits", "account/rateLimits/read"), ("usage", "account/usage/read")):
            try:
                out[key] = conn.request(method, {}, timeout=timeout)
            except Exception as e:  # noqa - report per-endpoint rather than failing the whole call
                out[key] = {"error": str(e)}
        return out
    finally:
        try:
            conn.p.terminate()
        except Exception:  # noqa
            pass


def cmd_usage(args):
    data = fetch_usage(timeout=args.timeout)
    if args.json:
        print(json.dumps(data, indent=2))
        return

    rl = (data.get("rate_limits") or {})
    if "error" in rl:
        print(f"rate limits: unavailable ({rl['error']})")
    else:
        snap = rl.get("rateLimits") or {}
        plan = snap.get("planType") or "unknown"
        print(f"=== Codex account usage (plan: {plan}) ===")
        buckets = rl.get("rateLimitsByLimitId") or {"": snap}
        for limit_id, b in buckets.items():
            name = b.get("limitName") or limit_id or "codex"
            for label, w in (("primary", b.get("primary")), ("secondary", b.get("secondary"))):
                if not w:
                    continue
                used = w.get("usedPercent")
                mins = w.get("windowDurationMins")
                window = f"{mins // 1440}d" if mins and mins % 1440 == 0 else (f"{mins // 60}h" if mins else "?")
                bar = "#" * int(round((used or 0) / 5)) + "." * (20 - int(round((used or 0) / 5)))
                print(f"  {name:<24} {label:<9} [{bar}] {used:>3}% of {window} window, "
                      f"resets {_fmt_reset(w.get('resetsAt'))}")
            cr = b.get("credits")
            if cr:
                print(f"  {name:<24} credits   unlimited={cr.get('unlimited')} "
                      f"balance={cr.get('balance')}")
            if b.get("rateLimitReachedType"):
                print(f"  {name:<24} !! LIMIT REACHED: {b['rateLimitReachedType']}")
        resets = rl.get("rateLimitResetCredits") or {}
        if resets.get("availableCount"):
            print(f"  reset credits available: {resets['availableCount']}")

    us = (data.get("usage") or {})
    if "error" in us:
        print(f"token usage: unavailable ({us['error']})")
    else:
        sm = us.get("summary") or {}
        print("--- lifetime ---")
        for label, key in (("lifetime tokens", "lifetimeTokens"), ("peak daily", "peakDailyTokens"),
                           ("current streak (days)", "currentStreakDays"),
                           ("longest streak (days)", "longestStreakDays")):
            v = sm.get(key)
            if v is not None:
                print(f"  {label:<24} {v:,}")
        daily = us.get("dailyUsageBuckets") or []
        if daily:
            recent = daily[-args.days:]
            print(f"--- last {len(recent)} day(s) ---")
            for d in recent:
                print(f"  {d.get('startDate')}  {d.get('tokens', 0):>15,}")


def _print_result(job, as_json=False):
    if as_json:
        print(json.dumps(_job_payload(job), indent=2))
        return
    print(f"=== job #{job['id']} ({job['slug']}) -> {job['status']} ===")
    tok = _fmt_tokens(job)
    if tok:
        print(f"[tokens] {tok}")
    if job["status"] == "done":
        if job.get("schema_error"):
            print(f"[schema] MISMATCH: {job['schema_error']} -- raw result below, result_json is null")
        elif job.get("result_json"):
            print("[schema] validated")
        print(job.get("result") or "(empty result)")
    else:
        print(f"error: {job.get('error')}")
        # a stopped/failed job's checkpoint is the only salvageable output -- surface it here
        # rather than making the caller go dig it out of `show`.
        if job.get("checkpoint"):
            print(f"\n--- last checkpoint (saved {job.get('checkpoint_at')}) ---")
            print(job["checkpoint"])
    print()


# ---------------------------------------------------------------- steer / stop

def cmd_steer(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
    finally:
        conn.close()
    if not job:
        err(f"no active (queued/running) job with slug '{args.slug}'")
    if job["status"] != "running":
        err(f"job '{args.slug}' is '{job['status']}', not running -- nothing to steer")
    with open(os.path.join(job["session_dir"], "control"), "a") as f:
        f.write(f"steer: {args.text}\n")
    print(f"steer sent to job #{job['id']} ({args.slug})")


def cmd_stop(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active (queued/running) job with slug '{args.slug}'")
        cause = f"stopped by Claude: {args.reason}" if args.reason else "stopped by Claude"
        result = db.stop_job(conn, job, cause)
    finally:
        conn.close()
    if result == "removed":
        casc = getattr(db.stop_job, "last_cascaded", None)
        extra = f"; failed {len(casc)} dependent(s): {', '.join(casc)}" if casc else ""
        print(f"removed queued job #{job['id']} ({args.slug}){extra}")
    elif result == "stopped":
        print(f"stopped job #{job['id']} ({args.slug})")
    else:
        # audit finding #6: this used to print "stopped" regardless. Losing the race means the job
        # settled on its own -- say so, because the caller's next move differs completely.
        err(f"job '{args.slug}' was no longer stoppable (it settled first) -- check `show {args.slug}`")


# ---------------------------------------------------------------- rm / edit / reorder

def cmd_rm(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active job with slug '{args.slug}'")
        if job["status"] != "queued":
            err(f"job '{args.slug}' is '{job['status']}' -- only queued jobs can be removed (use `stop` for running)")
        direct = conn.execute(
            "SELECT j.slug FROM job_deps d JOIN jobs j ON j.id=d.job_id WHERE d.depends_on_job_id=? AND j.status='queued'",
            (job["id"],),
        ).fetchall()
        if direct and not args.cascade:
            err(f"job '{args.slug}' has queued dependents {[d['slug'] for d in direct]}; pass --cascade to remove them too")

        # audit finding #2: cascade used to be one level deep, so for A -> B -> C removing A took
        # out A and B but left C pointing at a job that no longer existed -- and C then launched.
        # Walk the whole transitive closure instead.
        victims = []
        if args.cascade:
            seen_ids, frontier = {job["id"]}, [job["id"]]
            while frontier:
                nxt = []
                for jid in frontier:
                    rows = conn.execute(
                        """SELECT j.id AS id, j.slug AS slug FROM job_deps d JOIN jobs j ON j.id=d.job_id
                           WHERE d.depends_on_job_id=? AND j.status='queued'""",
                        (jid,),
                    ).fetchall()
                    for r in rows:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            victims.append(dict(r))
                            nxt.append(r["id"])
                frontier = nxt
            for v in victims:
                db.delete_job_and_deps(conn, v["id"])

        # audit finding #5: the final delete was unconditional, so a job the daemon had just
        # started (or finished) got deleted anyway. Scoped to 'queued' now, and the rowcount is
        # checked so a lost race reports honestly instead of claiming success.
        n = db.delete_job_and_deps(conn, job["id"])
        conn.commit()
        if not n:
            err(f"job '{args.slug}' was no longer queued (the daemon started it first) -- nothing removed")
    finally:
        conn.close()
    extra = f" and {len(victims)} queued dependent(s): {', '.join(v['slug'] for v in victims)}" if victims else ""
    print(f"removed job '{args.slug}'{extra}")


def cmd_edit(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active job with slug '{args.slug}'")
        if job["status"] != "queued":
            err(f"job '{args.slug}' is '{job['status']}' -- only queued jobs can be edited")
        fields, vals = [], []
        if args.prompt is not None:
            fields.append("prompt=?"); vals.append(args.prompt)
        if args.prompt_file is not None:
            with open(args.prompt_file) as f:
                fields.append("prompt=?"); vals.append(f.read())
        if args.priority is not None:
            fields.append("priority=?"); vals.append(args.priority)
        if args.effort is not None:
            if args.effort not in db.VALID_EFFORT:
                err(f"effort must be one of {db.VALID_EFFORT}")
            fields.append("effort=?"); vals.append(args.effort)
        if fields:
            vals.append(job["id"])
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=? AND status='queued'", vals)
        if args.deps is not None:
            conn.execute("DELETE FROM job_deps WHERE job_id=?", (job["id"],))
            for dep_slug in (args.deps.split(",") if args.deps else []):
                dep_slug = dep_slug.strip()
                if not dep_slug:
                    continue
                dep_id = resolve_dep(conn, dep_slug, {})
                if dep_id == job["id"]:
                    err(f"job '{args.slug}' cannot depend on itself")
                conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job["id"], dep_id))
            check_no_cycles(all_edges(conn), slug_of(conn))
        conn.commit()
    finally:
        conn.close()
    print(f"edited job '{args.slug}'")


def cmd_reorder(args):
    conn = db.connect()
    try:
        for i, slug in enumerate(args.slugs):
            job = db.find_job_by_slug(conn, slug, active_only=True)
            if not job:
                err(f"no active job with slug '{slug}'")
            if job["status"] != "queued":
                err(f"job '{slug}' is '{job['status']}' -- only queued jobs can be reordered")
            conn.execute("UPDATE jobs SET priority=? WHERE id=? AND status='queued'", (i, job["id"]))
        conn.commit()
    finally:
        conn.close()
    print(f"reordered: {', '.join(args.slugs)}")


# ---------------------------------------------------------------- config / ui

def cmd_config(args):
    conn = db.connect()
    try:
        if args.parallel is None and args.hang_timeout is None:
            print(json.dumps(db.get_config(conn)))
            return
        try:
            db.set_config(conn, parallel_limit=args.parallel, hang_timeout_minutes=args.hang_timeout)
        except db.ConfigError as e:
            err(str(e))
        print(json.dumps(db.get_config(conn)))
    finally:
        conn.close()


def cmd_ui(args):
    if ensure_dashboard(args.port):
        time.sleep(0.5)
        print(f"dashboard starting at http://localhost:{args.port}")
    else:
        print(f"dashboard already running at http://localhost:{args.port}")


# ---------------------------------------------------------------- argparse wiring

def build_parser():
    p = argparse.ArgumentParser(prog="scheduler_cli.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("--session", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--workspace", required=True)
    pg = s.add_mutually_exclusive_group(required=True)
    pg.add_argument("--prompt")
    pg.add_argument("--prompt-file")
    s.add_argument("--deps", default="")
    s.add_argument("--model", default="gpt-5.6-sol")
    s.add_argument("--effort", default="medium", choices=db.VALID_EFFORT)
    s.add_argument("--fast", action="store_true")
    s.add_argument("--sandbox", default="workspace-write", choices=db.VALID_SANDBOX)
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--schema", help="JSON Schema (inline or a .json path) the FINAL message must "
                                    "match; parsed into result_json and surfaced by `wait --json`")
    s.add_argument("--cite", action="store_true",
                    help="require every claim in the result to carry a checkable anchor "
                         "(file:line, URL + quote, or command + output)")
    s.add_argument("--max-seconds", type=int, dest="max_seconds",
                    help="stop the job once it has run this long; its last checkpoint survives")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("submit-batch")
    s.add_argument("--session", required=True)
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_submit_batch)

    s = sub.add_parser("list")
    s.add_argument("--session")
    s.add_argument("--status", choices=db.VALID_STATUS)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("slug")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("wait")
    s.add_argument("--session", required=True)
    s.add_argument("--slugs")
    s.add_argument("--all", action="store_true")
    s.add_argument("--timeout", type=float, default=0)
    s.add_argument("--follow", action="store_true",
                    help="keep streaming notify messages instead of returning after the first batch; "
                         "still returns as soon as a matching job reaches a terminal status")
    s.add_argument("--json", action="store_true",
                    help="emit the settled job(s) as JSON (result, result_json, tokens, checkpoint)")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("ask", help="ask a running job a question and block for its answer")
    s.add_argument("slug")
    s.add_argument("text")
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("answer", help="(called by Codex) answer a pending question")
    s.add_argument("slug")
    s.add_argument("ask_id", type=int)
    s.add_argument("text")
    s.set_defaults(func=cmd_answer)

    s = sub.add_parser("checkpoint", help="(called by Codex) save recoverable progress")
    s.add_argument("slug")
    s.add_argument("text")
    s.set_defaults(func=cmd_checkpoint)

    s = sub.add_parser("watch", help="block until a job matches a condition, then exit (background this)")
    s.add_argument("slug")
    s.add_argument("--match", help="regex; wake when new log output matches")
    s.add_argument("--flat-for", type=float, default=0, dest="flat_for",
                    help="wake if the log has not grown for N seconds (a stall)")
    s.add_argument("--timeout", type=float, default=0)
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--since-now", action="store_true",
                    help="only report output produced after this command starts")
    s.add_argument("--max-bytes", type=int, default=4000, dest="max_bytes")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("usage", help="live Codex account rate limits + token usage")
    s.add_argument("--json", action="store_true")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--timeout", type=float, default=45)
    s.set_defaults(func=cmd_usage)

    s = sub.add_parser("notify")
    s.add_argument("slug")
    s.add_argument("text")
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("steer")
    s.add_argument("slug")
    s.add_argument("text")
    s.set_defaults(func=cmd_steer)

    s = sub.add_parser("stop")
    s.add_argument("slug")
    s.add_argument("--reason", help="why it's being stopped -- recorded as the job's error/cause")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("rm")
    s.add_argument("slug")
    s.add_argument("--cascade", action="store_true")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("edit")
    s.add_argument("slug")
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--deps")
    s.add_argument("--priority", type=int)
    s.add_argument("--effort", choices=db.VALID_EFFORT)
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("reorder")
    s.add_argument("slugs", nargs="+")
    s.set_defaults(func=cmd_reorder)

    s = sub.add_parser("config")
    s.add_argument("--parallel", type=int)
    s.add_argument("--hang-timeout", type=int)
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("ui")
    s.add_argument("--port", type=int, default=DASHBOARD_PORT)
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("daemon")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.add_argument("--graceful", action="store_true",
                    help="for stop: wait for running jobs to finish naturally instead of killing them")
    s.add_argument("--timeout", type=float, default=0,
                    help="for stop --graceful: give up after N seconds if jobs are still running (0 = wait indefinitely)")
    s.set_defaults(func=cmd_daemon)

    return p


def main():
    args = build_parser().parse_args()
    # `usage` talks to Codex directly and `daemon` manages the daemon itself -- neither should
    # spawn a scheduler daemon as a side effect of being run.
    # `usage` talks to Codex directly; `daemon` manages the daemon itself; and notify/checkpoint/
    # answer are invoked by Codex from INSIDE a running job, which means a daemon already exists
    # by definition -- calling ensure_daemon() there just attempts a write (daemon.log) that a
    # sandboxed job is not allowed to make, turning a working callback into a permission error.
    if args.cmd not in ("daemon", "usage", "notify", "checkpoint", "answer"):
        ensure_daemon()
    args.func(args)


if __name__ == "__main__":
    main()
