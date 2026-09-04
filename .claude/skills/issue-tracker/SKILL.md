---
name: issue-tracker
description: A per-project issue tracker with a live kanban board at localhost:2345 (Open / Planned / In Progress / Deployed / Done / Cancelled). Use to file a bug or feature, mark it deployed, flag something for the user's attention, cancel or close an issue, check what is open, or open the board. Trigger on "file an issue", "track this bug", "what's open", "show the board", "cancel #03", "mark it deployed", "flag this for me".
---

# Issue tracker

A small issue tracker Claude drives from the CLI, with a board for the human. Issues are
numbered per project (`#01`, `#02`, …) and every project gets its own board, switchable from
the tabs at the top.

Set this once per session, then use `$ISSUES` everywhere:

```bash
ISSUES=~/.claude/skills/issue-tracker/issues.js
```

## Filing and moving issues

```bash
node $ISSUES add "Brief title" --type bug --size Small
node $ISSUES add "Brief title" --type feature --size Big --notes "optional detail"

node $ISSUES plan 3                         # → Planned
node $ISSUES start 3                        # → In Progress
node $ISSUES deploy 3                       # → Deployed (shipped, still verifying)
node $ISSUES undeploy 3                     # → back to In Progress
node $ISSUES done 3                         # → Done
node $ISSUES cancel 3 --reason "won't fix"  # → Cancelled
node $ISSUES reopen 3                       # → Open
node $ISSUES edit 3 --title "..." --size Medium
```

- `--type` is `bug` or `feature`. `--size` is `Small`, `Medium` or `Big`. Both accept a first
  letter (`--type b --size s`), and both have defaults (`feature`, `Medium`) so a bare
  `add "title"` still works.
- Issue numbers may be written `3` or `#03`.
- **Keep titles brief** — one line, imperative, no trailing period. The card shows the title and
  nothing else, so it has to carry the whole issue at a glance.

File an issue whenever you hit something real that you are not fixing right now: a bug you
noticed in passing, a follow-up the user deferred, a rough edge worth revisiting. Cancel rather
than delete — the Cancelled column is the record that it was considered.

## Flagging — ping the user for anything that needs their attention

Flag any active issue when you can't move it forward without the user, on any column except
Done or Cancelled: a question, a blocker outside your control, something only they can verify.
This is how you put something in front of them without ending the turn on it.

```bash
node $ISSUES flag 3 --reason "Need Fabian to confirm the retry budget before I change it"
node $ISSUES unflag 3
```

- The reason is required; a flag with no reason is a flag no one can act on.
- Clears automatically only on resolution — moving to Done or Cancelled answers whatever it was
  waiting on. Between any other columns (including once it's Deployed) the flag stays exactly as
  it is; moving it around doesn't clear it, only `unflag` or resolving does.
- The card is hard to miss on purpose: an amber-ringed card with the reason in a filled panel
  under the title, not a click-through. Every column's header carries a `⚑ n` count when it has
  a flagged card, so a hold is visible without opening one. `list`, `show`, and `--json` all
  surface `flagged` / `flagReason` / `flaggedAt` too.

## Deployed — a real column, not a label

An issue can be shipped and still not be Done — some things only surface once they're actually
running in production. **Deployed** sits between In Progress and Done for exactly that: code
that's live but still needs verification before it's really finished.

```bash
node $ISSUES deploy 3      # In Progress → Deployed
node $ISSUES undeploy 3    # Deployed → back to In Progress
```

- **Done means fully developed, deployed, and verified working** — not just "the code is
  written" and not just "it's deployed." If something needs production-only verification, move
  it to Deployed and leave it there; don't call `done` until you've actually confirmed it works.
- Deployed is active work, same as In Progress — it's never subject to the 12h window that
  hides old Done/Cancelled cards, and it can be flagged the same way any other active column can.

## Reading the board

```bash
node $ISSUES list                  # all six columns
node $ISSUES list open             # one column
node $ISSUES list planned
node $ISSUES list in_progress
node $ISSUES list deployed
node $ISSUES list done --json      # machine-readable
node $ISSUES show 3
node $ISSUES projects              # every project, * marks the current one
```

Prefer `list open` over `list` when you just need to know what is outstanding — it is shorter
and keeps resolved noise out of context.

## The board

**The board starts itself.** Every `issues.js` command checks port 2345 and, if nothing is
listening, spawns the server detached before doing its work — so the first `add` or `list` of a
session brings the board up and it stays up after the turn ends. When a command starts it, the
last line of output is the URL:

```
board: http://127.0.0.1:2345
```

Pass that URL on to the user the first time it appears in a session; after that the board is
already running and no line is printed.

<http://127.0.0.1:2345> — six columns, live over SSE, no reload needed when you add or cancel
something. `?theme=dark` or `?theme=light` pins the theme; otherwise it follows the OS.

| | |
|---|---|
| `ISSUES_PORT=3456` | move the port |
| `ISSUES_NO_AUTOSTART=1` or `--no-serve` | run a command without starting the board |
| `node $ISSUES serve` | run it in the foreground instead (exits 2 if the port is taken) |
| `pkill -f issue-tracker/server.js` | stop it |
| `~/.claude/issues/.server.log` | where an autostarted server's output goes |

Concurrent CLI calls can't produce duplicate servers: the loser of the race hits `EADDRINUSE`
and exits. HTTP reads are available directly — `/api/board`,
`/api/board?project=daemons-run`, `/api/board?project=daemons-run&column=open`.

The board is **view-only** — there is no drag-and-drop. Moving a card means running `start`,
`done` or `cancel`.

## How it behaves

- **Column order is Open → Planned → In Progress → Deployed → Done → Cancelled.** None of the
  steps are mandatory — `add` files into Open, and `start`/`deploy`/`done` all work directly from
  an earlier column, skipping the ones between.
- **Done and Cancelled show only the last 12 hours.** Older resolved issues stay on disk and the
  column footer says how many are hidden. Every other column — Open, Planned, In Progress,
  Deployed — is never filtered; nothing active ever disappears.
- **Projects come from the working directory**: the git repo containing the cwd, falling back to
  the cwd itself. `/Users/wesner/Herd/daemons-run` becomes the project `daemons-run`. Override
  with `--project <name|path>` to file against another board from anywhere.
- **Numbering is per project**, so every board starts at `#01`.
- **Every column sorts by creation date, youngest on top** — same order in the board, `list`,
  and `--json`. A newly filed issue always appears at the top of its column, including Done and
  Cancelled.

## Storage

```
~/.claude/issues/<project>/project.json   { name, path }
~/.claude/issues/<project>/<n>.json       one issue per file
```

```json
{ "id": 1, "title": "...", "type": "bug", "size": "Small",
  "status": "open|planned|in_progress|deployed|done|cancelled",
  "createdAt": "...", "updatedAt": "...", "resolvedAt": null,
  "notes": "", "reason": "",
  "flagged": false, "flagReason": "", "flaggedAt": null }
```

One file per issue, ids claimed with an exclusive create and writes staged through a temp file
and renamed. Two Claude sessions filing at once get separate numbers rather than a lost write,
and a reader never sees a half-written file. The board server only reads.
