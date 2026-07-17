#!/usr/bin/env python3
"""Minimal stdio JSON-RPC client for `codex app-server` (GPT-5.6 Sol, ChatGPT-sub auth).

Orchestration-side second-opinion tool with MID-FLIGHT STEERING (turn/steer) that a
one-shot `codex exec` cannot do. Uses the same ~/.codex/auth.json as the CLI (ChatGPT
subscription, no API key).

Usage:
  codex_appserver.py "<prompt>" [--cwd DIR] [--model gpt-5.6-sol]
                     [--effort low|medium|xhigh|ultra]
                     [--steer "<text>" --steer-after SECS] [-o OUTFILE]
Prints the final agent message to stdout (and OUTFILE). Streaming + logs go to stderr.
"""
import argparse, json, os, subprocess, sys, threading, time

class AppServer:
    def __init__(self, cwd):
        self.cwd = cwd
        self.p = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._id = 0
        self._resp = {}
        self._lock = threading.Lock()
        self.thread_id = None
        self.turn_id = None
        self.deltas = []
        self.turn_done = threading.Event()
        self.turn_error = None
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._errpump, daemon=True).start()

    def _errpump(self):
        for line in self.p.stderr:
            sys.stderr.write("[srv] " + line)

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None):
        with self._lock:
            self._id += 1
            rid = self._id
        self._send({"id": rid, "method": method, "params": params or {}})
        # block until the reader files a response for this id
        while rid not in self._resp:
            if self.p.poll() is not None:
                raise RuntimeError("app-server exited early")
            time.sleep(0.02)
        r = self._resp.pop(rid)
        if "error" in r:
            raise RuntimeError(f"{method} error: {r['error']}")
        return r.get("result", {})

    def notify(self, method, params=None):
        self._send({"method": method, "params": params or {}})

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self._resp[msg["id"]] = msg
            elif "method" in msg:
                self._on_notification(msg.get("method"), msg.get("params") or {}, msg.get("id"))

    def _on_notification(self, method, params, req_id):
        if method == "turn/started":
            self.turn_id = (params.get("turn") or {}).get("id")
        elif method and method.endswith("/delta") and "delta" in params:
            d = params["delta"]
            self.deltas.append(d)
            sys.stderr.write(d)
            sys.stderr.flush()
        elif method == "item/completed":
            item = params.get("item") or {}
            if not self.deltas and isinstance(item.get("text"), str):
                self.deltas.append(item["text"])
        elif method == "turn/completed":
            self.turn_done.set()
        elif method == "turn/failed" or (params.get("error") and method and method.startswith("turn/")):
            self.turn_error = params.get("error")
            self.turn_done.set()
        elif req_id is not None:
            # server-initiated request (approval etc.) — we run approvalPolicy=never, so
            # this should not happen; respond benignly so nothing hangs.
            self._send({"id": req_id, "result": {}})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--effort", default="xhigh", choices=["low", "medium", "xhigh", "ultra"])
    ap.add_argument("--steer")
    ap.add_argument("--steer-after", type=float, default=2.0)
    ap.add_argument("-o", "--output")
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()

    srv = AppServer(a.cwd)
    init = srv.request("initialize", {"clientInfo": {"name": "codex-subagent-appserver", "version": "0.1.0"}})
    srv.notify("initialized")
    th = srv.request("thread/start", {"cwd": a.cwd, "approvalPolicy": "never"})
    srv.thread_id = (th.get("thread") or {}).get("id")
    sys.stderr.write(f"[client] auth ok · model={th.get('model')} · thread={srv.thread_id}\n")

    turn = srv.request("turn/start", {
        "threadId": srv.thread_id,
        "input": [{"type": "text", "text": a.prompt}],
        "model": a.model, "effort": a.effort,
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    })
    srv.turn_id = (turn.get("turn") or {}).get("id")

    steered = None
    if a.steer:
        def do_steer():
            time.sleep(a.steer_after)
            if srv.turn_done.is_set():
                return
            try:
                srv.request("turn/steer", {
                    "threadId": srv.thread_id, "expectedTurnId": srv.turn_id,
                    "input": [{"type": "text", "text": a.steer}]})
                sys.stderr.write(f"\n[client] STEER accepted (turn {srv.turn_id})\n")
            except Exception as e:  # noqa
                nonlocal_hold["err"] = str(e)
                sys.stderr.write(f"\n[client] steer rejected: {e}\n")
        nonlocal_hold = {"err": None}
        threading.Thread(target=do_steer, daemon=True).start()

    ok = srv.turn_done.wait(timeout=a.timeout)
    final = "".join(srv.deltas).strip()
    try:
        srv.p.terminate()
    except Exception:
        pass
    if not ok:
        sys.stderr.write("\n[client] TIMEOUT\n"); sys.exit(3)
    if srv.turn_error:
        sys.stderr.write(f"\n[client] turn error: {srv.turn_error}\n"); sys.exit(4)
    sys.stderr.write("\n")
    print(final)
    if a.output:
        open(a.output, "w").write(final)

if __name__ == "__main__":
    main()
