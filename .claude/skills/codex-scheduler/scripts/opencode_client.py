#!/usr/bin/env python3
"""OpenCode CLI (`opencode run`) driver for the codex-scheduler daemon.

Mirrors cursor_client.CursorSession -- and therefore appserver_client.JobSession -- exactly: same
constructor callbacks, same file control plane (<jobdir>/{control,progress.log,result.txt,
status.json}), same on_done contract, so scheduler_daemon.py picks the class off the job's
`engine` and every other code path stays engine-agnostic.

Protocol: `opencode run --format json --model <provider/model> --dir <cwd> "<prompt>"` is a
ONE-SHOT subprocess that streams newline-delimited JSON events on stdout and exits. Like the
cursor driver that means no mid-turn steering channel, so `steer`/`ask` are rejected rather than
dropped into a void. Unlike cursor, OpenCode HAS a resumable session: every event carries a
top-level `sessionID`, and `--session <id>` continues it with its context and prompt cache, which
is what `submit --resume-from` uses (the id is stored on the job exactly like a Codex thread id).

There is NO sandbox flag on `opencode run`: tool calls execute against the real filesystem with
whatever permissions the user's OpenCode config grants. `danger-full-access` is therefore the only
honest label for an OpenCode job; the scheduler's other sandbox values are accepted (so a brief
written for another engine still submits) but map to nothing and are noted in the log.

Event shapes this driver relies on (verified 2026-09-07 against opencode 1.18.29):
  * every event: {"type": ..., "timestamp": ..., "sessionID": "ses_...", "part": {...}}
  * "text"        -> part.text is the assistant text for that part, and part.id identifies it.
    Text arrives whole per part in practice; the driver still diffs against the last text seen
    for that part id so a future incremental emitter cannot duplicate output.
  * "tool_use"    -> part.tool (e.g. "bash", "write"), part.state.status
    ("completed"/"error"), part.state.input (command/filePath/...), part.state.title.
  * "step_finish" -> part.reason ("stop" ends the turn, "tool-calls" continues it) and
    part.tokens {total, input, output, reasoning, cache:{read,write}} -- cumulative for the run,
    so the LAST one is the usage report.
  * "error"       -> error.name and error.data.message; the process then exits nonzero.
"""
import json
import os
import subprocess
import time

import db
from appserver_client import build_preamble, SENTINEL_RE

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

# The OpenCode Zen free tier. NOT `opencode-go/muse-spark-1.3-contributor`: that id bills against
# a paid plan and must never be the engine default.
DEFAULT_MODEL = "opencode/muse-spark-1.3-contributor-free"

# `opencode` is installed under the user's home rather than a system prefix, and the scheduler
# daemon does not inherit an interactive shell's PATH.
_BIN_CANDIDATES = (
    os.environ.get("OPENCODE_BIN"),
    os.path.expanduser("~/.opencode/bin/opencode"),
    "/usr/local/bin/opencode",
    "/opt/homebrew/bin/opencode",
)


def opencode_bin():
    for cand in _BIN_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "opencode"  # let PATH (and a clear ENOENT) decide


def describe_tool(part):
    """One line for a tool_use event: the tool plus the argument a human would recognise."""
    tool = part.get("tool") or "tool"
    state = part.get("state") or {}
    args = state.get("input") or {}
    for key in ("command", "filePath", "path", "pattern", "query", "url", "description"):
        val = args.get(key)
        if val:
            return f"{tool} {str(val)}"
    title = state.get("title")
    return f"{tool} {title}" if title else tool


