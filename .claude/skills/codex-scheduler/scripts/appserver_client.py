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
import re
import subprocess
import threading
import time

import db

# `workspace-write` confines writes to the job's cwd, which does NOT include the scheduler's own
# state dir -- so notify/checkpoint/answer (which write to the shared SQLite DB) were silently
# rejected by the sandbox. Granting exactly that one extra root fixes them without widening
# anything else. `read-only` has no writableRoots option at all in the protocol, which is why the
# sentinel channel below exists.
SANDBOX_MAP = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {"type": "workspaceWrite", "writableRoots": [db.STATE_DIR]},
    "danger-full-access": {"type": "dangerFullAccess"},
}

# Sandbox-proof back-channel: Codex writes these markers into its own message text, which reaches
# us over the protocol stream and needs no filesystem access whatsoever, so it works identically
# under read-only, workspace-write and danger-full-access.
# A marker's content runs to the next blank line, the next marker, or the end of the message --
# bounded rather than greedy, so ordinary prose written after a marker is not swallowed into it.
#
# The marker must START a line (leading whitespace allowed). Without that anchor, a job whose
# output merely *mentions* the syntax -- "write a line starting with the `[[NOTE]]` marker" --
# had that mention parsed as a real message and cut out of its own result. Observed live on a job
# documenting this scheduler. Anchoring matches what the preamble actually instructs Codex to do
# and leaves inline references alone.
SENTINEL_RE = re.compile(
    r"^[ \t]*\[\[(CHECKPOINT|NOTE)\]\](.*?)(?=\n\s*\n|^[ \t]*\[\[(?:CHECKPOINT|NOTE)\]\]|\Z)",
    re.S | re.M,
)

# Fixed nudge used to open each follow-up turn of a goal job. Deliberately short and constant:
# the goal itself carries the intent, so a per-turn instruction would only dilute it.
GOAL_NUDGE = "Continue toward the goal. Report what is verified so far."
# How long to wait after a turn completes before starting the next one ourselves. If the
# app-server drives the goal loop on its own it emits `turn/started` inside this window and we
# stand down, so the two mechanisms cannot double-start a turn.
GOAL_AUTOTURN_GRACE_SECONDS = 8.0

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

NOTIFY_PREAMBLE = (
    '[scheduler] Your job slug is "{slug}". To send the Claude session that queued this job a '
    "message WITHOUT ending your turn, write a line starting with the marker [[NOTE]] in any "
    "message:\n"
    "  [[NOTE]] found the cause: the retry loop never resets its backoff\n"
    "The scheduler strips that line out and delivers it immediately; it does not end your turn "
    "and it does not become part of your result. Use it for something worth surfacing before "
    "you're done (a question, a heads-up, an early finding) -- not for routine narration.\n"
    "(There is also a CLI form, `python3 {cli} notify {slug} \"<message>\"`, but it writes to the "
    "scheduler database and will be BLOCKED by the sandbox unless this job runs with "
    "danger-full-access. The [[NOTE]] marker always works -- prefer it.)\n\n"
)

CHECKPOINT_PREAMBLE = (
    "[scheduler] This job may run for a while. Every few minutes of work, and ALWAYS before "
    "starting anything long or risky, save your progress by writing a line starting with the "
    "marker [[CHECKPOINT]] in a message:\n"
    "  [[CHECKPOINT]] read db.py and daemon.py; state machine mapped; still to do: cli, client\n"
    "Everything after the marker up to the next blank line is stored as your checkpoint, "
    "replacing the previous one (so leave a blank line before resuming normal prose). Keep it "
    "short but self-contained -- findings, "
    "decisions, what is left. It does not end your turn and it is stripped from your result. If "
    "this job is stopped, times out, or exceeds its budget, the checkpoint is the ONLY thing that "
    "survives; without one the work is lost entirely.\n"
    "(The CLI form `python3 {cli} checkpoint {slug} \"<text>\"` also exists but is blocked by the "
    "sandbox unless this job runs with danger-full-access -- prefer the marker.)\n\n"
)

CITE_PREAMBLE = (
    "[scheduler] EVIDENCE REQUIRED. Every factual claim in your final answer must carry an anchor "
    "the reader can check independently, placed inline with the claim:\n"
    "  - about code: `path/to/file.py:LINE` (or LINE-RANGE) that you actually opened this turn\n"
    "  - about a web source: the URL plus the sentence you are relying on, quoted\n"
    "  - about behaviour you observed: the exact command you ran and the relevant output line\n"
    "Do not cite anything you did not actually read this turn -- an invented or approximate line "
    "number is worse than no citation. If you believe something but cannot anchor it, say so "
    "explicitly and mark it UNVERIFIED rather than dropping it or dressing it up.\n\n"
)

