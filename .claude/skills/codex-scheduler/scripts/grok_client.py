#!/usr/bin/env python3
"""Grok Build CLI (`grok`) driver for the codex-scheduler daemon.

Mirrors cursor_client.CursorSession / opencode_client.OpencodeSession / omp_client.OmpSession --
and therefore appserver_client.JobSession -- exactly: same constructor callbacks, same file
control plane (<jobdir>/{control,progress.log,result.txt,status.json}), same on_done contract, so
scheduler_daemon.py picks the class off the job's `engine` and every other code path stays
engine-agnostic.

Protocol: `grok -p "<prompt>" --output-format streaming-json --model <id> --reasoning-effort
<level> --sandbox <profile> --always-approve --cwd <cwd>` is a ONE-SHOT subprocess that streams
newline-delimited JSON events on stdout and exits, same shape as the cursor/opencode/omp drivers
-- no mid-turn steering channel, so `steer`/`ask` are rejected rather than dropped into a void. It
DOES have a resumable session (verified 2026-09-16 against grok 1.0.30): a completed run's "end"
event carries a `sessionId`, and `-r <id>` continues it with context and prompt cache intact,
which is what `submit --resume-from` uses (stored on the job like an OpenCode/OMP session id).
Unlike those two, the session id is only known once the run finishes -- there is no early
"session ready" event -- so `status.json`'s `thread` field stays null for the whole run unless
this is itself a resume (where the id is already known up front).

Unlike omp/opencode, `grok` HAS real sandbox profiles (`--sandbox <profile>`), so the scheduler's
three sandbox values map onto genuine enforcement rather than a recorded-but-ignored note:
`read-only` -> grok's built-in `read-only` profile, `workspace-write` -> `workspace`,
`danger-full-access` -> `none` (no sandbox at all). `--always-approve` is still required
separately (needed here since nothing is present to answer an interactive approval prompt).

Event shapes this driver relies on (verified 2026-09-16 against grok 1.0.30, `--output-format
streaming-json`):
  * "available_commands" -> noise (tool/command listing), ignored.
  * "thought"   -> data is a reasoning text delta (visible, like Cursor/OMP; unlike Codex).
  * "text"      -> data is an assistant text delta. There is no per-message boundary: preamble
    commentary and the final answer arrive as one continuous delta stream, exactly like the
    `text` field of `--output-format json`'s single JSON object -- so the whole accumulated
    buffer for the turn is treated as one message.
  * "tool_call" -> toolName, rawInput (command/target_file/path/... depending on the tool),
    kind ("execute"/"read"/...), toolCallId.
  * "tool_call_update" -> toolCallId, status (null while starting, then "in_progress",
    "completed", or "failed"/"error" on nonzero exit is NOT reported here -- a failing shell
    command still shows status "completed"; only the tool's own rawOutput.exit_code says whether
    the command itself failed).
  * "usage"     -> usage {input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    output_tokens, reasoning_tokens} -- per-turn, not cumulative; kept as a fallback.
  * "end"       -> isTerminal for the whole run; sessionId, stopReason, and the definitive
    cumulative `usage` (adds total_tokens on top of the per-turn shape above).
  * "error"     -> message is the failure explanation; the process then exits nonzero. A
    pre-flight failure (e.g. an unresolvable --sandbox profile) instead prints plain text to
    stderr with NO JSON event at all, so a non-JSON line is kept as the failure explanation too.
"""
import json
import os
import subprocess
import time

import db
from appserver_client import build_preamble, SENTINEL_RE

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

DEFAULT_MODEL = "grok-4.7"

# The CLI's own vocabulary (verified via a rejected --reasoning-effort bogus value: "use one of:
# xhigh, high, medium, low"). max/ultra reach for the ceiling the same way cursor_client's
# EFFORT_TO_LEVELS chain does for a family with no tier above xhigh.
EFFORT_MAP = {
    "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
    "max": "xhigh", "ultra": "xhigh",
}

# Real, enforced sandbox profiles (verified 2026-09-16): grok ships built-in `read-only` and
# `workspace` profiles plus `none` for no sandbox at all; `workspace-write`/`danger-full-access`
# are the scheduler's own vocabulary and are not grok profile names, so they are translated rather
# than passed through.
SANDBOX_MAP = {
    "read-only": "read-only",
    "workspace-write": "workspace",
    "danger-full-access": "none",
}

# `grok` is installed under the user's home rather than a system prefix, and the scheduler daemon
# does not inherit an interactive shell's PATH.
_BIN_CANDIDATES = (
    os.environ.get("GROK_BIN"),
    os.path.expanduser("~/.local/bin/grok"),
    "/usr/local/bin/grok",
    "/opt/homebrew/bin/grok",
)


def grok_bin():
    for cand in _BIN_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "grok"  # let PATH (and a clear ENOENT) decide


