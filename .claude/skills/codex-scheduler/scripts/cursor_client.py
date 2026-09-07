#!/usr/bin/env python3
"""Cursor CLI (`cursor-agent`) driver for the codex-scheduler daemon.

Mirrors appserver_client.JobSession's interface exactly -- same constructor callbacks, same
file control plane (<jobdir>/{control,progress.log,result.txt,status.json}), same on_done
contract -- so scheduler_daemon.py can drive a Cursor job and a Codex job through identical code
paths and only picks the class based on the job's `engine`.

Protocol is different, though: where Codex speaks JSON-RPC over a long-lived `codex app-server`,
`cursor-agent -p --output-format stream-json` is a ONE-SHOT subprocess that streams newline
-delimited JSON events and exits. Consequences worth knowing:
  * there is no mid-turn steering channel, so `steer` is rejected rather than silently dropped;
  * reasoning text IS visible here (`thinking/delta`), unlike Codex which emits only a marker;
  * token usage arrives once, in the final `result` event, rather than incrementally.
"""
import json
import os
import re
import subprocess
import threading
import time

import db
from appserver_client import build_preamble, SENTINEL_RE

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler_cli.py")

# Reasoning level is baked into the Cursor model id -- there is no separate effort parameter --
# so the scheduler's --effort vocabulary maps onto the id's suffix. Model families differ in which
# levels they offer (Grok 4.6 has low/medium/high/xhigh; Luna and Sol add none/max; Composer has
# NO levels at all), so resolution is validated against the CLI's own model list rather than
# assuming a family's shape.
# Each effort maps to a chain of acceptable levels, best first. Families differ: Luna and Sol
# offer `max`, Grok 4.6 stops at `xhigh`. Trying the chain in order means --effort max/ultra gets
# the strongest level a family actually has instead of resolving to a model id that does not
# exist. `ultra` has no separate Cursor tier above `max` (Codex's own `ultra` reasoning level is
# "max reasoning plus automatic task delegation" -- a Codex-only behavior, not a bigger id suffix),
# so it resolves to the same chain as `max`.
EFFORT_TO_LEVELS = {
    "low": ("low",),
    "medium": ("medium",),
    "high": ("high",),
    "xhigh": ("xhigh", "high"),
    "max": ("max", "xhigh", "high"),
    "ultra": ("max", "xhigh", "high"),
}
DEFAULT_MODEL_BASE = "composer-2.5"
_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")

_models_cache = {"at": 0.0, "ids": None}
_MODELS_TTL = 600

# `cursor-agent --list-models` writes to a TTY-agnostic pretty printer: every id is wrapped in SGR
# colour codes and the id/description separator is emitted as "<esc>[39m <esc>[2m- ", so the plain
# " - " split never matches and the escapes end up inside the parsed id. Strip them first.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Lines that are chrome rather than a model id.
_MODELS_NOISE_PREFIXES = ("available models", "tip:", "usage:", "note:")


def available_models(force=False):
    """Model ids `cursor-agent --list-models` reports, or None if it cannot be asked.

    Returning None (rather than an empty set) matters: callers must distinguish "this model is
    not available" from "we could not find out", and never reject a job because the CLI was
    briefly unreachable."""
    now = time.time()
    if not force and _models_cache["ids"] is not None and now - _models_cache["at"] < _MODELS_TTL:
        return _models_cache["ids"]
    try:
        out = subprocess.run(["cursor-agent", "--list-models"], capture_output=True,
                             text=True, timeout=30)
    except Exception:  # noqa
        return None
    if out.returncode != 0:
        return None
    ids = parse_models(out.stdout or "")
    if not ids:
        return None      # unparseable output is "could not find out", not "nothing available"
    _models_cache.update({"at": now, "ids": ids})
    return ids


def parse_models(text):
    """Model ids out of `cursor-agent --list-models` output (ANSI-decorated, one id per line)."""
    ids = set()
    for line in text.splitlines():
        line = _ANSI_RE.sub("", line).strip()
        if not line or line.lower().startswith(_MODELS_NOISE_PREFIXES):
            continue
        ident = line.split(" - ", 1)[0].strip() if " - " in line else line
        # A real id is a single bare token; anything with whitespace is prose we do not want.
        if ident and " " not in ident:
            ids.add(ident)
    return ids