SCHEMA_PREAMBLE = (
    "[scheduler] STRUCTURED OUTPUT REQUIRED. Your FINAL message this turn must be exactly one "
    "JSON value matching this schema -- no prose before or after it, no markdown code fence:\n"
    "{schema}\n"
    "Intermediate messages are free-form; only the last one is parsed. If you cannot fill a "
    "field, use null rather than omitting it or inventing a value.\n\n"
)


GOAL_PREAMBLE = (
    "[scheduler] GOAL MODE. This thread carries a goal, and the goal is the finish line -- not "
    "the end of this one turn:\n"
    "  {objective}\n"
    "The prompt below carries the requirements and constraints; the goal above says when you are "
    "actually done. Keep working across turns until the goal is genuinely met and verified. When "
    "it is met, say so explicitly and stop. If you are blocked on something only a human can "
    "resolve, say BLOCKED and what you need, rather than inventing a way around it.\n\n"
)


def build_preamble(job, cli_path):
    """Assemble the per-job instruction block prepended to the user's prompt."""
    slug = job["slug"]
    parts = [NOTIFY_PREAMBLE.format(slug=slug, cli=cli_path)]
    if job.get("goal_objective"):
        parts.append(GOAL_PREAMBLE.format(objective=job["goal_objective"].strip()))
    if job.get("max_seconds") or job.get("effort") in ("xhigh", "ultra"):
        parts.append(CHECKPOINT_PREAMBLE.format(slug=slug, cli=cli_path))
    if job.get("cite_mode"):
        parts.append(CITE_PREAMBLE)
    if job.get("result_schema"):
        parts.append(SCHEMA_PREAMBLE.format(schema=job["result_schema"]))
    return "".join(parts)


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

    def __init__(self, job, on_done, on_tokens=None, on_ask_answer=None,
                 on_checkpoint=None, on_message=None, on_goal=None):
        self.job = job
        self.on_done = on_done
        self.on_tokens = on_tokens          # (job, tokenUsage dict) -> None
        self.on_ask_answer = on_ask_answer  # (job, ask_id, answer_text) -> None
        self.on_checkpoint = on_checkpoint  # (job, text) -> None
        self.on_message = on_message        # (job, text) -> None
        self.on_goal = on_goal              # (job, ThreadGoal dict, turns) -> None
        self.cwd = db.job_cwd(job)
        # goal mode state
        self.goal_objective = (job.get("goal_objective") or "").strip() or None
        self.goal_active = False        # a goal is attached to this thread
        self.goal_status = None
        self.goal_turns = 0             # turns WE have started for this goal
        self.goal_max_turns = int(job.get("goal_max_turns") or 12)
        self._goal_timer = None
        self._turn_seen_after_complete = False
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None
        self.turn_id = None
        self.buf = []           # deltas of the agent message currently being streamed
        self.messages = []      # every completed agent message this turn, in order
        self._cur_item = None   # type of the item currently streaming
        self._pending_ask = None  # ask id whose answer we are waiting to capture
        self._ask_capture = None  # ask id whose answer the current message IS
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

    def _flush_message(self):
        """Close off the agent message currently streaming into self.buf.

        Previously every delta of every agent message accumulated into ONE buffer that was joined
        with no separator at turn end, so a spoken preamble was welded onto the front of the real
        answer ("...approximate ones.# Report"). Messages are now kept separate."""
        text = "".join(self.buf).strip()
        self.buf = []
        text = self._extract_sentinels(text)
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
            return  # an answer to an out-of-band question is not part of the job's result
        self.messages.append(text)

    def _extract_sentinels(self, text):
        """Pull [[CHECKPOINT]] / [[NOTE]] blocks out of a message, dispatch them, and return the
        message with those blocks removed so they never leak into the job's result."""
        if "[[" not in text:
            return text
        found = SENTINEL_RE.findall(text)
        if not found:
            return text
        for kind, body in found:
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
        return SENTINEL_RE.sub("", text).strip()

    def _final_result(self):
        """The job's result is the LAST agent message, not every message concatenated. Falls back
        to joining (separated) if the last one is somehow empty."""
        if self.messages:
            return self.messages[-1]
        return ""

    def _on_note(self, method, params, req_id):
        if method == "turn/started":
            self._turn_seen_after_complete = True
            self.turn_id = (params.get("turn") or {}).get("id")
            self.buf, self.messages, self._cur_item = [], [], None
            self.log(f"[turn started id={self.turn_id}]")
        elif method and method.endswith("/delta") and "delta" in params:
            self.buf.append(params["delta"])
            self.logf.write(params["delta"])
        elif method == "thread/goal/updated":
            self._record_goal(params.get("goal") or {})
        elif method == "thread/goal/cleared":
            self.goal_active = False
            self.goal_status = None
            self.log("\n[goal cleared]")
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            tot = usage.get("total") or {}
            self.log(
                f"\n[tokens in={tot.get('inputTokens')} cached={tot.get('cachedInputTokens')} "
                f"out={tot.get('outputTokens')} reasoning={tot.get('reasoningOutputTokens')} "
                f"total={tot.get('totalTokens')}]"
            )
            if self.on_tokens:
                try:
                    self.on_tokens(self.job, usage)
                except Exception as e:  # noqa
                    self.log(f"[on_tokens error: {e}]")
        elif method == "item/started":
            it = params.get("item") or {}
            itype = it.get("type", "?")
            self._flush_message()  # the previous message (if any) ended when this item began
            self._cur_item = itype
            if itype == "agentMessage" and self._pending_ask is not None:
                # this message is Codex answering the question `ask` just steered in
                self._ask_capture, self._pending_ask = self._pending_ask, None
            desc = it.get("command") or it.get("text") or itype or ""
            if isinstance(desc, list):
                desc = " ".join(map(str, desc))
            if desc:
                self.log(f"\n[item {itype}] {str(desc)[:200]}")
        elif method == "turn/completed":
            self._flush_message()
            final = self._final_result()
            try:
                with open(os.path.join(self.dir, "result.txt"), "w") as f:
                    f.write(final)
            except OSError:
                pass
            self.turn_id = None
            self.log(
                f"\n[turn complete -> result.txt, {len(final)} chars "
                f"({len(self.messages)} agent message(s), last one is the result)]"
            )
            if self.goal_active:
                self._on_goal_turn_complete(final)
            else:
                self._settle("done", final)
        elif method == "turn/failed" or (params.get("error") and method and method.startswith("turn/")):
            self._flush_message()
            err = str(params.get("error") or "turn failed")
            self.turn_id = None
            self.log(f"\n[turn failed: {err}]")
            self._settle("failed", err)
        elif req_id is not None:
            try:
                self.conn._send({"id": req_id, "result": {}})
            except Exception:
                pass

    # ------------------------------------------------------------------ goal mode

    def _record_goal(self, goal):
        """Persist a ThreadGoal payload and log the transition."""
        if not goal:
            return
        prev = self.goal_status
        self.goal_active = True
        self.goal_status = goal.get("status")
        if goal.get("objective"):
            self.goal_objective = goal["objective"]
        if self.goal_status != prev:
            self.log(f"\n[goal {prev or '-'} -> {self.goal_status} "
                     f"tokens={goal.get('tokensUsed')}/{goal.get('tokenBudget')} "
                     f"time={goal.get('timeUsedSeconds')}s]")
        if self.on_goal:
            try:
                self.on_goal(self.job, goal, self.goal_turns)
            except Exception as e:  # noqa
                self.log(f"[on_goal error: {e}]")

    def _attach_goal(self):
        """Called once the thread exists. Sets the goal when this job supplies one; otherwise
        (a --resume-from that inherits a goal) reads the goal already on the thread so resuming
        never resets it."""
        set_it = self.goal_objective and self.job.get("goal_set_on_start", 1)
        try:
            if set_it:
                params = {"threadId": self.thread, "objective": self.goal_objective,
                          "status": "active"}
                if self.job.get("goal_budget"):
                    params["tokenBudget"] = int(self.job["goal_budget"])
                r = self.conn.request("thread/goal/set", params, timeout=60)
                self.log(f"[goal set budget={params.get('tokenBudget')}] {self.goal_objective[:160]}")
                self._record_goal(r.get("goal") or {"status": "active",
                                                    "objective": self.goal_objective})
            else:
                r = self.conn.request("thread/goal/get", {"threadId": self.thread}, timeout=60)
                goal = r.get("goal")
                if goal:
                    self.log(f"[goal inherited from resumed thread] {str(goal.get('objective'))[:160]}")
                    self._record_goal(goal)
                else:
                    self.log("[goal: none on the resumed thread; running as a plain job]")
        except Exception as e:  # noqa
            # A goal failure must not silently turn a goal job into a one-shot job.
            self.log(f"[goal setup failed: {e}]")
            raise

    def _on_goal_turn_complete(self, final):
        """A turn ended while a goal is attached. Terminal goal statuses settle the job; an
        `active` goal continues in the same thread, unless the app-server continues it itself."""
        st = self.goal_status
        if st == "complete":
            self.log("[goal complete -> job done]")
            self._settle("done", final)
            return
        if st == "blocked":
            self.log("[goal blocked -> job blocked]")
            self._settle("blocked", final)
            return
        if st in ("usageLimited", "budgetLimited"):
            note = ("goal stopped: the account usage limit was reached before the goal was met"
                    if st == "usageLimited" else
                    "goal stopped: the token budget was exhausted before the goal was met")
            self.log(f"[goal {st} -> job done with a note]")
            self._settle("done", f"[{note}]\n\n{final}")
            return
        if st == "paused":
            self.log("[goal paused -> job done (paused goals are not auto-continued)]")
            self._settle("done", f"[goal paused before completion]\n\n{final}")
            return
        # status is active (or unknown): keep going in the same thread.
        if self.goal_turns >= self.goal_max_turns:
            self.log(f"[goal still active after {self.goal_turns} turns "
                     f"(--goal-max-turns {self.goal_max_turns}) -> job done]")
            self._settle("done",
                         f"[goal not reached within --goal-max-turns {self.goal_max_turns}; "
                         f"goal status is still '{st}']\n\n{final}")
            return
        self._turn_seen_after_complete = False
        self._goal_timer = threading.Timer(GOAL_AUTOTURN_GRACE_SECONDS, self._continue_goal)
        self._goal_timer.daemon = True
        self._goal_timer.start()

    def _continue_goal(self):
        """Start the next goal turn ourselves -- unless the app-server already started one."""
        if self._settled:
            return
        if self._turn_seen_after_complete or self.turn_id:
            self.log("[goal: app-server started the next turn itself; not double-starting]")
            return
        self.log(f"[goal: starting turn {self.goal_turns + 1}/{self.goal_max_turns} with the nudge]")
        self._start_turn_rpc(GOAL_NUDGE)

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
            resume_id = (self.job.get("resume_thread_id") or "").strip()
            if resume_id:
                # `thread/resume` (verified against `codex app-server generate-json-schema`:
                # ThreadResumeParams requires `threadId`, optionally takes `cwd`/`approvalPolicy`,
                # and returns the same `{thread: {id}}` shape as thread/start). Resuming loads the
                # rollout from disk, so the model keeps the previous turn's context and its prompt
                # cache. A failure here is fatal for the job on purpose: silently falling back to a
                # fresh thread would look like success while losing exactly what was asked for.
                th = self.conn.request(
                    "thread/resume",
                    {"threadId": resume_id, "cwd": self.cwd, "approvalPolicy": "never"},
                    timeout=120,
                )
            else:
                th = self.conn.request(
                    "thread/start", {"cwd": self.cwd, "approvalPolicy": "never"}, timeout=60
                )
            self.thread = (th.get("thread") or {}).get("id") or (resume_id or None)
            self.log(
                f"[READY thread={self.thread} cwd={self.cwd} model={self.job.get('model')} "
                f"sandbox={self.job.get('sandbox')} fast={bool(self.job.get('fast_mode'))}"
                f"{' resumed=' + resume_id if resume_id else ''}]"
            )
            self._write_status()
            if self.goal_objective or not self.job.get("goal_set_on_start", 1):
                self._attach_goal()
            full_prompt = build_preamble(self.job, _CLI_PATH) + prompt
            self._start_turn_rpc(full_prompt)
        except Exception as e:  # noqa
            self.log(f"[start_turn error: {e}]")
            self._settle("failed", f"failed to start turn: {e}")

    def _start_turn_rpc(self, text):
        """One turn/start call. Used for the first turn and for every goal-mode continuation."""
        sb = SANDBOX_MAP.get(self.job.get("sandbox", "workspace-write"),
                             SANDBOX_MAP["workspace-write"])
        try:
            r = self.conn.request(
                "turn/start",
                {
                    "threadId": self.thread,
                    "input": [{"type": "text", "text": text}],
                    "model": self.job.get("model") or "gpt-5.6-sol",
                    "effort": self.job.get("effort") or "medium",
                    "sandboxPolicy": sb,
                },
                timeout=60,
            )
            self.turn_id = (r.get("turn") or {}).get("id")
            if self.goal_active:
                self.goal_turns += 1
                if self.on_goal:
                    try:
                        self.on_goal(self.job, None, self.goal_turns)
                    except Exception:  # noqa
                        pass
        except Exception as e:  # noqa
            self.log(f"[turn/start failed: {e}]")
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
            elif low.startswith("ask:"):
                # ask:<ask_id>:<question> -- steer the question in and capture the reply that
                # comes back as the next agent message (see _flush_message).
                rest = cmd[4:]
                aid, _, question = rest.partition(":")
                try:
                    self._pending_ask = int(aid)
                except ValueError:
                    self.log(f"\n[ask rejected: bad id {aid!r}]")
                    continue
                self.log(f"\n[ASK #{aid} -> {question[:80]}]")
                self.steer(
                    "[scheduler question -- answer in your very next message, then carry on "
                    "exactly where you left off; this is not a change of task] " + question
                )
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
