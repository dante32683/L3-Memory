#!/usr/bin/env python3
"""doc-drift-check-cron — cron wrapper around bin/doc-drift-check.

Run by a `--no-agent --script` cron every 2 days. With --no-agent the script's
stdout is delivered verbatim and EMPTY stdout = silent, so this wrapper:

  * runs the deterministic checker (bin/doc-drift-check),
  * stays completely silent when there is no drift,
  * emits a compact Telegram digest when drift is found (counts + a few
    preview items per section; full detail lives in the report file),
  * surfaces a terse alert if the checker aborts, so a stall can't hide.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
CHECKER = HERMES_HOME / "bin" / "doc-drift-check"
REPORT = HERMES_HOME / "knowledge" / ".system" / f"drift-{date.today().isoformat()}.md"
REPORT_REL = "knowledge/.system/drift-<date>.md"
MAX_PREVIEW = 3  # items shown inline per section; full detail is in the report


def _preview(section: str, limit: int = MAX_PREVIEW) -> list[str]:
    """Pull up to `limit` item titles (### headings) from one report section.

    Sections are delimited by the `## FIXABLE` / `## UNSURE` headers the checker
    writes. Returns bullet strings; empty if the section has no items.
    """
    if not REPORT.exists():
        return []
    text = REPORT.read_text(encoding="utf-8", errors="replace")
    # Slice from this section header to the next `## ` header (or EOF).
    m = re.search(rf"^## {re.escape(section)}\b", text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    titles = re.findall(r"^### (.+)$", body, re.M)
    return [f"• {t.strip()}" for t in titles[:limit]]


def main() -> int:
    if not CHECKER.exists():
        return 0  # nothing installed → silent

    proc = subprocess.run(
        ["python3", str(CHECKER)],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
    )
    out = (proc.stdout or "").strip()

    # Checker crashed. Surface a one-line alert so a stall is never silent.
    if proc.returncode != 0:
        err = (proc.stderr or out or "unknown error").strip().splitlines()
        print("⚠️ doc-drift-check aborted: " + (err[-1] if err else "unknown"))
        return 0

    if "drift: clean" in out or not out:
        return 0  # silent on success — N days of nothing is not a notification

    fix_m = re.search(r"(\d+) fixable", out)
    uns_m = re.search(r"(\d+) unsure", out)
    n_fix = int(fix_m.group(1)) if fix_m else 0
    n_uns = int(uns_m.group(1)) if uns_m else 0

    blocks = ["📋 *Doc Drift Check*"]
    blocks.append(f"• Fixable (high confidence): {n_fix}")
    if n_uns:
        blocks.append(f"• Unsure (needs review): {n_uns}")

    fix_preview = _preview("FIXABLE")
    if fix_preview:
        blocks.append("\n*Fixable:*")
        blocks.extend(fix_preview)
        if n_fix > len(fix_preview):
            blocks.append(f"• …and {n_fix - len(fix_preview)} more")

    uns_preview = _preview("UNSURE")
    if uns_preview:
        blocks.append("\n*Unsure:*")
        blocks.extend(uns_preview)
        if n_uns > len(uns_preview):
            blocks.append(f"• …and {n_uns - len(uns_preview)} more")

    blocks.append(f"\nFull report: `{REPORT_REL}`")
    print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