def resolve_model(model, effort, fast_mode=False, available=None):
    """Turn (model, effort, fast) into a concrete Cursor model id.

    `auto` and an id that already carries a level pass through verbatim -- that is the escape
    hatch for levels --effort cannot express, such as Grok's `high`. Otherwise the effort-derived
    level is appended, EXCEPT where the model family has no levels (Composer), which is decided by
    checking the real model list instead of hardcoding family names.
    """
    model = (model or DEFAULT_MODEL_BASE).strip()
    if model == "auto":
        return model

    def with_fast(m):
        return m if (not fast_mode or m.endswith("-fast")) else m + "-fast"

    if any(model.endswith("-" + lv) or model.endswith("-" + lv + "-fast") for lv in _LEVELS):
        return with_fast(model)

    chain = EFFORT_TO_LEVELS.get(effort or "medium", ("medium",))
    candidates = [with_fast(f"{model}-{lv}") for lv in chain]
    bare = with_fast(model)
    if available is None:
        available = available_models()
    if available is None:
        return candidates[0]      # cannot check; the preferred level is the common case
    for c in candidates:
        if c in available:
            return c
    if bare in available:
        return bare               # a family without reasoning levels, e.g. composer-2.5
    return candidates[0]          # let submit-time validation report it properly


def describe_tool(ev):
    """One-line description of a tool_call event for the progress log.

    The payload nests the interesting part under tool_call.<kind>.args, so pull the command or
    path out of there; `subtype` alone would only ever say "started"/"completed"."""
    tc = ev.get("tool_call") or {}
    for kind, body in tc.items():
        args = (body or {}).get("args") or {}
        for key in ("command", "path", "filePath", "target_file", "pattern", "query", "url"):
            val = args.get(key)
            if val:
                return f"{kind} {val}"
        return kind
    return ev.get("subtype") or "tool"


def sandbox_args(sandbox):
    """Map the scheduler's sandbox vocabulary onto cursor-agent flags.

    `--force` is what lets tool calls run without an interactive approval prompt; without it a
    headless run would stall waiting for a human. read-only therefore uses plan mode, which is
    read-only by construction, instead of relying on approvals nobody is there to give.
    """
    if sandbox == "read-only":
        return ["--mode", "plan", "--sandbox", "enabled"]
    if sandbox == "danger-full-access":
        return ["--force", "--sandbox", "disabled"]
    return ["--force", "--sandbox", "enabled"]  # workspace-write


