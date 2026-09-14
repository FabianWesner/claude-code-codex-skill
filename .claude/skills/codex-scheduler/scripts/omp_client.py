#!/usr/bin/env python3
"""OMP CLI (`omp`) driver for the codex-scheduler daemon.

Mirrors cursor_client.CursorSession / opencode_client.OpencodeSession -- and therefore
appserver_client.JobSession -- exactly: same constructor callbacks, same file control plane
(<jobdir>/{control,progress.log,result.txt,status.json}), same on_done contract, so
scheduler_daemon.py picks the class off the job's `engine` and every other code path stays
engine-agnostic.

Scope: this engine exists for exactly one model, DeepSeek v4.1 Flash at the `high` thinking
level (opencode-go/deepseek-v4.1-flash --thinking high), because that is the only combination
that has been wired up and validated end to end. `resolve_model`/`resolve_thinking` enforce that
rather than accepting arbitrary --model/--effort values, so a typo'd model or an unsupported
effort fails at submit time instead of silently running something else.

Protocol: `omp -p --mode json --model <id> --thinking <level> "<prompt>"` is a ONE-SHOT subprocess
that streams newline-delimited JSON events on stdout and exits, same shape as the cursor/opencode
drivers -- no mid-turn steering channel, so `steer`/`ask` are rejected rather than dropped into a
void. It DOES have a resumable session (verified 2026-09-14 against omp v18.1.21): every run
prints a `session` event with a `id` (uuid), and `-r <id>` continues it with context and prompt
cache intact, which is what `submit --resume-from` uses (stored on the job like an OpenCode
session id).

There is NO sandbox flag: tool calls execute against the real filesystem, gated only by
`--auto-approve` (needed here since nothing is present to answer an interactive approval prompt).
`danger-full-access` is therefore the only honest label for an OMP job, same as OpenCode; the
scheduler's other sandbox values are accepted (so a brief written for another engine still
submits) but map to nothing and are noted in the log.

Event shapes this driver relies on (verified 2026-09-14 against omp v18.1.21):
  * "session"                             -> id is the resumable session id.
  * "message_update".assistantMessageEvent.type:
      "text_start"/"text_delta"/"text_end"         -> .delta / .content is assistant text.
      "thinking_start"/"thinking_delta"/"thinking_end" -> reasoning text (visible, unlike Codex).
      "toolcall_start"/"toolcall_delta"/"toolcall_end" -> streamed tool-call args.
  * "tool_execution_start"/"tool_execution_update"/"tool_execution_end" -> toolName, args, result.
  * "turn_end" -> message.usage is the token/cost report for that turn (cumulative per turn, not
    per run), message.content holds the turn's final content.
  * "agent_end" -> isTerminal: true marks the whole run finished; its `messages` is the full
    transcript, so the LAST assistant text message is the run's final answer.
  * Failures don't come as a JSON event: `omp` prints a plain-text error to stdout and exits
    nonzero (e.g. an unknown model id), so a non-JSON line is kept as the failure explanation.
"""
import json
import os
import subprocess
import time

import db
from appserver_client import build_preamble, SENTINEL_RE

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

# The only model/effort combination this engine supports. `--effort high` (the scheduler's
# vocabulary) is the only accepted value; everything else is rejected at submit time rather than
# silently coerced, since "high effort only" is the deployed scope, not a default.
MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_MODEL = MODEL
THINKING_LEVEL = "high"

# `omp` is installed under the user's home rather than a system prefix, and the scheduler daemon
# does not inherit an interactive shell's PATH.
_BIN_CANDIDATES = (
    os.environ.get("OMP_BIN"),
    os.path.expanduser("~/.local/bin/omp"),
    "/usr/local/bin/omp",
    "/opt/homebrew/bin/omp",
)


def omp_bin():
    for cand in _BIN_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "omp"  # let PATH (and a clear ENOENT) decide


def resolve_model(model):
    """The scheduler's --model is optional for this engine; if given it must match the one
    supported id (accepting a bare 'deepseek-v4.1-flash' too, since fuzzy-matching model ids is
    the omp CLI's own convention). Anything else is a submit-time error, not a silent override."""
    model = (model or MODEL).strip()
    if model in (MODEL, "deepseek-v4.1-flash"):
        return MODEL
    return None  # caller turns this into a validation error


def resolve_effort(effort):
    """Only 'high' is supported; None/'high' resolve, anything else is a submit-time error."""
    effort = (effort or "high").strip().lower()
    return THINKING_LEVEL if effort == "high" else None


