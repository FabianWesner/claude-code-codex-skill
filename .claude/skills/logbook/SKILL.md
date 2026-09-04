---
name: logbook
description: Write an entry to the user's logbook artifact (hourly reports, plus ad-hoc alerts and questions). Use on every /loop tick and whenever something needs the user's attention.
---

# Logbook

**A template.** Before using this, fill in `PROJECT_NAME` and `ARTIFACT_URL` below, and adjust the
writing rules to match how your user actually wants to be talked to — the defaults here are a
reasonable starting point, not fixed requirements.

**Only one session should write to a given logbook.** No cloud routine, no sub-agent, no second
session publishing entries in parallel — two publishers with different instructions produce a
mixed, inconsistent feed. Decide which session owns it (usually the main orchestrator for the
project) and have every other session leave it alone.

A Twitter-style feed artifact where Claude reports to the user. Short entry every hour (driven by
/loop), plus ad-hoc entries at any time.

- **Project:** `PROJECT_NAME` — set this once, use it in the header and in your own head as
  "what is this logbook for."
- **Artifact URL:** `ARTIFACT_URL` — publish `logbook.html` once with the Artifact tool, then
  paste the URL it gives you here. Every future publish must pass this same `url`, or you'll
  create a second, separate artifact by mistake.
- **Canonical local file:** `~/.claude/skills/logbook/logbook.html` (edit this, then republish)

## What the logbook is for (read this before writing anything)

The user wants to know **what changed for them**: what they can go and try out, what got better,
what is broken. They are not interested in who did the work, how it was organised, or what is
currently in flight.

**Write about outcomes:**
- A feature they can now use, and where to find it (link or page name).
- A fix to something they reported or would notice, described from their side ("the copied link
  on the phone is complete again", not the mechanism).
- An improvement they would feel: faster, clearer, less broken.
- Something that broke or is at risk, and whether they need to do anything.

**Do not write about:**
- Agents, lanes, models, job or deployment ids, commit shas, QA cycle numbers, findings row numbers.
- Work in progress, dispatches, plans, what is being investigated. Report a thing when it is live,
  not when it starts.
- Internal refactors, test coverage, tooling and process changes, unless they visibly change the
  product.
- Reassurance filler ("everything is stable", "memory is fine") unless that IS the news.

If an hour produced nothing the user can try or feel, say so in one line. That is a perfectly good
entry, and much better than dressing up process as progress.

## How to write an entry

1. Read `~/.claude/skills/logbook/logbook.html`. If another session may have published since (or a
   publish conflicts), first `Artifact read` the URL and sync the local file to the live version
   before editing.
2. Prepend a new `<article class="entry TYPE">` at the top of the `.feed` section, under today's
   `.day` header (create the day header if it is a new day, format `Monday, September 1`).
3. Update the "updated YYYY-MM-DD" date in the header `.sub`.
4. Maintain the **Open questions** box at the top: add a `<li>` for each new unanswered question
   (with the time it was asked); remove items once the user has answered. If empty, use
   `<li class="none">No open questions right now.</li>`.
5. Republish with the Artifact tool: `file_path` = the local file, `url` = the artifact URL above.
   Never publish without `url` (that would create a separate artifact). Do not pass `favicon` on
   republish.
6. The local file IS the source; keep it in sync.

## Entry template

```html
<article class="entry report">
  <div class="avatar">C</div>
  <div>
    <div class="meta">
      <span class="name">Claude</span>
      <span class="handle">@claude</span>
      <span class="dot">·</span>
      <span class="time">Sep 1, 17:50</span>
      <span class="chip report">Report</span>
    </div>
    <div class="body">
      <p>Human-readable summary...</p>
      <img src="data:image/jpeg;base64,..." alt="what it shows">
    </div>
  </div>
</article>
```

Types (both the entry class and the chip class/label):
- `report` / chip `Report`: what shipped or changed for the user this hour.
- `alert` / chip `Alert`: something broke, is at risk, or needs awareness now.
- `question` / chip `Question`: needs an answer from the user. Also add it to the Open questions box.

## Writing rules

These are opinionated defaults — good starting rules, not requirements. Adjust them to match
actual feedback from your user; keep the ones that hold up, drop or rewrite the ones that don't.

- **Outcomes, not ceremony.** Every sentence should tell the user something they can act on or
  try. Cut anything that is about how the work happened.
- **Short.** Hard cap: 3 sentences or 3 one-line bullets per hourly report, about 60 words, one
  paragraph. Lead with the outcome. Alerts and questions may be one sentence longer. **Never edit,
  shorten, or trim a published entry afterwards** — the artifact should always show the full
  original text of every entry, so readers can trust the timeline.
- **Show it, do not just describe it.** Illustrate visual and UI changes with a screenshot in the
  entry. Prefer an after-screenshot of the real thing on the relevant device; a before/after pair
  when the change only makes sense as a comparison. A phone change gets a phone screenshot.
- **Human-readable, not agent jargon.** Assume the user reads this on their phone.
- **Times** in the user's timezone, format `Sep 1, 17:50` (date + 24h time in every entry). Alerts
  and questions are regular posts in the timeline, in chronological position; open questions are
  additionally mirrored in the pinned box.
- Links are welcome (the exact page or screen the user should open). Use `<a href>`.
- Keep the feed newest-first. When the feed exceeds ~10 days, trim the oldest days.

## Screenshots

Embed as compressed data URIs so the page stays self-contained:

```bash
sips -Z 800 -s format jpeg -s formatOptions 60 shot.png --out /tmp/shot.jpg
printf '<img src="data:image/jpeg;base64,%s" alt="...">' "$(base64 -i /tmp/shot.jpg)"
```

- Resize to at most 800px wide, JPEG quality about 60.
- One or two images per entry, only where they carry the point.
- The whole page must stay under 16MB. When it grows large, replace the oldest images with a
  short text note in place of the `<img>`, leaving the entry text untouched.

## Hourly loop behavior

On a /loop tick, ask one question: **what changed for the user since the last entry?**

Check what actually reached production (deployments that finished, fixes now live) and what the
user reported that is now fixed. Write one `report` entry about that, with a screenshot when it is
visual. If nothing reached them, one line saying so. If a real problem or decision surfaced, add a
separate `alert` or `question` entry rather than burying it in the report.
