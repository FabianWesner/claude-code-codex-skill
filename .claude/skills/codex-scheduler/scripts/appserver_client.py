#!/usr/bin/env python3
"""Codex app-server client for the scheduler daemon.

Adapted from codex-subagent's codex_session.py (same JSON-RPC protocol, same file-based control
plane: <jobdir>/{control,progress.log,result.txt,status.json}) but restructured as an importable
class so the daemon can run several JobSessions concurrently *in one process* (one `codex
app-server` OS subprocess per job) and get a direct in-process callback when a turn finishes,
instead of a separate process polling result.txt.

Kept as its own copy rather than editing codex-subagent's scripts, which are already verified and
in active use.
"""
import json
import os
import subprocess
import threading
import time

import db

SANDBOX_MAP = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {"type": "workspaceWrite"},
    "danger-full-access": {"type": "dangerFullAccess"},
}

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

NOTIFY_PREAMBLE = (
    '[scheduler] Your job slug is "{slug}". If you want to send a status update, ask a question, '
    "or flag something to the Claude session that queued this job WITHOUT ending your turn, run "
    "this shell command:\n"
    '  python3 {cli} notify {slug} "<message>"\n'
    "It delivers immediately and does not end your turn -- keep working after sending it. Your "
    "final reply when you finish this turn still becomes the job's result as usual, so only use "
    "this for something worth surfacing before you're done (a question, a heads-up, an early "
    "finding) -- not for routine narration.\n\n"
)


class Conn:
    """One `codex app-server` subprocess + JSON-RPC 2.0 stdio connection."""

    def __init__(self, on_note, fast_mode=False):
        argv = ["codex", "app-server"]
        if fast_mode:
            # Confirmed via `codex app-server --help`: -c/--config and --enable are accepted
            # before the (optional) subcommand. service_tier/fast_mode per
            # learn.chatgpt.com/docs/agent-configuration/speed.
            argv += ["-c", "service_tier=fast", "--enable", "fast_mode"]
        self.p = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._id, self._resp, self._lock = 0, {}, threading.Lock()
        self._on_note = on_note
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None, timeout=60):
        with self._lock:
            self._id += 1
            rid = self._id
        self._send({"id": rid, "method": method, "params": params or {}})
        start = time.time()
        while rid not in self._resp:
            if self.p.poll() is not None:
                raise RuntimeError("app-server exited")
            if time.time() - start > timeout:
                raise TimeoutError(f"{method} timed out after {timeout}s")
            time.sleep(0.02)
        r = self._resp.pop(rid)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def notify(self, method, params=None):
        self._send({"method": method, "params": params or {}})

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in m and ("result" in m or "error" in m):
                self._resp[m["id"]] = m
            elif "method" in m:
                self._on_note(m.get("method"), m.get("params") or {}, m.get("id"))


