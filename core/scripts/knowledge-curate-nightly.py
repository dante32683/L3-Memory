#!/usr/bin/env python3
"""knowledge-curate-nightly — cron wrapper around bin/knowledge-curate.

Run by a `--no-agent --script` cron twice daily. With --no-agent the script's
stdout is delivered verbatim and EMPTY stdout = silent, so this wrapper:

  * runs the real janitor (bin/knowledge-curate),
  * stays completely silent when the INBOX is empty (nothing happened),
  * ALWAYS reports a summary when curation ran — merges, noise dropped,
    new topics, alias refreshes — so the user knows what happened and why.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
CURATE = HERMES_HOME / "bin" / "knowledge-curate"
LOG_FILE = HERMES_HOME / "knowledge" / ".system" / "curate-full-report.log"


def _log_full_report(proc: subprocess.CompletedProcess) -> None:
    """Append this run's complete raw stdout/stderr, never truncated/parsed."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        body = (proc.stdout or "").strip()
        if proc.returncode != 0:
            body += ("\n[stderr]\n" + proc.stderr.strip()) if proc.stderr else ""
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} "
                f"(exit {proc.returncode}) ===\n{body}\n"
            )
    except Exception as e:
        print(f"⚠️ knowledge-curate: failed to write full report log: {e}", file=sys.stderr)


def main() -> int:
    if not CURATE.exists():
        return 0  # nothing installed → silent
    proc = subprocess.run(
        ["python3", str(CURATE)],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
    )
    _log_full_report(proc)
    out = (proc.stdout or "").strip()

    # Janitor failed (it preserves INBOX). Surface a one-line alert so a stall
    # is never silent — but keep it terse.
    if proc.returncode != 0:
        err = (proc.stderr or out or "unknown error").strip().splitlines()
        print("⚠️ knowledge-curate aborted (INBOX preserved): " + (err[-1] if err else "unknown"))
        return 0

    # Empty inbox = nothing to report
    if not out or "INBOX empty" in out:
        return 0

    # Always report what happened — parse the janitor's stdout and format it
    # beautifully for Telegram.
    lines = out.splitlines()

    captured, noise = 0, 0
    updates = []
    new_topics = []

    cap_match = re.search(r"captured lines:\s*(\d+)\s+noise dropped:\s*(\d+)", out)
    if cap_match:
        captured = int(cap_match.group(1))
        noise = int(cap_match.group(2))

    for line in lines:
        m_match = re.match(r"-\s*merged\s+(\d+)\s+fact\(s\)\s*[-→]\s*([a-z0-9-]+)\s*\((\d+)\s+chars\)", line)
        if m_match:
            facts, slug, chars = m_match.groups()
            updates.append({
                "slug": slug,
                "facts": int(facts),
                "chars": int(chars),
                "aliases": []
            })
            continue

        a_match = re.match(r"\s*↳\s*aliases\s+refreshed\s+for\s+([a-z0-9-]+):\s*\+\[(.*?)\]", line)
        if a_match:
            slug, raw_aliases = a_match.groups()
            aliases = [a.strip().strip("'\"") for a in raw_aliases.split(",") if a.strip()]
            for upd in updates:
                if upd["slug"] == slug:
                    upd["aliases"] = aliases
                    break
            continue

        n_match = re.match(r"-\s*🆕\s*NEW\s+topic:\s*([a-z0-9-]+)\s*\((.*?)\)\s*[-—]\s*aliases\s+\[(.*?)\]", line)
        if n_match:
            slug, title, raw_aliases = n_match.groups()
            aliases = [a.strip().strip("'\"") for a in raw_aliases.split(",") if a.strip()]
            new_topics.append({
                "slug": slug,
                "title": title.strip(),
                "aliases": aliases
            })
            continue

    blocks = ["🧠 *Knowledge Curation Complete*"]
    blocks.append(f"• Captured: {captured} line(s)")
    if noise > 0:
        blocks.append(f"• Noise dropped: {noise} line(s)")

    if updates:
        blocks.append("\n📝 *Updates:*")
        for upd in updates:
            alias_str = f" (New aliases: _{', '.join(upd['aliases'])}_)" if upd["aliases"] else ""
            blocks.append(f"• *{upd['slug']}*: Merged {upd['facts']} fact(s){alias_str}")

    if new_topics:
        blocks.append("\n🆕 *New Topics:*")
        for nt in new_topics:
            alias_str = f" (Aliases: _{', '.join(nt['aliases'])}_)" if nt["aliases"] else ""
            blocks.append(f"• *{nt['slug']}* ({nt['title']}){alias_str}")

    print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
