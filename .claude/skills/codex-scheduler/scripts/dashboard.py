#!/usr/bin/env python3
"""codex-scheduler live dashboard: stdlib-only HTTP server (no dependencies) serving a JSON API
plus static/index.html, which polls the API to show a live job table + per-job log tail.

Read-only against the shared SQLite DB -- never mutates scheduler state.
"""
import argparse
import json
import os
import socketserver
import urllib.parse
from http.server import BaseHTTPRequestHandler

import db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def job_to_json(conn, job):
    job = dict(job)
    job["deps"] = db.get_deps(conn, job["id"])
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


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print(f"codex-scheduler dashboard listening on http://127.0.0.1:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
