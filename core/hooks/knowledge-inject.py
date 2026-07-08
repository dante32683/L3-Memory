#!/usr/bin/env python3
"""knowledge-inject — deterministic L3 knowledge injection (v3 §4).

A ``pre_llm_call`` shell hook. Reads the incoming user message, matches it
against the OWNED knowledge files in ``$HERMES_HOME/knowledge/*.md`` purely by
FILE IDENTITY (slug + ``aliases:`` frontmatter + H1 + ``##`` headings), and
returns the WHOLE matched file(s) as ``{"context": ...}`` to be appended to the
user message.

Design rules (the point of the clean slate):
  * No model. No gemma enrichment. No keyword cache cron to go stale.
  * The index is rebuilt from the files themselves on every call (N is tiny).
    Knowledge edits take effect immediately; there is nothing to "refresh."
  * Whole-file push: files are kept small (nightly cap-and-split), so we inject
    the entire file, not fragments — no retrieval ranking to get subtly wrong.
  * Deterministic selection: a file matches iff a strong identity token (slug or
    an alias) appears in the message, or enough title/heading tokens overlap.

stdin: JSON {user_message, is_first_turn, ...}.  stdout: JSON {"context": "..."}.
Silence (no stdout) when nothing matches — the hook then contributes nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))
KNOW = HERMES_HOME / "knowledge"

# Shared per-platform mode state (single source of truth for the platform
# normalizer, so this hook and the /know-mode command never key different
# entries). Same-dir import; insert defensively in case sys.path[0] isn't the
# hook dir under some invocation. If the import fails, the hook falls back to
# legacy whole-file behavior ("full") — a missing toggle must never break
# injection.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import knowledge_mode as _km
except Exception:
    _km = None
# knowledge_topics.py lives in bin/, a sibling of this hooks/ dir.
sys.path.insert(0, str(HERMES_HOME / "bin"))
import knowledge_topics as _kt

# Budgets: keep injected context bounded regardless of how many files match.
# Strong-only injection: weak (title/heading-overlap) matches are NOT injected
# — they were the low-value, cache-expensive case (every injected turn shifts the
# prompt prefix and re-charges the prior turn's tool chain as uncached). Weak
# topics still surface via the turn-1 TOC, and the model is told to reach for the
# `search_knowledge` tool when a request touches a topic that wasn't auto-loaded.
MAX_STRONG_FILES = int(os.environ.get("HERMES_KNOW_MAX_STRONG_FILES", "5"))
MAX_WEAK_FILES = int(os.environ.get("HERMES_KNOW_MAX_WEAK_FILES", "0"))
# Per-file cap must stay >= the largest knowledge file so a strong match is never
# truncated mid-content (largest KB files ~15.3K: hermes-knowledge.md,
# ref-provider-rates.md). 16K gives ~700 char headroom; raise this if a
# knowledge file grows past it (the nightly cod-and-split should keep files well under).
STRONG_PER_FILE_CHARS = int(os.environ.get("HERMES_KNOW_STRONG_PER_FILE_CHARS", "16000"))
WEAK_PER_FILE_CHARS = int(os.environ.get("HERMES_KNOW_WEAK_PER_FILE_CHARS", "4000"))
TOTAL_CHARS = int(os.environ.get("HERMES_KNOW_TOTAL_CHARS", "34000"))

_STOP = {
    "the", "and", "for", "with", "you", "your", "are", "was", "what", "whats",
    "how", "can", "did", "does", "this", "that", "have", "has", "get", "got",
    "tell", "about", "give", "show", "from", "any", "all", "out", "now", "please",
    "context", "project", "info", "plan", "setup", "they", "them", "his", "her",
}

# Generic terms (already stemmed) that must NEVER act as identity tokens — neither
# as an authored alias, nor as a slug-derived token (e.g. the slug
# "ref-canvas-tools" decomposes into "tool", which would otherwise match EVERY
# message mentioning a tool), nor as a heading token. These are common words that
# appear across unrelated prompts; letting them select a knowledge file produces
# false-positive injections. Kept as stems so plural/singular both collapse here.
#
# SINGLE SOURCE OF TRUTH: knowledge/.system/generic-blocklist.txt — shared with
# bin/knowledge-curate (the alias writer) so the two can never drift. The file
# holds one singular/stem word per line; #-comments and blank lines are ignored.
_BLOCKLIST_FILE = KNOW / ".system" / "generic-blocklist.txt"


def _load_generic() -> set[str]:
    try:
        return {
            ln.strip()
            for ln in _BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }
    except Exception:
        return set()


_GENERIC = _load_generic()


def _stem(w: str) -> str:
    """Lightweight singular/plural normalizer so e.g. 'backups' matches the
    alias 'backup', 'classes' matches 'class', 'policies' matches 'policy'.
    Applied to BOTH message tokens and identity tokens, so matching is
    plural-insensitive without anyone having to author both forms."""
    if len(w) < 4:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]          # classes->class, boxes->box, dishes->dish
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]          # backups->backup, crons->cron (but class stays class)
    return w


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()):
        if w in _STOP:
            continue
        s = _stem(w)
        if s in _GENERIC:
            continue
        out.add(s)
    return out


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end]
    body = text[end + 4:].lstrip("\n")
    fm: dict = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


_topics = _kt.iter_topics       # every topic is a folder now, one implementation
_manifest = _kt.manifest


def _identity(slug: str, text: str) -> dict:
    """Strong tokens (slug+aliases) and weak tokens (title+headings) for a file."""
    fm, body = _parse_frontmatter(text)
    strong: set[str] = set(re.split(r"[-_]+", slug.lower()))
    strong.add(slug.lower())
    aliases_raw = fm.get("aliases", "")
    m = re.search(r"\[(.*?)\]", aliases_raw)
    if m:
        for a in m.group(1).split(","):
            a = a.strip().strip("\"'").lower()
            if a:
                strong.add(a)
                # also add individual words from multi-word aliases so e.g.
                # "luna gabriela" matches a message containing just "luna"
                for part in a.split():
                    if len(part) >= 3:
                        strong.add(part)
    weak: set[str] = set()
    title = fm.get("title", "")
    weak |= _tokens(title)
    for h in re.findall(r"^#{1,3}\s+(.+)$", body, re.MULTILINE):
        weak |= _tokens(h)
    strong = {
        st for s in strong
        if len(s) >= 3 and s not in _STOP and (st := _stem(s)) not in _GENERIC
    }
    return {"slug": slug, "strong": strong, "weak": weak - strong, "text": text}


def _score(idx: dict, msg_tokens: set[str]) -> int:
    """Strong identity hit dominates; weak (heading) overlap is a tiebreak boost."""
    strong_hits = len(idx["strong"] & msg_tokens)
    weak_hits = len(idx["weak"] & msg_tokens)
    if strong_hits:
        return 100 * strong_hits + weak_hits
    # No identity hit: require a meaningful heading overlap to fire at all.
    if weak_hits >= 2:
        return weak_hits
    return 0


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[...truncated]\n"


def _toc() -> str:
    """A compact table of contents of every knowledge topic — one line per file
    (slug · aliases · H1). Injected on the first turn so the model KNOWS what
    context exists and can pull a miss via the search_knowledge tool, even when
    identity matching didn't auto-load anything. Cheap: ~1 line/file, no bodies."""
    lines: list[str] = []
    for slug, text, _files_dir in _topics():
        fm, body = _parse_frontmatter(text)
        title = ""
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if m:
            title = m.group(1).strip()
        am = re.search(r"\[(.*?)\]", fm.get("aliases", ""))
        aliases = ""
        if am:
            parts = [a.strip().strip("\"'") for a in am.group(1).split(",") if a.strip()]
            if parts:
                aliases = " (aka " + ", ".join(parts[:4]) + ")"
        desc = f" — {title}" if title and title.lower() != slug.replace("-", " ") else ""
        lines.append(f"- **{slug}**{aliases}{desc}")
    if not lines:
        return ""
    return (
        "[SYSTEM-INJECTED, NOT WRITTEN BY THE USER — appended below the user's actual "
        "message by an automated hook on the first turn of the session. The user did "
        "not type this block and has not seen it.]\n"
        "# Knowledge base — table of contents\n"
        "These topics exist in the user's personal knowledge base. Relevant ones are "
        "auto-loaded when your wording names them, but that can miss. If a request "
        "touches a topic below that you lack loaded context for, call **search_knowledge** "
        "before answering — do not guess. It also searches your skills. Usage: "
        "`search_knowledge(query=…)` returns candidates (slug + summary + outline; tiny "
        "files inlined); then `search_knowledge(mode='section', slug=…, section=…)` or "
        "`mode='full'` reads exactly what you need.\n\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# toc mode (HERMES_KNOW_MODE == "toc") — Phase 2
#
# Instead of injecting whole file bodies, inject a capped per-turn INDEX of the
# topics this message's wording named: slug · aliases · a one-line "read when"
# note. Plus a static recipe block (constant every turn → cache-stable) that
# teaches the search_knowledge/skill_view recipe. The model pulls a full file
# on demand only when actually relevant. Budget is locked by the plan:
#   * 1000-char hard cap across all matched entries combined (NOT counting the
#     constant recipe block, which is cacheable and separate).
#   * each "read when" string capped at 80 chars, AND their sum capped at 400
#     (40% of the 1000 total) — both apply at once.
#   * overflow drops WHOLE entries (never truncates one mid-string), ranked by
#     match strength, and names exactly what was dropped so nothing vanishes
#     silently.
TOC_TOTAL_CHARS = 1000
TOC_READ_WHEN_PER = 80
TOC_READ_WHEN_TOTAL = 400          # 40% of TOC_TOTAL_CHARS
TOC_MAX_ALIASES_CHARS = 60

_TOC_RECIPE = (
    "[SYSTEM-INJECTED, NOT WRITTEN BY THE USER — appended below the user's actual "
    "message by an automated hook, based on keyword/identity matches found in that "
    "message's text. The user did not type this block and has not seen it.]\n"
    "# Personal knowledge base — how to use it\n"
    "The user keeps a curated knowledge base (facts: contacts, server config, "
    "provider/rate tables, project state) and a skill library (procedures). "
    "Topics whose identity your message named THIS turn are listed below with a "
    "short \"read when\" note. Only fetch a file if it is actually relevant to "
    "the current request — do not fetch reflexively just because it is listed.\n"
    "- Find + read a knowledge fact OR a skill: call **search_knowledge**. "
    "`mode='search'` lists candidates (slug, summary, outline; tiny files inlined); "
    "then `mode='section'`/`'full'` with the `slug` reads exactly what you need.\n"
    "- Open a skill in full: call **skill_view** (or search_knowledge mode='full', kind='skill').\n"
    "- The base is larger than what's listed; if a request touches a topic not "
    "shown, call **search_knowledge** to discover and load it before "
    "answering — do not guess."
)


def _use_when(fm: dict, body: str, slug: str) -> str:
    """The one-line 'read when' note for a topic. Prefers a curated
    ``use_when:`` frontmatter field; falls back to the H1 title, then the
    title frontmatter, then the de-slugged name — so un-annotated files still
    render a usable entry (graceful degradation while use_when is rolled out)."""
    v = fm.get("use_when", "").strip().strip("\"'")
    if v:
        return v
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fm.get("title", "").strip() or slug.replace("-", " ")


def _toc_entry_fields(slug: str, text: str) -> tuple[str, str]:
    """(aliases_str, read_when) for one topic, with aliases capped to whole
    entries under TOC_MAX_ALIASES_CHARS (identity tokens stay intact, never
    clipped mid-word)."""
    fm, body = _parse_frontmatter(text)
    aliases = ""
    am = re.search(r"\[(.*?)\]", fm.get("aliases", ""))
    if am:
        parts = [a.strip().strip("\"'") for a in am.group(1).split(",") if a.strip()]
        acc: list[str] = []
        for p in parts:
            if len(", ".join(acc + [p])) > TOC_MAX_ALIASES_CHARS:
                break
            acc.append(p)
        aliases = ", ".join(acc)
        if acc and len(acc) < len(parts):
            aliases += ", …"
    return aliases, _use_when(fm, body, slug)


def _fmt_entry(slug: str, aliases: str, read_when: str, files_summary: str = "") -> str:
    head = f"- **{slug}**"
    if aliases:
        head += f" (aka {aliases})"
    if read_when:
        head += f" — {read_when}"
    if files_summary:
        head += files_summary
    return head


def _render_toc_entries(scored: list) -> str:
    """Render the capped per-turn matched-topic index. `scored` is the same
    (score, idx) list built in main(), already sorted by score desc; only
    strong matches (score >= 100) become entries."""
    prepped: list[tuple[str, str, str, str]] = []   # (slug, aliases, read_when, files_summary)
    for s, idx in scored:
        if s < 100:
            continue
        slug = idx["slug"]
        aliases, uw = _toc_entry_fields(slug, idx["text"])
        if len(uw) > TOC_READ_WHEN_PER:
            uw = uw[:TOC_READ_WHEN_PER - 1].rstrip() + "…"
        prepped.append((slug, aliases, uw, idx.get("files_summary", "")))
    if not prepped:
        return ""

    included: list[tuple[str, str]] = []   # (slug, rendered_line)
    dropped: list[str] = []
    total = 0
    rw_sum = 0
    overflow = False
    for slug, aliases, uw, files_summary in prepped:
        if overflow:
            dropped.append(slug)
            continue
        # 400-char aggregate "read when" cap: keep the entry, drop only its
        # note, so a known-relevant topic never disappears just because the
        # description budget filled.
        uw_used = "" if (rw_sum + len(uw) > TOC_READ_WHEN_TOTAL) else uw
        line = _fmt_entry(slug, aliases, uw_used, files_summary)
        if total + len(line) + 1 > TOC_TOTAL_CHARS:
            overflow = True
            dropped.append(slug)
            continue
        included.append((slug, line))
        total += len(line) + 1
        rw_sum += len(uw_used)

    lines = [ln for _, ln in included]
    if dropped:
        # Make room for the "what got cut" pointer within the 1000 budget by
        # evicting the lowest-ranked included entries until it fits. Evicting
        # an entry frees far more than its slug adds to the pointer, so this
        # converges.
        def _ptr(slugs: list[str]) -> str:
            return "Other matched topics (use search_knowledge if relevant): " + ", ".join(slugs)
        while included and total + len(_ptr(dropped)) > TOC_TOTAL_CHARS:
            slug, line = included.pop()
            total -= len(line) + 1
            dropped.insert(0, slug)
        lines = [ln for _, ln in included]
        ptr = _ptr(dropped)
        if len(ptr) > TOC_TOTAL_CHARS:          # pathological: too many drops
            ptr = ptr[:TOC_TOTAL_CHARS - 1] + "…"
        lines.append(ptr)
    return "\n".join(lines)


def _main_toc(scored: list, is_first: bool) -> int:
    """toc-mode emit: constant recipe (every turn, cache-stable) + the capped
    matched-topic index when anything matched."""
    entries = _render_toc_entries(scored)
    body = _TOC_RECIPE + ("\n\n" + entries if entries else "")
    json.dump({"context": body}, sys.stdout)
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # pre_llm_call kwargs (user_message, is_first_turn, platform, …) arrive
    # under "extra"; only tool_name/tool_input/session_id/cwd are top-level.
    extra = payload.get("extra") or {}
    msg = str(extra.get("user_message") or payload.get("user_message") or "")
    is_first = bool(extra.get("is_first_turn") or payload.get("is_first_turn"))
    if not KNOW.is_dir():
        return 0

    platform_raw = extra.get("platform") or payload.get("platform") or ""
    mode = _km.read_mode(platform_raw) if _km else "full"
    if mode == "off":
        return 0        # toggle off: no TOC, no bodies, nothing injected

    # Shared scoring pass — both toc and full modes rank the same matches.
    msg_tokens = _tokens(msg) if msg.strip() else set()
    scored = []
    if msg_tokens:
        for slug, text, files_dir in _topics():
            idx = _identity(slug, text)
            idx["text"] = text
            idx["manifest"] = _manifest(files_dir)   # kept separate so clipping never drops it
            idx["files_summary"] = _kt.manifest_summary(files_dir)  # compact, for toc mode
            s = _score(idx, msg_tokens)
            if s > 0:
                scored.append((s, idx))
        scored.sort(key=lambda t: (-t[0], t[1]["slug"]))

    if mode == "toc":
        try:
            return _main_toc(scored, is_first)
        except Exception:
            # Fail safe on a live hook: inject nothing rather than erroring.
            return 0

    # ---- mode == "full": legacy whole-file injection (unchanged) ----
    toc = _toc() if is_first else ""

    def _emit(body: str) -> int:
        pieces = [p for p in (toc, body) if p]
        if not pieces:
            return 0
        json.dump({"context": "\n\n---\n\n".join(pieces)}, sys.stdout)
        return 0

    if not msg_tokens:
        # No identity tokens to match on, but still surface the TOC on turn 1.
        return _emit("")
    if not scored:
        return _emit("")
    parts: list[str] = []
    used = 0
    strong_count = 0
    weak_count = 0
    for s, idx in scored:
        is_strong = (s >= 100)
        if is_strong:
            if strong_count >= MAX_STRONG_FILES:
                continue
        else:
            if weak_count >= MAX_WEAK_FILES:
                continue

        per_file_chars = STRONG_PER_FILE_CHARS if is_strong else WEAK_PER_FILE_CHARS
        manifest = idx.get("manifest", "")
        body_budget = max(per_file_chars - len(manifest), 1000)
        chunk = _clip(idx["text"], body_budget) + manifest
        if used + len(chunk) > TOTAL_CHARS:
            break
        parts.append(chunk)
        used += len(chunk)
        if is_strong:
            strong_count += 1
        else:
            weak_count += 1
    if not parts:
        return _emit("")

    context = (
        "[SYSTEM-INJECTED, NOT WRITTEN BY THE USER — appended below the user's actual "
        "message by an automated hook, based on identity matches found in that message's "
        "text. The user did not type this block and has not seen it.]\n"
        "# Relevant knowledge (auto-loaded by file identity)\n"
        "CRITICAL FOR HERMES AGENT: Do NOT call 'search_knowledge' or 'session_search' for the topics below. "
        "They have ALREADY been auto-injected into your context. Prioritize using this information directly "
        "to answer the user.\n\n"
        + "\n\n---\n\n".join(parts)
    )
    return _emit(context)


if __name__ == "__main__":
    raise SystemExit(main())
