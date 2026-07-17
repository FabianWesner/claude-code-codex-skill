#!/usr/bin/env python3
"""Interactive Codex session daemon (app-server) — lets an orchestration agent drive Codex as a
mid-flight-steerable sub-agent. Runs on the ChatGPT subscription (no API key).

Fire-and-wait `codex exec` can't be corrected once running. This daemon holds ONE
app-server thread and exposes a file-based control plane that fits how an agent works:
  - the agent APPENDS line-commands to <dir>/control
  - the daemon STREAMS Codex's live activity to <dir>/progress.log (tail it with Monitor)
  - final answers land in <dir>/result.txt

Control commands (one per line, appended to <dir>/control):
  turn: <prompt>   start a new turn
  steer: <text>    inject text into the ACTIVE turn (mid-flight correction)
  interrupt        stop the active turn
  status           dump state to <dir>/status.json
  quit             shut down

Usage:
  codex_session.py --dir <session-dir> --cwd <repo> [--model gpt-5.6-sol]
                   [--effort low|medium|xhigh|ultra]
                   [--sandbox read-only|workspace-write] [--prompt "<first turn>"]
"""
import argparse, json, os, subprocess, sys, threading, time

class Conn:
    def __init__(self, on_note):
        self.p = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self._id, self._resp, self._lock = 0, {}, threading.Lock()
        self._on_note = on_note
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n"); self.p.stdin.flush()

    def request(self, method, params=None):
        with self._lock:
            self._id += 1; rid = self._id
        self._send({"id": rid, "method": method, "params": params or {}})
        while rid not in self._resp:
            if self.p.poll() is not None:
                raise RuntimeError("app-server exited")
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

class Session:
    def __init__(self, a):
        self.a = a
        self.dir = a.dir
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        self.log_path = os.path.join(self.dir, "progress.log")
        open(self.ctl, "a").close()
        self.logf = open(self.log_path, "a", buffering=1)
        self.turn_id = None
        self.buf = []
        self.turns_done = 0
        self.conn = Conn(self._on_note)

    def log(self, s):
        self.logf.write(s + "\n")

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
                self.log(f"\n[item {it.get('type','?')}] {str(desc)[:200]}")
        elif method == "turn/completed":
            final = "".join(self.buf).strip()
            with open(os.path.join(self.dir, "result.txt"), "w") as f:
                f.write(final)
            self.turns_done += 1
            self.turn_id = None
            self.log(f"\n[turn complete -> result.txt, {len(final)} chars]")
        elif req_id is not None:
            self.conn._send({"id": req_id, "result": {}})

    def status(self):
        with open(os.path.join(self.dir, "status.json"), "w") as f:
            json.dump({"thread": self.thread, "active_turn": self.turn_id,
                       "turns_completed": self.turns_done}, f)

    def start_turn(self, prompt):
        if self.turn_id:
            self.log("[reject turn: one already active]"); return
        sb = ({"type": "readOnly", "networkAccess": False}
              if self.a.sandbox == "read-only"
              else {"type": "workspaceWrite"})
        r = self.conn.request("turn/start", {
            "threadId": self.thread, "input": [{"type": "text", "text": prompt}],
            "model": self.a.model, "effort": self.a.effort, "sandboxPolicy": sb})
        self.turn_id = (r.get("turn") or {}).get("id")

    def steer(self, text):
        if not self.turn_id:
            self.log("[reject steer: no active turn]"); return
        try:
            self.conn.request("turn/steer", {
                "threadId": self.thread, "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": text}]})
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

    def run(self):
        self.conn.request("initialize", {"clientInfo": {"name": "codex-subagent-session", "version": "0.1.0"}})
        self.conn.notify("initialized")
        th = self.conn.request("thread/start", {"cwd": self.a.cwd, "approvalPolicy": "never"})
        self.thread = (th.get("thread") or {}).get("id")
        self.log(f"[READY thread={self.thread} model={th.get('model')} sandbox={self.a.sandbox}]")
        self.status()
        if self.a.prompt:
            self.start_turn(self.a.prompt)
        # poll the control file for appended commands
        off = os.path.getsize(self.ctl)
        while True:
            time.sleep(0.15)
            try:
                sz = os.path.getsize(self.ctl)
            except OSError:
                break
            if sz <= off:
                continue
            with open(self.ctl) as f:
                f.seek(off); new = f.read(); off = f.tell()
            for raw in new.splitlines():
                cmd = raw.strip()
                if not cmd:
                    continue
                low = cmd.lower()
                if low.startswith("turn:"):
                    self.start_turn(cmd[5:].strip())
                elif low.startswith("steer:"):
                    self.steer(cmd[6:].strip())
                elif low == "interrupt":
                    self.interrupt()
                elif low == "status":
                    self.status()
                elif low == "quit":
                    self.log("[quit]")
                    try: self.conn.p.terminate()
                    except Exception: pass
                    return

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--effort", default="xhigh", choices=["low", "medium", "xhigh", "ultra"])
    ap.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write"])
    ap.add_argument("--prompt")
    Session(ap.parse_args()).run()

if __name__ == "__main__":
    main()
