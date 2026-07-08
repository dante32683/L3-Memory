"""knowledge-tools — knowledge-base + skill read/search + explicit attachment filing.

Exposes two tools:

* ``save_to_knowledge`` — user-invoked filing of an attachment (unchanged).
* ``search_knowledge`` — recall + granular read over BOTH the L3 knowledge
  files AND the skill library (pipeline Phase 6). The deterministic
  ``knowledge-inject`` hook only auto-loads a knowledge file when the message
  contains a strong *identity* token (slug/alias/heading), and skills are only
  discoverable via the always-on system-prompt index; when the user's phrasing
  misses (e.g. "my off-road car thing" instead of "baja"), nothing injects and
  the model is blind. This tool lets the model pull context itself, at the
  granularity it needs:

    - ``mode="search"`` (default): the cheap "what exists" step. Runs the same
      identity index as the hook (strong slug/alias match, still the dominant
      signal) across knowledge AND skills, but the body-text relevance tail
      is BM25 (via ``bin/search_index.py``'s persistent FTS5 index) instead of
      hand-counted word/substring overlap — knowledge-retrieval-upgrade.md
      Phase B, validated against plans/knowledge-search-eval-set.md before
      shipping (real usage queries, not synthetic ones). Returns CANDIDATES
      only — slug, kind, score, one-line summary, size, days since the file
      was last modified (``last_updated_days_ago`` — cheap gap/staleness
      signal, knowledge-retrieval-upgrade.md Phase D, no LLM call), and (for
      larger files) a heading outline. Tiny files are inlined in full so a
      lookup that lands on a small topic still one-shots.
    - ``mode="outline"`` / ``mode="section"`` / ``mode="full"``: targeted reads
      of one ``slug`` — heading tree, one named section, or the whole file.
      This is the fix for the old "always dump whole files" behaviour: the
      model previews structure cheaply, then pulls exactly the section it needs
      (``full`` remains the always-available, lossless escape hatch).

Deliberately NOT a gateway hook: nothing is auto-saved. Filing happens only on
explicit request, which is exactly the agent calling this tool.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# search-mode tuning (tunable constants, not magic numbers)
MAX_CANDIDATES = 6          # top-N results returned by mode=search
SMALL_FILE_CHARS = 1200     # a matched file this small is inlined whole in search
OUTLINE_IN_SEARCH_CHARS = 1200  # larger matches get a heading outline instead of body


def _hermes_home() -> Path:
    # Look for L3_HOME first to decouple it, falling back to HERMES_HOME
    return Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))


def _md_sections():
    """Lazy-load bin/md_sections.py (shared heading splitter) for the granular
    read modes. Same sys.path-insert precedent as the curator importing it."""
    bin_dir = _hermes_home() / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    import md_sections  # noqa: E402
    return md_sections


def _kt():
    """Lazy-load bin/knowledge_topics.py — single source of truth for topic
    layout (every topic is a folder <slug>/<slug>.md + <slug>/files/)."""
    bin_dir = _hermes_home() / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    import knowledge_topics  # noqa: E402
    return knowledge_topics


def _search_index():
    """Lazy-load bin/search_index.py — persistent FTS5/BM25 corpus index
    (Phase B of knowledge-retrieval-upgrade.md), used to rank body-text
    relevance instead of the old hand-counted word/substring overlap."""
    bin_dir = _hermes_home() / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    import search_index  # noqa: E402
    return search_index


def _skill_rows_for_index():
    """(name, description, full_text) for every skill — feeds search_index's
    corpus build. Thin wrapper so search_index.py doesn't duplicate the skill
    glob/summary logic this module already owns."""
    for name, text in _skills():
        yield name, _skill_summary(text), text


# --- shared index from the inject hook (DRY: one source of truth for identity) --
_INJECT = None


def _inject_mod():
    """Lazy-load hooks/knowledge-inject.py as a module so search_knowledge reuses
    the exact same _topics()/_identity()/_parse_frontmatter() the gateway hook
    uses. Hyphenated filename → load by path via importlib."""
    global _INJECT
    if _INJECT is None:
        path = _hermes_home() / "hooks" / "knowledge-inject.py"
        spec = importlib.util.spec_from_file_location("_knowledge_inject", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _INJECT = mod
    return _INJECT


# Recall-mode tokenizer: like the hook's _tokens but WITHOUT the _GENERIC drop,
# so a query like "list my tools" can still match a tools file. Keeps the same
# stopword drop and stemmer for consistency.
def _recall_tokens(text: str) -> set[str]:
    inj = _inject_mod()
    out: set[str] = set()
    for w in re.findall(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+", text.lower()):
        norm = w.replace("_", "-")
        if len(norm) >= 3:
            out.add(norm)
    for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()):
        if w in inj._STOP:
            continue
        out.add(inj._stem(w))
    return out


# --- skill index (Phase 6: skills become searchable, they had no search tool) --
def _skills():
    """Yield (name, text) for every skill's root SKILL.md. glob one level under
    skills/ (the canonical layout skills/<name>/SKILL.md); rglob catches any
    nested collections too, keyed by the immediate parent dir name."""
    root = _hermes_home() / "skills"
    if not root.is_dir():
        return
    seen: set[str] = set()
    for skill_md in sorted(root.rglob("SKILL.md")):
        name = skill_md.parent.name
        if name in seen:
            continue
        seen.add(name)
        try:
            yield name, skill_md.read_text(encoding="utf-8")
        except Exception:
            continue


def _skill_summary(text: str) -> str:
    """One-line skill summary: the frontmatter `description:` (first line,
    de-quoted), else the first H1."""
    m = re.search(r'^description:\s*"?(.+?)"?\s*$', text, re.M)
    if m:
        return m.group(1).strip().strip('"')
    h = re.search(r"^#\s+(.+)$", text, re.M)
    return h.group(1).strip() if h else ""


def _bm25_component(bm25_scores: dict[tuple[str, str], float], kind: str, slug: str) -> float:
    """bm25() returns <= 0 (more negative = better match); negate so higher is
    better, consistent with every other term in the linear score below.
    0.0 (not "no match") when the slug had no lexical hit at all — this is
    only ever added on top of strong/weak identity hits, which have their own
    inclusion check, so a true zero here never wrongly excludes a file."""
    bm = bm25_scores.get((kind, slug))
    return -bm if bm is not None else 0.0


def _score_knowledge(qtoks: set[str], bm25_scores: dict[tuple[str, str], float]):
    inj = _inject_mod()
    scored = []
    for slug, text, _files_dir in inj._topics():
        idx = inj._identity(slug, text)
        strong_hits = len(idx["strong"] & qtoks)
        weak_hits = len(idx["weak"] & qtoks)
        bm = bm25_scores.get(("knowledge", slug))
        score = 1000 * strong_hits + 50 * weak_hits + _bm25_component(bm25_scores, "knowledge", slug)
        if strong_hits or weak_hits or bm is not None:
            scored.append((score, "knowledge", slug, text))
    return scored


def _score_skills(qtoks: set[str], bm25_scores: dict[tuple[str, str], float]):
    scored = []
    for name, text in _skills():
        inj = _inject_mod()
        name_toks = {
            st for w in re.split(r"[^a-z0-9]+", name.lower())
            if len(w) >= 3 and (st := inj._stem(w)) not in inj._GENERIC
        }
        desc_toks = _recall_tokens(_skill_summary(text))
        name_hits = len(name_toks & qtoks)
        desc_hits = len(desc_toks & qtoks)
        bm = bm25_scores.get(("skill", name))
        score = 1000 * name_hits + 40 * desc_hits + _bm25_component(bm25_scores, "skill", name)
        if name_hits or desc_hits or bm is not None:
            scored.append((score, "skill", name, text))
    return scored


SEARCH_SCHEMA = {
    "name": "search_knowledge",
    "description": (
        "Search AND read the user's personal knowledge base (saved facts about people, "
        "projects, accounts, tools, classes, preferences, how things are set up) and "
        "skill library (procedures / how-tos). Relevant knowledge is normally "
        "auto-loaded, but only when your phrasing names a topic; CALL THIS TOOL whenever "
        "a request refers to a specific person, project, account, class, skill, or system "
        "you do NOT already have context on — especially if the user assumes you know "
        "something you don't.\n"
        "Two steps:\n"
        "1. DISCOVER — call with `query` and default `mode='search'`. Returns matching "
        "candidates (slug, kind, one-line summary, size, and a heading outline for larger "
        "files); tiny files come back in full. Returns no match → the topic genuinely "
        "isn't there; say so rather than guessing.\n"
        "2. READ — call with `slug` (from step 1) and `mode='section'` (+ `section` name) "
        "to pull just what you need, `mode='outline'` to see a file's headings, or "
        "`mode='full'` to load the whole file. Prefer `section` for big files; use `full` "
        "when unsure. Set `kind` to narrow to 'knowledge' or 'skill' (default searches both)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "For mode='search': what to look up — a topic/person/project/skill name or a natural-language phrase, e.g. 'baja team roles', 'how to edit a docx', 'off-road car project'.",
            },
            "mode": {
                "type": "string",
                "enum": ["search", "outline", "section", "full"],
                "description": "search = find candidates (default). outline/section/full = read a specific `slug`.",
            },
            "kind": {
                "type": "string",
                "enum": ["knowledge", "skill", "both"],
                "description": "Restrict to knowledge facts or skill procedures. Default 'both'.",
            },
            "slug": {
                "type": "string",
                "description": "For mode=outline/section/full: the topic slug or skill name to read (as returned by a mode='search' call).",
            },
            "section": {
                "type": "string",
                "description": "For mode='section': the heading name to pull (case-insensitive; partial match allowed).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _staleness_days(kind: str, slug: str) -> int | None:
    """Days since a search result's source file last changed on disk."""
    _rkind, path = _resolve_read(slug, kind)
    if not path or not path.exists():
        return None
    return max(0, int((time.time() - path.stat().st_mtime) // 86400))


def _do_search(query: str, kind: str) -> str:
    if not query:
        return json.dumps({"success": False, "error": "mode='search' needs a `query`."})
    qtoks = _recall_tokens(query)
    si = _search_index()
    conn = si.get_conn(_kt(), _skill_rows_for_index)
    try:
        bm25_scores = {(k, slug): score for slug, k, score in si.search(conn, qtoks, kind)}
    finally:
        conn.close()
    scored = []
    if kind in ("knowledge", "both"):
        scored += _score_knowledge(qtoks, bm25_scores)
    if kind in ("skill", "both"):
        scored += _score_skills(qtoks, bm25_scores)
    if not scored:
        return json.dumps({
            "success": True, "matches": 0,
            "message": f"Nothing in the knowledge base or skills matches '{query}'.",
        })
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    top = scored[:MAX_CANDIDATES]
    md = _md_sections()
    results = []
    for _s, k, slug, text in top:
        body = text.strip()
        if k == "skill":
            summary = _skill_summary(text)
        else:
            fm, kbody = _inject_mod()._parse_frontmatter(text)
            summary = _inject_mod()._use_when(fm, kbody, slug)
        entry = {"slug": slug, "kind": k, "score": _s,
                 "summary": summary[:160], "chars": len(body)}
        age_days = _staleness_days(k, slug)
        if age_days is not None:
            entry["last_updated_days_ago"] = age_days
        if len(body) <= SMALL_FILE_CHARS:
            entry["content"] = body
        elif len(body) > OUTLINE_IN_SEARCH_CHARS:
            entry["outline"] = md.outline(body)
        results.append(entry)
    return json.dumps({
        "success": True,
        "matches": len(results),
        "note": "Candidates only. Read one with mode='section'/'full' and its `slug`.",
        "results": results,
    })


def _resolve_read(slug: str, kind: str):
    home = _hermes_home()
    k_body = _kt().topic_path(slug)
    s_md = home / "skills" / slug / "SKILL.md"

    def _skill_path():
        if s_md.exists():
            return s_md
        return next((p for p in (home / "skills").rglob("SKILL.md")
                     if p.parent.name == slug), None)

    if kind == "skill":
        p = _skill_path()
        return ("skill", p) if p else (None, None)
    if kind == "knowledge":
        return ("knowledge", k_body) if k_body.exists() else (None, None)
    if k_body.exists():
        return ("knowledge", k_body)
    p = _skill_path()
    return ("skill", p) if p else (None, None)


def _do_read(slug: str, kind: str, mode: str, section: str) -> str:
    if not slug:
        return json.dumps({"success": False, "error": f"mode='{mode}' needs a `slug`."})
    rkind, path = _resolve_read(slug, kind)
    if not path:
        where = "knowledge or skill" if kind == "both" else kind
        return json.dumps({"success": False,
                           "error": f"No {where} file for slug '{slug}'. "
                                    "Run mode='search' first to get a valid slug."})
    text = path.read_text(encoding="utf-8")
    try:
        rel = str(path.relative_to(_hermes_home()))
    except ValueError:
        rel = str(path)
    manifest = _kt().manifest(_kt().files_dir(slug)) if rkind == "knowledge" else ""

    if mode == "full":
        return json.dumps({"success": True, "slug": slug, "kind": rkind,
                           "path": rel, "content": (text.strip() + manifest) if manifest else text.strip()})
    md = _md_sections()
    if mode == "outline":
        result = {"success": True, "slug": slug, "kind": rkind, "path": rel,
                  "outline": md.outline(text),
                  "note": "Pull one with mode='section' + its heading name, or mode='full'."}
        if manifest:
            result["files"] = manifest.strip()
        return json.dumps(result)
    if not section:
        return json.dumps({"success": False,
                           "error": "mode='section' needs a `section` heading name.",
                           "available_sections": md.section_titles(text)})
    sec = md.get_section(text, section)
    if sec is None:
        return json.dumps({"success": False,
                           "error": f"No unique section matching '{section}' in {slug}.",
                           "available_sections": md.section_titles(text)})
    return json.dumps({"success": True, "slug": slug, "kind": rkind, "path": rel,
                       "section": section, "content": sec})


def handle_search_knowledge(args: Dict[str, Any], **_kw) -> str:
    mode = str(args.get("mode") or "search").strip().lower()
    kind = str(args.get("kind") or "both").strip().lower()
    if kind not in ("knowledge", "skill", "both"):
        kind = "both"
    if mode in ("outline", "section", "full"):
        return _do_read(str(args.get("slug") or "").strip(), kind, mode,
                        str(args.get("section") or "").strip())
    if mode != "search":
        return json.dumps({"success": False,
                           "error": f"Unknown mode '{mode}'. Use search|outline|section|full."})
    return _do_search(str(args.get("query") or "").strip(), kind)


SAVE_SCHEMA = {
    "name": "save_to_knowledge",
    "description": (
        "Save an attached file into the user's personal knowledge base. "
        "Call this ONLY when the user explicitly asks to save/file a document "
        "(e.g. 'save this textbook to knowledge', 'file this under my physics class'). "
        "Do NOT call it for files the user merely sent without asking to save. "
        "Pass `path` = the file path from the '[The user sent a document: ... It is "
        "saved at: <path>]' context note. Optionally pass `topic_hint` to steer which "
        "topic/class/project it belongs to. The right topic folder is chosen (and "
        "created if needed) automatically; the file is copied into that topic's files/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the attached file to save (from the document context note).",
            },
            "topic_hint": {
                "type": "string",
                "description": "Optional hint for which topic/class/project this belongs to, e.g. 'physics 40 textbook'. Combined with the user's wording. If omitted, the filename + recent message is used.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def handle_save_to_knowledge(args: Dict[str, Any], **_kw) -> str:
    path = str(args.get("path") or "").strip()
    hint = str(args.get("topic_hint") or "").strip()
    if not path:
        return json.dumps({"success": False, "error": "No file path given."})
    if not os.path.exists(path):
        return json.dumps({"success": False, "error": f"No file at {path}."})

    router = _hermes_home() / "bin" / "artifact-route"
    if not router.exists():
        return json.dumps({"success": False, "error": "artifact-route not installed."})

    try:
        proc = subprocess.run(
            ["python3", str(router), "--file", path, "--caption", hint],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "HERMES_HOME": str(_hermes_home())},
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Filing timed out."})

    if proc.returncode != 0:
        return json.dumps({"success": False, "error": (proc.stderr or "routing failed").strip()[:300]})
    try:
        res = json.loads(proc.stdout.strip())
    except Exception:
        return json.dumps({"success": False, "error": "Unexpected router output.", "raw": proc.stdout[:300]})

    slug = res.get("slug", "?")
    dest = res.get("filed", "")
    kind = res.get("kind", "")
    where = "a new topic" if kind == "new" else "the existing topic"
    return json.dumps({
        "success": True,
        "message": f"Filed into {where} '{slug}'. Saved at {dest}.",
        "slug": slug, "path": dest,
    })


_TOOLS = (
    ("save_to_knowledge", SAVE_SCHEMA, handle_save_to_knowledge, "📎"),
    ("search_knowledge", SEARCH_SCHEMA, handle_search_knowledge, "🔎"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="knowledge-tools",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )
    logger.info("knowledge-tools: registered save_to_knowledge, search_knowledge")
