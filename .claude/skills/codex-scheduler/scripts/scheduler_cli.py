#!/usr/bin/env python3
"""codex-scheduler CLI -- the entrypoint Claude actually calls. See SKILL.md for the recipes.

Subcommands: submit, submit-batch, list, show, wait, steer, stop, rm, edit, reorder, config, ui,
daemon (start|stop|status).
"""
import argparse
import json
import os
import re
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
        if pid and db.is_pid_alive(pid):
            os.kill(pid, 15)
            print(f"sent SIGTERM to daemon pid={pid}")
        else:
            print("daemon not running")


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
                              claude_session_id, priority)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec["slug"], spec["prompt"], spec["workspace"],
            spec.get("model") or "gpt-5.6-sol", spec.get("effort") or "medium",
            1 if spec.get("fast_mode") else 0, spec.get("sandbox") or "workspace-write",
            session_id, spec.get("priority", 0),
        ),
    )
    return cur.lastrowid


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
                conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job_id, dep_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    print(f"submitted job #{job_id} ({spec['slug']})")


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
                    conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job_id, dep_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    for spec in specs:
        print(f"submitted job #{batch_map[spec['slug']]} ({spec['slug']})")


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
        widths = {"id": 4, "slug": 20, "status": 9, "effort": 6, "session": 12, "workspace": 30}
        header = f"{'ID':<{widths['id']}} {'SLUG':<{widths['slug']}} {'STATUS':<{widths['status']}} " \
                 f"{'EFFORT':<{widths['effort']}} {'SESSION':<{widths['session']}} WORKSPACE"
        print(header)
        for j in jobs:
            deps = db.get_deps(conn, j["id"])
            dep_str = "" if not deps else f" deps=[{','.join(d['slug'] for d in deps)}]"
            print(
                f"{j['id']:<{widths['id']}} {j['slug']:<{widths['slug']}} {j['status']:<{widths['status']}} "
                f"{j['effort']:<{widths['effort']}} {j['claude_session_id'][:12]:<{widths['session']}} "
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
    while True:
        conn = db.connect()
        try:
            mq = """SELECT m.id AS mid, m.text AS text, m.created_at AS created_at,
                           j.id AS job_id, j.slug AS slug
                    FROM job_messages m JOIN jobs j ON j.id = m.job_id
                    WHERE j.claude_session_id=? AND m.notified=0"""
            margs = [args.session]
            if slugs:
                mq += f" AND j.slug IN ({','.join('?' * len(slugs))})"
                margs += slugs
            mrows = [dict(r) for r in conn.execute(mq, margs).fetchall()]
            if mrows:
                ids = [r["mid"] for r in mrows]
                conn.execute(f"UPDATE job_messages SET notified=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
                conn.commit()
                for r in mrows:
                    print(f"--- message from job #{r['job_id']} ({r['slug']}) at {r['created_at']} ---")
                    print(r["text"])
                    print()
                return

            q = "SELECT * FROM jobs WHERE claude_session_id=? AND notified=0 AND status IN ('done','failed','stopped')"
            args_l = [args.session]
            if slugs:
                q += f" AND slug IN ({','.join('?' * len(slugs))})"
                args_l += slugs
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
                        _print_result(seen[s])
                    return
                else:
                    ids = [r["id"] for r in rows]
                    conn.execute(f"UPDATE jobs SET notified=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
                    conn.commit()
                    for r in rows:
                        _print_result(r)
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


def _print_result(job):
    print(f"=== job #{job['id']} ({job['slug']}) -> {job['status']} ===")
    if job["status"] == "done":
        print(job.get("result") or "(empty result)")
    else:
        print(f"error: {job.get('error')}")
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
        if job["status"] == "queued":
            conn.execute("DELETE FROM jobs WHERE id=? AND status='queued'", (job["id"],))
            conn.commit()
            print(f"removed queued job #{job['id']} ({args.slug})")
            return
        cause = f"stopped by Claude: {args.reason}" if args.reason else "stopped by Claude"
        conn.execute(
            "UPDATE jobs SET status='stopped', error=?, finished_at=? WHERE id=? AND status='running'",
            (cause, db.now_iso(), job["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    if job["session_dir"]:
        with open(os.path.join(job["session_dir"], "control"), "a") as f:
            f.write("interrupt\nquit\n")
    print(f"stopped job #{job['id']} ({args.slug})")


# ---------------------------------------------------------------- rm / edit / reorder

def cmd_rm(args):
    conn = db.connect()
    try:
        job = db.find_job_by_slug(conn, args.slug, active_only=True)
        if not job:
            err(f"no active job with slug '{args.slug}'")
        if job["status"] != "queued":
            err(f"job '{args.slug}' is '{job['status']}' -- only queued jobs can be removed (use `stop` for running)")
        dependents = conn.execute(
            "SELECT j.slug FROM job_deps d JOIN jobs j ON j.id=d.job_id WHERE d.depends_on_job_id=? AND j.status='queued'",
            (job["id"],),
        ).fetchall()
        if dependents and not args.cascade:
            err(f"job '{args.slug}' has queued dependents {[d['slug'] for d in dependents]}; pass --cascade to remove them too")
        if args.cascade:
            for d in dependents:
                conn.execute("DELETE FROM job_deps WHERE job_id=(SELECT id FROM jobs WHERE slug=?)", (d["slug"],))
                conn.execute("DELETE FROM jobs WHERE slug=? AND status='queued'", (d["slug"],))
        conn.execute("DELETE FROM job_deps WHERE job_id=?", (job["id"],))
        conn.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        conn.commit()
    finally:
        conn.close()
    print(f"removed job '{args.slug}'" + (" and its queued dependents" if args.cascade else ""))


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
                conn.execute("INSERT INTO job_deps (job_id, depends_on_job_id) VALUES (?, ?)", (job["id"], dep_id))
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
        db.set_config(conn, parallel_limit=args.parallel, hang_timeout_minutes=args.hang_timeout)
        print(json.dumps(db.get_config(conn)))
    finally:
        conn.close()


def cmd_ui(args):
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/jobs", timeout=1)
        print(f"dashboard already running at http://localhost:{args.port}")
        return
    except Exception:
        pass
    script = os.path.join(SCRIPT_DIR, "dashboard.py")
    logf = open(os.path.join(db.STATE_DIR, "dashboard.log"), "a")
    db.ensure_state_dirs()
    subprocess.Popen(
        [sys.executable, script, "--port", str(args.port)],
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, cwd=SCRIPT_DIR,
    )
    time.sleep(0.5)
    print(f"dashboard starting at http://localhost:{args.port}")


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
    s.set_defaults(func=cmd_wait)

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
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("daemon")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.set_defaults(func=cmd_daemon)

    return p


def main():
    args = build_parser().parse_args()
    if args.cmd != "daemon":
        ensure_daemon()
    args.func(args)


if __name__ == "__main__":
    main()
