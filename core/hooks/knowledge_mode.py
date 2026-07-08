"""knowledge_mode — shared per-platform HERMES_KNOW_MODE state (Phase 2).

Single source of truth for the L3-injection mode toggle, imported by BOTH:
  * ``hooks/knowledge-inject.py`` (same directory — imports directly), and
  * the ``know-mode`` plugin command (``$HERMES_HOME/plugins/know-mode/``,
    which does ``sys.path.insert(0, $HERMES_HOME/hooks)`` first — same
    precedent as ``hooks/office-lock-gate.py`` importing office-tools).

Why a shared module instead of duplicating the JSON read in each side:
the ONE correctness invariant is that the hook and the command must resolve
the *same* per-platform key for a given session. The raw platform string
differs by code path — the hook receives ``agent.platform`` (may be "cli"),
the command reads ``HERMES_SESSION_PLATFORM`` (``Platform.LOCAL.value`` ==
"local") — so both sides MUST run the identical ``platform_key`` normalizer.
Keeping that normalizer in one file makes drift impossible.

State file: ``$HERMES_HOME/knowledge/.system/know-mode.json``
Shape:      ``{"tui": "toc", "telegram": "toc"}``  (keys are normalized
            logical channels, not raw platform strings).

Modes:
  * ``off``  — no L3 injection at all (no TOC, no file bodies).
  * ``toc``  — capped per-turn index of matched topics + static recipe block.
  * ``full`` — legacy behavior: inject whole matched file bodies.

Default (missing file, missing key, or an unrecognized value): ``toc``.
Conservative rollout per the plan — no channel starts on ``full``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
_STATE_PATH = HERMES_HOME / "knowledge" / ".system" / "know-mode.json"

VALID_MODES = ("off", "toc", "full")
DEFAULT_MODE = "toc"


def platform_key(raw: str | None) -> str:
    """Collapse a raw platform string into a logical channel key.

    Every terminal-ish variant this instance emits (``Platform.LOCAL.value``
    == "local" on the command side, ``agent.platform`` == "cli"/"tui" on the
    hook side, or an empty string when unset) maps to the single ``"tui"``
    bucket, matching the plan's two-channel intent ("TUI and Telegram each
    have their own mode"). ``telegram`` maps to itself. Any genuinely new
    platform keys by its own lowercased name — consistently on both sides, so
    they still agree — and simply gets DEFAULT_MODE until someone sets it.
    """
    p = (raw or "").strip().lower()
    if p == "telegram":
        return "telegram"
    if p in ("", "local", "cli", "tui", "terminal"):
        return "tui"
    return p


def read_all() -> dict:
    """Whole state dict, ``{}`` on any error (missing file / bad JSON)."""
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_mode(raw_platform: str | None) -> str:
    """Current mode for the session's platform, DEFAULT_MODE if unset/invalid."""
    v = read_all().get(platform_key(raw_platform))
    return v if v in VALID_MODES else DEFAULT_MODE


def set_mode(raw_platform: str | None, mode: str) -> str:
    """Persist ``mode`` for the session's platform. Returns the normalized key.

    Atomic write (temp file + ``os.replace``) so a concurrent read from the
    hook never sees a half-written file. Raises ``ValueError`` on a bad mode
    so the command handler can report it rather than silently no-op.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {VALID_MODES}")
    key = platform_key(raw_platform)
    state = read_all()
    state[key] = mode
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_STATE_PATH.parent), prefix=".know-mode.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, _STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key