class JobSession:
    """Drives one job's Codex turn(s) and mirrors codex_session.py's file control plane so the
    CLI's `steer`/`stop` and the dashboard's log tail work exactly like the existing skill.

    on_done(job, status, text) fires exactly once, in-process, the moment the turn settles
    (status in 'done'/'failed') — the daemon uses this to update the DB immediately.
    """

    def __init__(self, job, on_done):
        self.job = job
        self.on_done = on_done
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None
        self.turn_id = None
        self.buf = []
        self._ctl_offset = 0
        self._settled = False
        self.conn = Conn(self._on_note, fast_mode=bool(job.get("fast_mode")))
        self.pid = self.conn.p.pid

    def log(self, s):
        try:
            self.logf.write(s + "\n")
        except ValueError:
            pass  # file already closed

    def _write_status(self):
        try:
            with open(os.path.join(self.dir, "status.json"), "w") as f:
                json.dump({"thread": self.thread, "active_turn": self.turn_id}, f)
        except OSError:
            pass

    def _on_note(self, method, params, req_id):
        if method == "turn/started":
            self.turn_id = (params.get("turn") or {}).get("id")
            self.buf = []
            self.log(f"[turn started id={self.turn_id}]")
        elif method and method.endswith("/delta") and "delta" in params:
            self.buf.append(params["delta"])
            self.logf.write(params["delta"])
        elif method == "item/started":
            it = params.get("item") or {}
            desc = it.get("command") or it.get("text") or it.get("type") or ""
            if isinstance(desc, list):
                desc = " ".join(map(str, desc))
            if desc:
                self.log(f"\n[item {it.get('type', '?')}] {str(desc)[:200]}")
        elif method == "turn/completed":
            final = "".join(self.buf).strip()
            try:
                with open(os.path.join(self.dir, "result.txt"), "w") as f:
                    f.write(final)
            except OSError:
                pass
            self.turn_id = None
            self.log(f"\n[turn complete -> result.txt, {len(final)} chars]")
            self._settle("done", final)
        elif method == "turn/failed" or (params.get("error") and method and method.startswith("turn/")):
            err = str(params.get("error") or "turn failed")
            self.turn_id = None
            self.log(f"\n[turn failed: {err}]")
            self._settle("failed", err)
        elif req_id is not None:
            try:
                self.conn._send({"id": req_id, "result": {}})
            except Exception:
                pass

    def _settle(self, status, text):
        if self._settled:
            return
        self._settled = True
        try:
            self.on_done(self.job, status, text)
        except Exception as e:  # noqa
            self.log(f"[on_done callback error: {e}]")

    def start_turn(self, prompt):
        """Blocking (thread/start + turn/start RPCs) — call from its own thread."""
        try:
            self.conn.request(
                "initialize", {"clientInfo": {"name": "codex-scheduler", "version": "0.1.0"}}, timeout=60
            )
            self.conn.notify("initialized")
            sb = SANDBOX_MAP.get(self.job.get("sandbox", "workspace-write"), SANDBOX_MAP["workspace-write"])
            th = self.conn.request(
                "thread/start", {"cwd": self.job["workspace"], "approvalPolicy": "never"}, timeout=60
            )
            self.thread = (th.get("thread") or {}).get("id")
            self.log(
                f"[READY thread={self.thread} model={self.job.get('model')} "
                f"sandbox={self.job.get('sandbox')} fast={bool(self.job.get('fast_mode'))}]"
            )
            self._write_status()
            full_prompt = NOTIFY_PREAMBLE.format(slug=self.job["slug"], cli=_CLI_PATH) + prompt
            r = self.conn.request(
                "turn/start",
                {
                    "threadId": self.thread,
                    "input": [{"type": "text", "text": full_prompt}],
                    "model": self.job.get("model") or "gpt-5.6-sol",
                    "effort": self.job.get("effort") or "medium",
                    "sandboxPolicy": sb,
                },
                timeout=60,
            )
            self.turn_id = (r.get("turn") or {}).get("id")
        except Exception as e:  # noqa
            self.log(f"[start_turn error: {e}]")
            self._settle("failed", f"failed to start turn: {e}")

    def steer(self, text):
        if not self.turn_id:
            self.log("[reject steer: no active turn]")
            return
        try:
            self.conn.request(
                "turn/steer",
                {"threadId": self.thread, "expectedTurnId": self.turn_id, "input": [{"type": "text", "text": text}]},
            )
            self.log(f"\n[STEER accepted -> {text[:80]}]")
        except Exception as e:  # noqa
            self.log(f"\n[steer rejected: {e}]")

    def interrupt(self):
        if self.turn_id:
            try:
                self.conn.request("turn/interrupt", {"threadId": self.thread, "turnId": self.turn_id})
                self.log("\n[interrupt sent]")
            except Exception as e:  # noqa
                self.log(f"\n[interrupt failed: {e}]")

    def quit(self):
        try:
            self.conn.p.terminate()
        except Exception:  # noqa
            pass
        try:
            self.logf.close()
        except Exception:  # noqa
            pass

    def poll_control(self):
        """Call periodically from the daemon loop: picks up steer:/interrupt/quit commands
        appended to <dir>/control by `scheduler_cli.py steer|stop`."""
        try:
            sz = os.path.getsize(self.ctl)
        except OSError:
            return
        if sz <= self._ctl_offset:
            return
        with open(self.ctl) as f:
            f.seek(self._ctl_offset)
            new = f.read()
            self._ctl_offset = f.tell()
        for raw in new.splitlines():
            cmd = raw.strip()
            if not cmd:
                continue
            low = cmd.lower()
            if low.startswith("steer:"):
                self.steer(cmd[6:].strip())
            elif low == "interrupt":
                self.interrupt()
            elif low == "quit":
                self.log("[quit]")
                self.quit()

    def last_activity(self):
        try:
            return os.path.getmtime(os.path.join(self.dir, "progress.log"))
        except OSError:
            return time.time()

    def alive(self):
        return self.conn.p.poll() is None
