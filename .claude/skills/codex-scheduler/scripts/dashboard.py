#!/usr/bin/env python3
"""codex-scheduler live dashboard: stdlib-only HTTP server (no dependencies) serving a JSON API
plus static/index.html, which polls the API to show a live job table + per-job log tail.

Read-only except for one endpoint: POST /api/jobs/<slug>/stop, the dashboard's Cancel button. It
goes through db.stop_job() -- the same code path scheduler_cli.py's `stop` uses -- with a cause
that says the user did it via the dashboard, so the owning Claude session's next `wait` sees why.
"""
import argparse
import json
import os
import socketserver
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import db


def compute_updated_at(job):
    """Best signal for 'last activity': while running, the job's own progress.log mtime (ticks on
    every streamed delta); otherwise the most recent lifecycle timestamp we have."""
    if job["status"] == "running" and job.get("session_dir"):
        log_path = os.path.join(job["session_dir"], "progress.log")
        try:
            mtime = os.path.getmtime(log_path)
        except OSError:
            mtime = None
        if mtime:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
    return job.get("finished_at") or job.get("started_at") or job.get("created_at")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def get_steers(job):
    """All steer: commands ever sent to this job, in order, full text (from the control file --
    the definitive record of what was appended, independent of accept/reject)."""
    if not job.get("session_dir"):
        return []
    path = os.path.join(job["session_dir"], "control")
    if not os.path.exists(path):
        return []
    steers = []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.lower().startswith("steer:"):
                steers.append(line[6:].strip())
    return steers


def job_to_json(conn, job):
    job = dict(job)
    job["deps"] = db.get_deps(conn, job["id"])
    job["steers"] = get_steers(job)
    job["updated_at"] = compute_updated_at(job)
    job["working_on_history"] = db.get_working_on_history(conn, job["id"]) if job["status"] == "running" else []
    return job


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass  # keep dashboard.log quiet; daemon.log carries the real operational trail

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, status=200, content_type="text/plain"):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            path = os.path.join(STATIC_DIR, "index.html")
            if not os.path.exists(path):
                self._text("index.html missing", status=500)
                return
            with open(path) as f:
                self._text(f.read(), content_type="text/html")
            return

        if parts == ["api", "jobs"]:
            conn = db.connect()
            try:
                jobs = db.list_jobs(conn)
                self._json([job_to_json(conn, j) for j in jobs])
            finally:
                conn.close()
            return

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            slug = parts[2]
            conn = db.connect()
            try:
                job = db.find_job_by_slug(conn, slug)
                if not job:
                    self._json({"error": "not found"}, status=404)
                    return
                self._json(job_to_json(conn, job))
            finally:
                conn.close()
            return

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "log":
            slug = parts[2]
            offset = int((qs.get("offset") or ["0"])[0])
            conn = db.connect()
            try:
                job = db.find_job_by_slug(conn, slug)
            finally:
                conn.close()
            if not job or not job.get("session_dir"):
                self._json({"offset": 0, "text": ""})
                return
            path = os.path.join(job["session_dir"], "progress.log")
            text, new_offset = "", offset
            if os.path.exists(path):
                with open(path, errors="replace") as f:
                    f.seek(offset)
                    text = f.read()
                    new_offset = f.tell()
            self._json({"offset": new_offset, "text": text})
            return

        self._text("not found", status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "stop":
            slug = parts[2]
            conn = db.connect()
            try:
                job = db.find_job_by_slug(conn, slug, active_only=True)
                if not job:
                    self._json({"error": "no active job with that slug"}, status=404)
                    return
                result = db.stop_job(conn, job, "stopped by the user via the dashboard")
            finally:
                conn.close()
            self._json({"result": result})
            return
        self._text("not found", status=404)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1234)
    args = ap.parse_args()
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print(f"codex-scheduler dashboard listening on http://127.0.0.1:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
