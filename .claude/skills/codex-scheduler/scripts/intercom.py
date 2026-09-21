#!/usr/bin/env python3
"""Push a notice into a live Claude Code session's cross-session inbox.

Each interactive Claude Code process binds a Unix socket (usually /tmp/cc-socks/<pid>.sock)
and registers under ~/.claude/sessions/<pid>.json plus a sibling .key holding peerToken.
Posting newline-delimited JSON -- auth, then a user message -- arrives in that session as a
peer message, without a backgrounded `wait`. Used for both successful settles and failures.
"""
import glob
import json
import os
import socket

SESSIONS_DIR = os.environ.get(
    "CLAUDE_SESSIONS_DIR",
    os.path.expanduser("~/.claude/sessions"),
)
MAX_BODY = 8000
SOCKET_TIMEOUT = 3.0
FROM_LABEL = "codex-scheduler"


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError, TypeError):
        return False


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _key_path(pid):
    matches = glob.glob(os.path.join(SESSIONS_DIR, f"{pid}.*.key"))
    return matches[0] if matches else None


def find_session(session_id):
    """Return the live inbox for a Claude session id, or None.

    Matches `sessionId` (the UUID jobs store today) or `bridgeSessionId` (older submits).
    Stale registry rows whose process or socket is gone are skipped.
    """
    if not session_id:
        return None
    want = str(session_id).strip()
    if not os.path.isdir(SESSIONS_DIR):
        return None
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        base = os.path.basename(path)
        if not base.endswith(".json") or "." in base[:-5]:
            continue
        try:
            meta = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        ids = {str(meta.get("sessionId") or ""), str(meta.get("bridgeSessionId") or "")}
        if want not in ids:
            continue
        pid = meta.get("pid")
        sock = meta.get("messagingSocketPath")
        if not _pid_alive(pid) or not sock or not os.path.exists(sock):
            continue
        key_file = _key_path(pid)
        if not key_file:
            return None
        try:
            token = (_read_json(key_file) or {}).get("peerToken")
        except (OSError, json.JSONDecodeError):
            return None
        if not token:
            return None
        return {
            "pid": pid,
            "name": meta.get("name") or "",
            "sessionId": meta.get("sessionId"),
            "bridgeSessionId": meta.get("bridgeSessionId"),
            "socket": sock,
            "token": token,
        }
    return None


def inbox_reachable(session_id):
    return find_session(session_id) is not None


def post_peer_message(socket_path, token, content, from_label=FROM_LABEL):
    """Write auth + user NDJSON to a session inbox. Returns True on a completed write."""
    if not socket_path or not token or content is None:
        return False
    body = content if len(content) <= MAX_BODY else (content[: MAX_BODY - 20] + "\n…[truncated]")
    lines = [
        json.dumps({"type": "auth", "token": token}, separators=(",", ":")),
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": body},
                "from": from_label,
            },
            separators=(",", ":"),
        ),
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    try:
        sock.connect(socket_path)
        sock.sendall(payload)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            sock.recv(256)
        except (OSError, TimeoutError):
            pass
        return True
    except (OSError, TimeoutError):
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _clip(text, n=3500):
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 20] + "\n…[truncated]"


def format_settled(job):
    """Human text for a terminal job -- success, failure, stop, or blocked."""
    status = job.get("status") or "unknown"
    slug = job.get("slug") or "?"
    jid = job.get("id")
    head = f"[codex-scheduler] job #{jid} ({slug}) {status}"
    bits = [head]
    if status == "done":
        if job.get("schema_error"):
            bits.append(f"schema mismatch: {job['schema_error']}")
        bits.append(_clip(job.get("result") or "(empty result)"))
    elif status == "blocked":
        bits.append("BLOCKED: needs a human decision before it can continue.")
        bits.append(_clip(job.get("result") or job.get("error") or "(empty)"))
    else:
        bits.append(f"error: {job.get('error') or '(no error recorded)'}")
        if job.get("checkpoint"):
            bits.append(f"last checkpoint ({job.get('checkpoint_at') or '?'}):")
            bits.append(_clip(job["checkpoint"], 1500))
    bits.append(f"Full detail: scheduler_cli.py show {slug}")
    return "\n".join(bits)


def format_message(job, text):
    slug = job.get("slug") or "?"
    jid = job.get("id")
    return (
        f"[codex-scheduler] message from running job #{jid} ({slug})\n"
        f"{_clip(text)}\n"
        f"Full detail: scheduler_cli.py show {slug}"
    )


def notify_session(session_id, content):
    meta = find_session(session_id)
    if not meta:
        return False
    return post_peer_message(meta["socket"], meta["token"], content)


def notify_job_settled(job):
    sid = job.get("claude_session_id")
    if not sid:
        return False
    return notify_session(sid, format_settled(job))


def notify_job_message(job, text):
    sid = job.get("claude_session_id")
    if not sid:
        return False
    return notify_session(sid, format_message(job, text))


def _iso_epoch(s):
    if not s:
        return 0.0
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


TERMINAL = ("done", "failed", "stopped", "blocked")


def deliver_pending(conn, since_epoch, log=None):
    """Push any recent un-notified settles or job messages. Safe to call every tick.

    `since_epoch` is the daemon start time. Older terminal rows with notified=0 are left
    alone so a restart does not dump the job history into the inbox.
    """
    slack = since_epoch - 5
    terminal = ",".join(f"'{st}'" for st in TERMINAL)

    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM jobs WHERE notified=0 AND status IN ({terminal}) AND finished_at IS NOT NULL"
    ).fetchall()]
    for job in rows:
        if _iso_epoch(job.get("finished_at")) < slack:
            continue
        if not find_session(job.get("claude_session_id")):
            continue
        claimed = conn.execute(
            "UPDATE jobs SET notified=1 WHERE id=? AND notified=0", (job["id"],)
        ).rowcount
        conn.commit()
        if not claimed:
            continue
        ok = notify_job_settled(job)
        if ok:
            if log:
                log(f"inbox: notified session {job.get('claude_session_id')} "
                    f"that job #{job['id']} ({job['slug']}) {job['status']}")
        else:
            conn.execute("UPDATE jobs SET notified=0 WHERE id=?", (job["id"],))
            conn.commit()
            if log:
                log(f"inbox: write failed for job #{job['id']} ({job['slug']}); will retry")

    msgs = [dict(r) for r in conn.execute(
        """SELECT m.id AS mid, m.text AS text, m.created_at AS created_at,
                  j.id AS job_id, j.slug AS slug,
                  j.claude_session_id AS claude_session_id
           FROM job_messages m JOIN jobs j ON j.id = m.job_id
           WHERE m.notified=0"""
    ).fetchall()]
    for m in msgs:
        if _iso_epoch(m.get("created_at")) < slack:
            continue
        if not find_session(m.get("claude_session_id")):
            continue
        claimed = conn.execute(
            "UPDATE job_messages SET notified=1 WHERE id=? AND notified=0", (m["mid"],)
        ).rowcount
        conn.commit()
        if not claimed:
            continue
        job = {"id": m["job_id"], "slug": m["slug"], "claude_session_id": m["claude_session_id"]}
        ok = notify_job_message(job, m["text"])
        if ok:
            if log:
                log(f"inbox: delivered message #{m['mid']} from job #{job['id']} ({job['slug']})")
        else:
            conn.execute("UPDATE job_messages SET notified=0 WHERE id=?", (m["mid"],))
            conn.commit()
            if log:
                log(f"inbox: write failed for message #{m['mid']} from job #{job['id']}; will retry")
