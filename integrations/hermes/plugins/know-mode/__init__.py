"""know-mode — /know-mode slash command (Phase 2 of the knowledge/skill plan).

Lets the USER switch the per-platform L3 knowledge-injection mode that
``hooks/knowledge-inject.py`` reads on every ``pre_llm_call``:

  * ``off``  — inject nothing (no TOC, no file bodies).
  * ``toc``  — inject a capped index of the topics this turn's wording named,
               plus a static search recipe (the default).
  * ``full`` — inject whole matched knowledge file bodies (legacy, heavier).

Why a plugin slash command rather than a skill or a tool: the plan requires
this to be USER-ONLY and changeable mid-chat. A slash command is typed by the
human and dispatched by the gateway's flat plugin-command lookup — the model
has no way to invoke it, so "user-only" holds by construction. (Spike finding:
``PluginContext.register_command`` needs zero upstream changes and lives
entirely here, consistent with the never-patch-upstream rule.)

The command handler and the hook share ONE source of truth for the state file
and the platform normalizer — ``hooks/knowledge_mode.py`` — so the two sides
can never key a different per-platform entry for the same session.

Survives ``hermes update`` (lives under $HERMES_HOME/plugins).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
_HOOKS_DIR = HERMES_HOME / "hooks"


def _load_km():
    """Import the shared knowledge_mode module (same precedent as
    hooks/office-lock-gate.py importing office-tools via sys.path)."""
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    import knowledge_mode  # noqa: E402
    return knowledge_mode


def _raw_platform() -> str:
    """Raw platform string for the calling session. get_session_env is
    task-local (contextvar-backed), so this is correct under the gateway's
    concurrent sessions; falls back to the process env for CLI/cron."""
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM", "") or ""
    except Exception:
        return os.environ.get("HERMES_SESSION_PLATFORM", "") or ""


def _legend() -> str:
    return (
        "Modes:\n"
        "  • off  — no knowledge auto-injected\n"
        "  • toc  — capped index of matched topics + a search recipe (default)\n"
        "  • full — inject the whole matched knowledge file(s) [legacy, heavier]"
    )


def _status(km, raw_platform: str, key: str) -> str:
    state = km.read_all()
    channels = sorted(set(["tui", "telegram"]) | set(state.keys()))
    rows = []
    for ch in channels:
        m = state.get(ch)
        label = m if m in km.VALID_MODES else f"{km.DEFAULT_MODE} (default)"
        marker = "   ← this session" if ch == key else ""
        rows.append(f"  {ch:<10} {label}{marker}")
    return (
        "**Knowledge injection mode (per platform)**\n"
        + "\n".join(rows)
        + "\n\nChange it for THIS channel:  `/know-mode off|toc|full`\n\n"
        + _legend()
    )


def _handle(raw_args: str):
    try:
        km = _load_km()
    except Exception as exc:  # pragma: no cover
        logger.warning("know-mode: could not import knowledge_mode: %s", exc)
        return "⚠️ know-mode: shared state module (hooks/knowledge_mode.py) is unavailable."

    raw_platform = _raw_platform()
    key = km.platform_key(raw_platform)
    arg = (raw_args or "").strip().lower()

    if arg in ("", "status", "show", "?"):
        return _status(km, raw_platform, key)

    if arg in km.VALID_MODES:
        try:
            km.set_mode(raw_platform, arg)
        except Exception as exc:
            logger.warning("know-mode: set_mode failed: %s", exc)
            return f"⚠️ know-mode: failed to persist mode: {exc}"
        return (
            f"✅ Knowledge injection mode for **{key}** set to **{arg}**. "
            "Takes effect on your next message.\n\n" + _legend()
        )

    return (
        f"❓ Unknown mode {arg!r}. Expected off, toc, or full.\n\n"
        + _status(km, raw_platform, key)
    )


def register(ctx) -> None:
    ctx.register_command(
        "know-mode",
        handler=_handle,
        description="View or set this platform's L3 knowledge-injection mode (off/toc/full).",
        args_hint="[off|toc|full]",
    )
    logger.info("know-mode: /know-mode command registered")