def available_models():
    """Model ids `grok models` reports, or None if it cannot be asked.

    Output (verified 2026-09-16 against grok 1.0.30) is banner prose ("You are logged in with
    grok.com.", "Default model: <id>", "Available models:") followed by one bulleted line per id
    ("  * grok-4.7 (default)" / "  - grok-4.6"); only the bulleted lines are real ids."""
    try:
        out = subprocess.run([grok_bin(), "models"], capture_output=True, text=True, timeout=30)
    except Exception:  # noqa
        return None
    if out.returncode != 0:
        return None
    ids = set()
    for line in (out.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "*-":
            continue
        ident = stripped[1:].strip().split(" ", 1)[0].strip()
        if ident:
            ids.add(ident)
    return ids or None


def resolve_effort(effort):
    """None/medium default; anything outside the CLI's own vocabulary is a submit-time error."""
    return EFFORT_MAP.get((effort or "medium").strip().lower())


def resolve_sandbox(sandbox):
    return SANDBOX_MAP.get(sandbox or "workspace-write", "workspace")


def describe_tool(toolcall_or_input):
    """One line for a tool_call event: the tool plus a recognisable argument."""
    tool = toolcall_or_input.get("toolName") or toolcall_or_input.get("title") or "tool"
    args = toolcall_or_input.get("rawInput") or {}
    for key in ("command", "target_file", "file_path", "path", "pattern", "query", "url",
                "description"):
        val = args.get(key)
        if val:
            return f"{tool} {str(val)}"
    return tool


class GrokSession:
    """Drives one Grok Build job. Interface-compatible with appserver_client.JobSession."""

    def __init__(self, job, on_done, on_tokens=None, on_ask_answer=None,
                 on_checkpoint=None, on_message=None, on_goal=None):
        self.job = job
        self.on_done = on_done
        self.on_tokens = on_tokens
        self.on_ask_answer = on_ask_answer
        self.on_checkpoint = on_checkpoint
        self.on_message = on_message
        # grok has no thread-level goal protocol; the kwarg is accepted so all drivers share one
        # call signature, and a --goal on a grok job is rejected at submit time.
        self.on_goal = on_goal
        self.cwd = db.job_cwd(job)
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None          # grok session id (uuid), known only once "end" arrives
        self.turn_id = None
        self.messages = []
        self._text_buf = []
        self._text_started = False
        self._in_thought = False
        self._tool_desc = {}
        self._ask_capture = None
        self._ctl_offset = 0
        self._settled = False
        self._last_error = None
        self._last_usage = None
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
        if t == "thought":
            if not self._in_thought:
                self._in_thought = True
                self.log("\n[item reasoning] reasoning")
            return
        self._in_thought = False
        if t == "text":
            delta = ev.get("data") or ""
            if not self._text_started:
                self._text_started = True
                self.log("\n[item agentMessage] agentMessage")
            self.logf.write(delta)
            self._text_buf.append(delta)
        elif t == "tool_call":
            desc = describe_tool(ev)[:200]
            cid = ev.get("toolCallId")
            if cid:
                self._tool_desc[cid] = desc
            self.log(f"\n[item commandExecution] {desc}")
        elif t == "tool_call_update":
            status = ev.get("status")
            if status in ("completed", "failed", "error"):
                cid = ev.get("toolCallId")
                desc = self._tool_desc.get(cid, "tool")
                self.log(f"\n[item commandExecution] {desc} [{status}]")
        elif t == "usage":
            usage = ev.get("usage")
            if usage:
                self._last_usage = usage
        elif t == "end":
            self.thread = ev.get("sessionId") or self.thread
            self.turn_id = self.thread
            usage = ev.get("usage")
            if usage:
                self._last_usage = usage
            self._write_status()
        elif t == "error":
            self._last_error = str(ev.get("message") or "grok error")[:400]
            self.log(f"\n[error] {self._last_error}")

    def _report_tokens(self):
        u = self._last_usage
        if not u or not self.on_tokens:
            return
        try:
            self.on_tokens(self.job, {
                "total": {
                    "inputTokens": u.get("input_tokens"),
                    "cachedInputTokens": u.get("cache_read_input_tokens"),
                    "outputTokens": u.get("output_tokens"),
                    "reasoningOutputTokens": u.get("reasoning_tokens"),
                    "totalTokens": u.get("total_tokens"),
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
        """Blocking: runs `grok -p` to completion. Call from its own thread."""
        resume_id = (self.job.get("resume_thread_id") or "").strip()
        sandbox = resolve_sandbox(self.job.get("sandbox"))
        effort = resolve_effort(self.job.get("effort")) or "medium"
        argv = [grok_bin(), "--output-format", "streaming-json", "--model", self.model,
                "--reasoning-effort", effort, "--sandbox", sandbox, "--always-approve",
                "--cwd", self.cwd]
        if resume_id:
            # Continuing a previous lane's session: context and prompt cache carry over. No
            # silent fallback to a fresh session -- if the id is unknown grok errors and the job
            # fails, which is what "resume" asking for should mean.
            argv += ["-r", resume_id]
            self.thread = resume_id
        argv += ["-p", build_preamble(self.job, _CLI_PATH) + prompt]
        try:
            self.p = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            self.pid = self.p.pid
        except Exception as e:  # noqa
            self.log(f"[start error: {e}]")
            self._settle("failed", f"failed to start grok: {e}")
            return
        self.log(f"[grok pid={self.pid} model={self.model} effort={effort} sandbox={sandbox}"
                 f"{' resumed=' + resume_id if resume_id else ''}]")
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                # A pre-flight failure (e.g. an unresolvable --sandbox profile) prints plain text
                # instead of a JSON event and the process exits nonzero with no further output.
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
        self._handle_text("".join(self._text_buf))
        final = self.messages[-1] if self.messages else ""
        if rc != 0 or (not final and self._last_error):
            msg = self._last_error or f"grok exited {rc} without a final message"
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
        # `grok -p` is one-shot: the prompt is fixed at launch and there is no channel into a turn
        # in flight. Say so rather than accepting it into a void.
        self.log("\n[steer rejected: grok engine does not support mid-turn steering]")

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
                self.log("\n[ask rejected: grok engine cannot be questioned mid-turn]")
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
