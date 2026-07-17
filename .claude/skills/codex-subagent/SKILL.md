---
name: codex-subagent
description: Run headless Codex (GPT-5.6 Sol, selectable reasoning) from Bash as an orchestration sub-agent for second opinions, heavy analysis, or bounded code tasks.
---

# Codex as an orchestration sub-agent

Use headless Codex (`codex exec`) to offload heavy work or get an independent second opinion when
a separate reasoning or coding agent would help. Runs on the ChatGPT login (no metered API key).
Model: `gpt-5.6-sol`. Reasoning effort is chosen per task by the calling agent — pick one of `low`,
`medium`, `xhigh`, `ultra` and scale it to the job (`low` for quick lookups, `xhigh`/`ultra` for
heavy analysis or hard code tasks). The recipes pass `-m` and `model_reasoning_effort` explicitly.

## HARD SCOPE RULE

Codex is a build-time developer tool ONLY: analysis, code, reviews, specific development work, and second opinions launched by the
supervising agent or operator. Any Codex-produced code lands as ordinary working-tree changes for review; sub-agents do not commit.

Do not route through host-specific user commands, plugins, or UI shortcuts. Use the raw `codex exec` Bash recipes below.

## Recipes (all verified; run via Bash)

`-s`/`--sandbox`: `read-only` (default) | `workspace-write` (edits the tree) | `danger-full-access`.
Final message goes to stdout and to `-o <file>`; streaming logs go to stderr; `--json` = JSONL events.
Some agent runtimes reset cwd, so ALWAYS pass `-C <abs repo>` and an ABSOLUTE `-o` path (scratchpad, not /tmp).
There is no native timeout flag: run in the background and poll, or wrap with `gtimeout <secs>`.

**ALWAYS append `</dev/null` to every `codex exec` launch.** ROOT CAUSE of ALL observed silent
startup hangs (3 confirmed, up to 5h46m dead): when stdin is an open pipe (any backgrounded/harness
launch), codex exec blocks forever on "Reading additional input from stdin..." BEFORE creating a
session — 0 CPU, no session log, no output file. With `</dev/null` the same command returns in
seconds (SMOKE-OK verified 2026-07-04).

(a) One-shot read-only second opinion / analysis:
```bash
codex exec -C <abs-repo> -m gpt-5.6-sol -c model_reasoning_effort="<low|medium|xhigh|ultra>" -s read-only \
  -o <abs-out>.txt "<question>"
```

(b) Bounded code-writing task (edits the working tree):
```bash
codex exec -C <abs-repo> -m gpt-5.6-sol -c model_reasoning_effort="<low|medium|xhigh|ultra>" -s workspace-write \
  -o <abs-out>.txt "<imperative task>"
```

(c) Follow-up that keeps Codex's session context (the run header prints `session id:`):
```bash
cd <abs-repo> && codex exec resume --last -o <abs-out>.txt "<next step>"
# or target a specific session: codex exec resume <SESSION_ID> -o <abs-out>.txt "<next step>"
```
`resume` accepts `-m`/`-o`/`--json` but NOT `-s/--sandbox` or `-C`. Sandbox is inherited from the
resumed session; workdir is the CURRENT cwd (not the original session's dir), so `cd` into the target
repo first — verified: a resume from the wrong cwd wrote its file there instead of the intended dir.

Launch pattern for an agent: prefer launching in the background so xhigh reasoning does not block,
then read the `-o` file when done. Smoke-tested baseline: a read-only STATE.md summary ran in ~15s.

## Operations discipline (learned 2026-07-04, keep)

- **Concurrency:** Codex's 5-HOUR rolling limit is the real constraint (weekly ≈ unlimited). Default
  max 2 concurrent instances (1 long + 1 short); queue the rest serially. Burst higher only when the
  user states the window has headroom.
- **Liveness:** one-shot `codex exec` arms can hang SILENTLY at startup (observed: 1h dead, zero
  scratch activity). Rule: no scratch/output activity for 30 min ⇒ presume hung, kill, relaunch with
  a SIMPLIFIED brief (fewer moving parts: no scratch copy/ports/browser unless truly needed).
- **Jobs expected >20 min** run in the SESSION DAEMON (below) instead of one-shot: live progress.log
  + mid-flight `steer:`/`interrupt`. Proven: multi-steer work-order sessions incorporated corrections
  live without restarts.

## Codex as a mid-flight-steerable sub-agent (app-server)

`codex exec` is fire-and-wait: once it starts you can only wait for it to finish. The app-server
(`codex app-server`, JSON-RPC 2.0 over stdio, SAME ChatGPT-sub auth — auth.json has NO `OPENAI_API_KEY`,
not metered API) lets an agent WATCH a run and CORRECT it mid-flight. Two bundled clients:

INTERACTIVE (recommended) — a background session with a file control plane that fits how an agent works:
```bash
# 1. launch in the background; optional first --prompt
python3 .claude/skills/codex-subagent/scripts/codex_session.py \
  --dir <session-dir> --cwd <abs-repo> --model gpt-5.6-sol --effort <low|medium|xhigh|ultra> [--prompt "<first turn>"]
# 2. WATCH live: tail <session-dir>/progress.log (turn start, each exec command, streamed answer, done)
# 3. DRIVE by appending one command per line to <session-dir>/control:
echo 'steer: <correction>' >> <session-dir>/control   # inject into the ACTIVE turn
echo 'turn: <new prompt>'  >> <session-dir>/control    # start another turn
echo 'interrupt'           >> <session-dir>/control    # stop the active turn
echo 'quit'                >> <session-dir>/control     # shut down
# 4. read the answer from <session-dir>/result.txt
```
VERIFIED across separate agent calls: launched a turn (sleep 8 → name a COLOR), watched progress.log
show the live `sleep 8`, appended `steer: name a FRUIT instead` — result came back "Apple". That is
Codex driven as a sub-agent and corrected while running.

ONE-SHOT (simpler, no live control) — single turn, optional pre-timed steer:
```bash
python3 .claude/skills/codex-subagent/scripts/codex_appserver.py "<prompt>" \
  --cwd <abs-repo> --model gpt-5.6-sol --effort <low|medium|xhigh|ultra> -o <abs-out>.txt [--steer "<text>" --steer-after <secs>]
```
Both default to read-only + approvalPolicy=never; the session takes `--sandbox workspace-write` to let
Codex edit the tree. Prefer plain `codex exec` for fire-and-forget; use these to see and steer a run.
Benign `rmcp AuthorizationRequired` MCP-noise line, same as the CLI. Protocol: https://developers.openai.com/codex/app-server