class CursorSession:
    """Drives one Cursor job. Interface-compatible with appserver_client.JobSession."""

    def __init__(self, job, on_done, on_tokens=None, on_ask_answer=None,
                 on_checkpoint=None, on_message=None, on_goal=None):
        self.job = job
        self.on_done = on_done
        self.on_tokens = on_tokens
        self.on_ask_answer = on_ask_answer
        self.on_checkpoint = on_checkpoint
        self.on_message = on_message
        # cursor-agent has no thread-level goal protocol; the kwarg is accepted so both drivers
        # share one call signature, and a --goal on a cursor job is rejected at submit time.
        self.on_goal = on_goal
        self.cwd = db.job_cwd(job)
        self.dir = db.job_dir(job["id"], job["slug"])
        os.makedirs(self.dir, exist_ok=True)
        self.ctl = os.path.join(self.dir, "control")
        open(self.ctl, "a").close()
        self.logf = open(os.path.join(self.dir, "progress.log"), "a", buffering=1)
        self.thread = None          # cursor session_id
        self.turn_id = None
        self.messages = []
        self._pending_ask = None
        self._ask_capture = None
        self._ctl_offset = 0
        self._settled = False
        self._last_error = None
        self.model = resolve_model(job.get("model"), job.get("effort"), job.get("fast_mode"))
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
        """Same sentinel contract as Codex: [[NOTE]]/[[CHECKPOINT]] lines are dispatched and
        stripped, and the remainder becomes an agent message. Keeping this identical means a
        prompt written for one engine behaves the same on the other."""
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
        t, sub = ev.get("type"), ev.get("subtype")
        if t == "system" and sub == "init":
            self.thread = ev.get("session_id")
            self.turn_id = self.thread
            self.log(f"[READY session={self.thread} model={ev.get('model')!r} "
                     f"requested={self.model} sandbox={self.job.get('sandbox')}]")
            self._write_status()
        elif t == "thinking" and sub == "delta":
            # Cursor exposes reasoning text; Codex does not. Prefix it so the dashboard's
            # filter and a human reading the log can tell it from an agent message.
            self.logf.write(ev.get("text") or "")
        elif t == "thinking" and sub == "completed":
            self.log("\n[item reasoning] reasoning")
        elif t == "assistant":
            parts = ((ev.get("message") or {}).get("content")) or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if text.strip():
                self.log(f"\n[item agentMessage] agentMessage")
                self.logf.write(text)
                self._handle_text(text)
        elif t == "tool_call" or (t or "").startswith("tool"):
            if sub == "started":
                self.log(f"\n[item commandExecution] {describe_tool(ev)[:200]}")
            elif sub == "completed":
                self.log(f"\n[item commandExecution] completed")
        elif t in ("connection", "retry"):
            self.log(f"\n[{t}/{sub} attempt={ev.get('attempt')}]")
        elif t == "result":
            usage = ev.get("usage") or {}
            if usage and self.on_tokens:
                # Normalised into the same shape appserver_client emits, so db.record_tokens and
                # every reader downstream stay engine-agnostic.
                try:
                    self.on_tokens(self.job, {
                        "total": {
                            "inputTokens": usage.get("inputTokens"),
                            "cachedInputTokens": usage.get("cacheReadTokens"),
                            "outputTokens": usage.get("outputTokens"),
                            "reasoningOutputTokens": None,
                            "totalTokens": (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0),
                        },
                        "modelContextWindow": None,
                    })
                except Exception as e:  # noqa
                    self.log(f"[on_tokens error: {e}]")
            # Prefer the messages we processed over cursor's raw `result` field. Two reasons:
            # the raw field still contains [[NOTE]]/[[CHECKPOINT]] lines that were already
            # dispatched as messages (they would otherwise appear twice -- once delivered, once
            # embedded in the result), and it concatenates every assistant message, whereas the
            # documented contract for both engines is that the result is the FINAL message.
            if self.messages:
                final = self.messages[-1]
            else:
                final = SENTINEL_RE.sub("", (ev.get("result") or "")).strip()
            if ev.get("is_error") or sub == "error":
                self.log(f"\n[turn failed: {final or sub}]")
                self._settle("failed", final or f"cursor-agent reported {sub}")
                return
            try:
                with open(os.path.join(self.dir, "result.txt"), "w") as f:
                    f.write(final)
            except OSError:
                pass
            self.log(f"\n[turn complete -> result.txt, {len(final)} chars "
                     f"(duration {ev.get('duration_ms')}ms)]")
            self._settle("done", final)

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
        """Blocking: runs cursor-agent to completion. Call from its own thread."""
        argv = ["cursor-agent", "-p", "--output-format", "stream-json",
                "--model", self.model, "--workspace", self.cwd]
        argv += sandbox_args(self.job.get("sandbox", "workspace-write"))
        argv += ["--trust", build_preamble(self.job, _CLI_PATH) + prompt]
        try:
            self.p = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            self.pid = self.p.pid
        except Exception as e:  # noqa
            self.log(f"[start error: {e}]")
            self._settle("failed", f"failed to start cursor-agent: {e}")
            return
        self.log(f"[cursor-agent pid={self.pid} model={self.model}]")
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                # cursor-agent prints plain-text errors (e.g. "RetriableError: [resource_exhausted]")
                # interleaved with the JSON stream. Keep the last one: if the process dies without
                # a result event it is the only explanation we will have.
                self._last_error = line[:400]
                self.log(f"\n[stderr] {line[:400]}")
                continue
            try:
                self._on_event(ev)
            except Exception as e:  # noqa
                self.log(f"[event handler error: {e}]")
        rc = self.p.wait()
        if not self._settled:
            msg = self._last_error or f"cursor-agent exited {rc} without a result event"
            self.log(f"\n[turn failed: {msg}]")
            self._settle("failed", msg)

    def steer(self, text):
        # cursor-agent -p is one-shot: the prompt is fixed at launch and there is no channel to
        # inject into a turn in flight. Say so rather than accepting it into a void.
        self.log(f"\n[steer rejected: cursor engine does not support mid-turn steering]")

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
                self.log("\n[ask rejected: cursor engine cannot be questioned mid-turn]")
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