def describe_tool(toolcall_or_exec):
    """One line for a tool_execution_start/update event: the tool plus a recognisable argument."""
    tool = toolcall_or_exec.get("toolName") or "tool"
    args = toolcall_or_exec.get("args") or {}
    for key in ("command", "filePath", "path", "pattern", "query", "url", "description"):
        val = args.get(key)
        if val:
            return f"{tool} {str(val)}"
    return tool


class OmpSession:
    """Drives one OMP job. Interface-compatible with appserver_client.JobSession."""

    def __init__(self, job, on_done, on_tokens=None, on_ask_answer=None,
                 on_checkpoint=None, on_message=None, on_goal=None):
        self.job = job
        self.on_done = on_done
        self.on_tokens = on_tokens
        self.on_ask_answer = on_ask_answer
        self.on_checkpoint = on_checkpoint
        self.on_message = on_message
        # omp has no thread-level goal protocol; the kwarg is accepted so all drivers share one
        # call signature, and a --goal on an omp job is rejected at submit time.
        self.on_goal = on_goal
        self.cwd = db.job_cwd(job)
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None          # omp session id (uuid)
        self.turn_id = None
        self.messages = []
        self._ask_capture = None
        self._ctl_offset = 0
        self._settled = False
        self._last_error = None
        self._last_usage = None
        self.model = resolve_model(job.get("model")) or MODEL
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
        if t == "session":
            self.thread = ev.get("id")
            self.turn_id = self.thread
            self.log(f"[READY session={self.thread} model={self.model} thinking={THINKING_LEVEL} "
                     f"cwd={self.cwd} sandbox={self.job.get('sandbox')}]")
            self._write_status()
        elif t == "message_update":
            aev = ev.get("assistantMessageEvent") or {}
            et = aev.get("type")
            if et == "text_delta":
                self.logf.write(aev.get("delta") or "")
            elif et == "text_end":
                self.log("\n[item agentMessage] agentMessage")
                self._handle_text(aev.get("content") or "")
            elif et == "thinking_end":
                self.log("\n[item reasoning] reasoning")
        elif t == "tool_execution_start":
            self.log(f"\n[item commandExecution] {describe_tool(ev)[:200]}")
        elif t == "tool_execution_end":
            self.log(f"\n[item commandExecution] {describe_tool(ev)[:200]} "
                     f"[{'error' if ev.get('isError') else 'completed'}]")
        elif t == "turn_end":
            usage = (ev.get("message") or {}).get("usage")
            if usage:
                self._last_usage = usage
        elif t == "agent_end":
            self.log(f"\n[agent_end isTerminal={ev.get('isTerminal')}]")

    def _report_tokens(self):
        u = self._last_usage
        if not u or not self.on_tokens:
            return
        try:
            self.on_tokens(self.job, {
                "total": {
                    "inputTokens": u.get("input"),
                    "cachedInputTokens": u.get("cacheRead"),
                    "outputTokens": u.get("output"),
                    "reasoningOutputTokens": u.get("reasoningTokens"),
                    "totalTokens": u.get("totalTokens"),
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
        """Blocking: runs `omp -p` to completion. Call from its own thread."""
        resume_id = (self.job.get("resume_thread_id") or "").strip()
        argv = [omp_bin(), "-p", "--mode", "json", "--model", self.model,
                "--thinking", THINKING_LEVEL, "--auto-approve", "--cwd", self.cwd]
        if resume_id:
            # Continuing a previous lane's session: context and prompt cache carry over. No
            # silent fallback to a fresh session -- if the id is unknown omp errors and the job
            # fails, which is what "resume" asking for should mean.
            argv += ["-r", resume_id]
            self.thread = resume_id
        # Session saving is on by default (not --no-session) so the "session" event's id is
        # always a real, later-resumable id -- that id becomes resume_thread_id for --resume-from.
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
            self._settle("failed", f"failed to start omp: {e}")
            return
        self.log(f"[omp pid={self.pid} model={self.model} thinking={THINKING_LEVEL}"
                 f"{' resumed=' + resume_id if resume_id else ''}]")
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                # omp prints plain-text errors (e.g. "Model ... not found") interleaved with, or
                # instead of, the JSON stream. Keep the last one: if the process dies without a
                # usable turn it is the only explanation we will have.
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
            msg = self._last_error or f"omp exited {rc} without a final message"
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
        # `omp -p` is one-shot: the prompt is fixed at launch and there is no channel into a turn
        # in flight. Say so rather than accepting it into a void.
        self.log("\n[steer rejected: omp engine does not support mid-turn steering]")

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
                self.log("\n[ask rejected: omp engine cannot be questioned mid-turn]")
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
