#!/usr/bin/env python3
"""Tests for the Claude Code inbox notifier (intercom.py)."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
import sys
sys.path.insert(0, SCRIPTS)

import intercom  # noqa: E402


class InboxServer:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "inbox.sock")
        self.lines = []
        self._stop = False
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                buf = b""
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line:
                            self.lines.append(json.loads(line.decode()))

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def write_session(sessions_dir, pid, session_id, token, sock_path, bridge=None, name="test-agent"):
    os.makedirs(sessions_dir, exist_ok=True)
    meta = {
        "pid": pid,
        "sessionId": session_id,
        "bridgeSessionId": bridge,
        "messagingSocketPath": sock_path,
        "name": name,
        "kind": "interactive",
    }
    with open(os.path.join(sessions_dir, f"{pid}.json"), "w") as f:
        json.dump(meta, f)
    with open(os.path.join(sessions_dir, f"{pid}.deadbeef.key"), "w") as f:
        json.dump({"peerToken": token}, f)


class IntercomTests(unittest.TestCase):
    def setUp(self):
        self._prev = intercom.SESSIONS_DIR
        self.tmpdir = tempfile.mkdtemp()
        intercom.SESSIONS_DIR = self.tmpdir
        self.inbox = InboxServer()

    def tearDown(self):
        intercom.SESSIONS_DIR = self._prev
        self.inbox.close()

    def test_posts_auth_then_user_on_success_and_error(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_session(self.tmpdir, os.getpid(), sid, "tok-1", self.inbox.path)
        job_ok = {
            "id": 1, "slug": "ok-job", "status": "done",
            "claude_session_id": sid, "result": "all green",
        }
        job_fail = {
            "id": 2, "slug": "bad-job", "status": "failed",
            "claude_session_id": sid, "error": "boom",
        }
        self.assertTrue(intercom.notify_job_settled(job_ok))
        self.assertTrue(intercom.notify_job_settled(job_fail))
        deadline = time.time() + 2
        while len(self.inbox.lines) < 4 and time.time() < deadline:
            time.sleep(0.05)
        types = [x.get("type") for x in self.inbox.lines]
        self.assertEqual(types, ["auth", "user", "auth", "user"])
        self.assertEqual(self.inbox.lines[0]["token"], "tok-1")
        bodies = [x["message"]["content"] for x in self.inbox.lines if x["type"] == "user"]
        self.assertIn("ok-job", bodies[0])
        self.assertIn("done", bodies[0])
        self.assertIn("all green", bodies[0])
        self.assertIn("bad-job", bodies[1])
        self.assertIn("failed", bodies[1])
        self.assertIn("boom", bodies[1])

    def test_matches_bridge_session_id(self):
        sid = "uuid-1"
        bridge = "session_01ABCDEF"
        write_session(self.tmpdir, os.getpid(), sid, "tok-2", self.inbox.path, bridge=bridge)
        found = intercom.find_session(bridge)
        self.assertIsNotNone(found)
        self.assertEqual(found["token"], "tok-2")

    def test_skips_dead_pid(self):
        write_session(self.tmpdir, 1, "uuid-dead", "tok", self.inbox.path)
        self.assertIsNone(intercom.find_session("uuid-dead"))

    def test_format_blocked_and_stopped(self):
        blocked = intercom.format_settled({
            "id": 3, "slug": "epic", "status": "blocked", "result": "need a call",
        })
        self.assertIn("BLOCKED", blocked)
        stopped = intercom.format_settled({
            "id": 4, "slug": "x", "status": "stopped", "error": "budget exceeded",
            "checkpoint": "halfway", "checkpoint_at": "now",
        })
        self.assertIn("budget exceeded", stopped)
        self.assertIn("halfway", stopped)

    def test_unknown_session_is_false_not_exception(self):
        self.assertFalse(intercom.notify_session("no-such-session", "hi"))


if __name__ == "__main__":
    unittest.main()
