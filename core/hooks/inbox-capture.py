#!/usr/bin/env python3
"""inbox-capture — append-only session capture (v3 §4).

An ``on_session_finalize`` / ``on_session_end`` shell hook. When a session ends,
it appends that session's USER messages (the facts/requests the user stated) as a
timestamped, append-only block to ``$HERMES_HOME/knowledge/INBOX.md``.

Why this shape:
  * APPEND-ONLY. The hook never rewrites or reorders INBOX.md — capture must be
    lossless and never destroy earlier entries (the failure mode of "smart"
    memory that overwrites itself). A later curation step PROMOTES entries into
    the per-topic knowledge files and trims INBOX; capture itself just records.
  * DETERMINISTIC. No model, no summarization. Raw user lines only. stdlib-only
    (reads state.db with sqlite3 directly), so it survives ``hermes update`` —
    no agent-source import to break.
  * IDEMPOTENT. A session is captured at most once (cache/inbox-captured set),
    so on_session_end + on_session_finalize both firing can't double-write.

stdin: JSON {session_id, platform, ...}.  stdout: nothing (observer).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
DB = HERMES_HOME / "state.db"
INBOX = HERMES_HOME / "knowledge" / ".system" / "INBOX.md"
SEEN = HERMES_HOME / "cache" / "inbox-captured.json"
JSON_PREFIX = "\x00json:"

sys.path.insert(0, str(HERMES_HOME / "bin"))
try:
    import inbox_meta
except Exception:
    inbox_meta = None

# Skip trivial/noise user lines so INBOX stays signal.
_SKIP_RE = re.compile(r"^\s*(/\w+|hi|hey|hello|thanks?|thank you|ok|okay|yes|no|yep|nope)\s*$", re.I)
MAX_LINES = 12          # cap per session block
# NOT a content cap — capture is lossless for real facts. This is only a
# pathological-paste guard (someone pastes a 50KB log). Normal messages, even
# multi-paragraph specs, pass through whole so the nightly curator (which reads
# INBOX text directly and never dereferences state.db) routes on the FULL fact.
# When a line does exceed this, we mark it + point at the full text in state.db.
MAX_LINE_CHARS = 4000


def _load_seen() -> set:
    try:
        return set(json.loads(SEEN.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set) -> None:
    try:
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        # keep the set bounded
        SEEN.write_text(json.dumps(sorted(seen)[-2000:]))
    except Exception:
        pass


def _decode(content):
    if isinstance(content, str) and content.startswith(JSON_PREFIX):
        try:
            parts = json.loads(content[len(JSON_PREFIX):])
            if isinstance(parts, list):
                return " ".join(
                    p.get("text", "") for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                )
        except Exception:
            return ""
    return content if isinstance(content, str) else ""


def _user_lines(session_id: str) -> list[str]:
    if not DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
    except Exception:
        return []
    try:
        cur = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' "
            "AND active=1 ORDER BY id",
            (session_id,),
        )
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out: list[str] = []
    for (content,) in rows:
        text = _decode(content).strip().replace("\n", " ")
        if not text or _SKIP_RE.match(text):
            continue
        if len(text) > MAX_LINE_CHARS:
            # Pathological length only: truncate but leave a visible, recoverable
            # marker (never a silent slice) pointing at the full text in state.db.
            text = (f"{text[:MAX_LINE_CHARS]}…[+{len(text) - MAX_LINE_CHARS} "
                    f"chars — full text in state.db session {session_id}]")
        out.append(text)
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    extra = payload.get("extra") or {}
    session_id = str(payload.get("session_id") or extra.get("session_id") or "").strip()
    if not session_id:
        return 0
    seen = _load_seen()
    if session_id in seen:
        return 0
    lines = _user_lines(session_id)
    if not lines:
        seen.add(session_id)        # nothing to capture, but don't re-scan
        _save_seen(seen)
        return 0

    platform = str(extra.get("platform") or payload.get("platform") or "cli")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    captured = lines[:MAX_LINES]
    omitted = max(len(lines) - MAX_LINES, 0)
    block = [f"\n## {ts} · {platform} · {session_id}"]
    for ln in captured:
        block.append(f"- {ln}")
    if omitted:
        block.append(f"- …(+{omitted} more)")

    INBOX.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX.exists():
        INBOX.write_text(
            "# INBOX — append-only capture\n\n"
            "Raw user lines per session. NEVER rewritten by capture; a curation "
            "step promotes facts into the per-topic knowledge files and trims this.\n",
            encoding="utf-8",
        )
    with INBOX.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    if inbox_meta:
        try:
            inbox_meta.append_records(
                INBOX,
                block_id=inbox_meta.block_id(ts, "inbox-capture", session_id),
                timestamp=ts,
                source="inbox-capture",
                source_class="user_direct",
                trust="user",
                session_id=session_id,
                platform=platform,
                lines=captured,
                max_lines=MAX_LINES,
                omitted_lines=omitted,
            )
        except Exception:
            pass

    seen.add(session_id)
    _save_seen(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