class OpencodeSession:
    """Drives one OpenCode job. Interface-compatible with appserver_client.JobSession."""

    def __init__(self, job, on_done, on_tokens=None, on_ask_answer=None,
                 on_checkpoint=None, on_message=None, on_goal=None):
        self.job = job
        self.on_done = on_done
        self.on_tokens = on_tokens
        self.on_ask_answer = on_ask_answer
        self.on_checkpoint = on_checkpoint
        self.on_message = on_message
        # OpenCode has no thread-level goal protocol; the kwarg is accepted so all three drivers
        # share one call signature, and a --goal on an opencode job is rejected at submit time.
        self.on_goal = on_goal
        self.cwd = db.job_cwd(job)
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None          # opencode session id (ses_...)
        self.turn_id = None
        self.messages = []
        self._ask_capture = None
        self._ctl_offset = 0
        self._settled = False
        self._last_error = None
        self._last_tokens = None
        self._part_text = {}
        self.model = (job.get("model") or DEFAULT_MODEL).strip()
        self.p = None
        self.pid = None

    # ---------------------------------------------------------------- logging / status
    def log(self, s):
        try:
            self.logf.write(s + "\n")
        except ValueError:
            pass

    def _write_status(self):
        try:
            with open(os.path.join(self.dir, "status.json"), "w") as f:
                json.dump({"thread": self.thread, "active_turn": self.turn_id}, f)
        except OSError:
            pass

    # ---------------------------------------------------------------- message handling
    def _handle_text(self, text):
        """Same sentinel contract as the other engines: [[NOTE]]/[[CHECKPOINT]] blocks are
        dispatched and stripped, and the remainder becomes an agent message."""
        text = (text or "").strip()
        if not text:
            return
        if "[[" in text:
            for kind, body in SENTINEL_RE.findall(text):
                body = body.strip()
                if not body:
                    continue
                try:
                    if kind == "CHECKPOINT" and self.on_checkpoint:
                        self.on_checkpoint(self.job, body)
                        self.log(f"\n[checkpoint via marker, {len(body)} chars]")
                    elif kind == "NOTE" and self.on_message:
                        self.on_message(self.job, body)
                        self.log(f"\n[message via marker] {body[:160]}")
                except Exception as e:  # noqa
                    self.log(f"[sentinel dispatch error: {e}]")
            text = SENTINEL_RE.sub("", text).strip()
            if not text:
                return
        if self._ask_capture is not None:
            ask_id, self._ask_capture = self._ask_capture, None
            self.log(f"\n[ask #{ask_id} answered, {len(text)} chars]")
            if self.on_ask_answer:
                try:
                    self.on_ask_answer(self.job, ask_id, text)
                except Exception as e:  # noqa
                    self.log(f"[on_ask_answer error: {e}]")
            return
        self.messages.append(text)

    def _on_event(self, ev):
        t = ev.get("type")
        part = ev.get("part") or {}
        sid = ev.get("sessionID")
        if sid and not self.thread:
            self.thread = sid
            self.turn_id = sid
            self.log(f"[READY session={sid} model={self.model} cwd={self.cwd} "
                     f"sandbox={self.job.get('sandbox')} (opencode has no sandbox; "
                     f"effectively danger-full-access)]")
            self._write_status()

        if t == "text":
            pid = part.get("id") or ""
            full = part.get("text") or ""
            prev = self._part_text.get(pid, "")
            if full == prev:
                return
            delta = full[len(prev):] if full.startswith(prev) else full
            self._part_text[pid] = full
            self.log("\n[item agentMessage] agentMessage")
            self.logf.write(delta)
            self._handle_text(full if not prev else delta)
        elif t == "tool_use":
            status = ((part.get("state") or {}).get("status")) or ""
            if status in ("completed", "error"):
                self.log(f"\n[item commandExecution] {describe_tool(part)[:200]} [{status}]")
            elif status in ("running", "pending"):
                self.log(f"\n[item commandExecution] {describe_tool(part)[:200]}")
        elif t == "reasoning":
            self.log("\n[item reasoning] reasoning")
        elif t == "step_finish":
            tk = part.get("tokens") or {}
            if tk:
                self._last_tokens = tk
        elif t == "error":
            e = ev.get("error") or {}
            msg = ((e.get("data") or {}).get("message")) or e.get("name") or "opencode error"
            self._last_error = str(msg)[:400]
            self.log(f"\n[error] {self._last_error}")

    def _report_tokens(self):
        tk = self._last_tokens
        if not tk or not self.on_tokens:
            return
        cache = tk.get("cache") or {}
        try:
            self.on_tokens(self.job, {
                "total": {
                    "inputTokens": tk.get("input"),
                    "cachedInputTokens": cache.get("read"),
                    "outputTokens": tk.get("output"),
                    "reasoningOutputTokens": tk.get("reasoning"),
                    "totalTokens": tk.get("total"),
                },
                "modelContextWindow": None,
            })
        except Exception as e:  # noqa
            self.log(f"[on_tokens error: {e}]")

    def _settle(self, status, text):
        if self._settled:
            return
        self._settled = True
        try:
            self.on_done(self.job, status, text)
        except Exception as e:  # noqa
            self.log(f"[on_done callback error: {e}]")

    # ---------------------------------------------------------------- lifecycle
    def start_turn(self, prompt):
        """Blocking: runs `opencode run` to completion. Call from its own thread."""
        resume_id = (self.job.get("resume_thread_id") or "").strip()
        argv = [opencode_bin(), "run", "--format", "json", "--model", self.model,
                "--dir", self.cwd]
        if resume_id:
            # Continuing a previous lane's session: context and prompt cache carry over. No
            # silent fallback to a fresh session -- if the id is unknown OpenCode errors and the
            # job fails, which is what "resume" asking for should mean.
            argv += ["--session", resume_id]
            self.thread = resume_id
        else:
            argv += ["--title", f"codex-scheduler {self.job['slug']}"]
        # No sandbox flag exists; see the module docstring.
        argv += [build_preamble(self.job, _CLI_PATH) + prompt]
        try:
            self.p = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            self.pid = self.p.pid
        except Exception as e:  # noqa
            self.log(f"[start error: {e}]")
            self._settle("failed", f"failed to start opencode: {e}")
            return
        self.log(f"[opencode pid={self.pid} model={self.model}"
                 f"{' resumed=' + resume_id if resume_id else ''}]")
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                # opencode interleaves plain-text warnings and stack traces with the JSON stream.
                # Keep the last one: if the process dies without a usable turn it is the only
                # explanation we will have.
                self._last_error = line[:400]
                self.log(f"\n[stderr] {line[:400]}")
                continue
            try:
                self._on_event(ev)
            except Exception as e:  # noqa
                self.log(f"[event handler error: {e}]")
        rc = self.p.wait()
        self._report_tokens()
        if self._settled:
            return
        final = self.messages[-1] if self.messages else ""
        if rc != 0 or (not final and self._last_error):
            msg = self._last_error or f"opencode exited {rc} without a final message"
            self.log(f"\n[turn failed: {msg}]")
            self._settle("failed", msg)
            return
        try:
            with open(os.path.join(self.dir, "result.txt"), "w") as f:
                f.write(final)
        except OSError:
            pass
        self._write_status()
        self.log(f"\n[turn complete -> result.txt, {len(final)} chars]")
        self._settle("done", final)

    def steer(self, text):
        # `opencode run` is one-shot: the prompt is fixed at launch and there is no channel into
        # a turn in flight. Say so rather than accepting it into a void.
        self.log("\n[steer rejected: opencode engine does not support mid-turn steering]")

    def interrupt(self):
        self.quit()

    def quit(self):
        try:
            if self.p and self.p.poll() is None:
                self.p.terminate()
        except Exception:  # noqa
            pass
        try:
            self.logf.close()
        except Exception:  # noqa
            pass

    def poll_control(self):
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
            elif low.startswith("ask:"):
                self.log("\n[ask rejected: opencode engine cannot be questioned mid-turn]")
            elif low == "interrupt":
                self.log("\n[interrupt]")
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
        return self.p is not None and self.p.poll() is None
