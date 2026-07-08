#!/usr/bin/env python3
"""md_sections — deterministic markdown heading/section splitting (pure stdlib).

Shared utility. The runtime granular-read modes of ``search_knowledge``
(knowledge/skill pipeline Phase 6: ``outline`` / ``section``) and any future
section-level overlap detector (Phase 3b's embedding candidate generator) need
the SAME heading-based splitter, so it lives here once and both import it rather
than duplicating the logic (see the plan's "shared section-splitter" note).

A heading is an ATX line (``#``..``######`` followed by space + text). A
"section" is a heading plus every line after it up to — but not including — the
next heading of the SAME OR HIGHER level, so a section naturally contains its
own sub-sections. Content before the first heading is the preamble (level 0,
title "").

Fence-aware: a ``#`` inside a ``` or ~~~ fenced code block is never a heading.
"""
from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def headings(body: str) -> list[tuple[int, str, int]]:
    """Every heading as (level, title, line_index), fence-aware, in order."""
    out: list[tuple[int, str, int]] = []
    fence: str | None = None
    for i, line in enumerate(body.splitlines()):
        fm = _FENCE_RE.match(line)
        if fm:
            tok = fm.group(1)[0]
            if fence is None:
                fence = tok
            elif line.lstrip().startswith(fence * 3):
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), i))
    return out


def outline(body: str, snippet_chars: int = 90) -> str:
    """Indented heading tree, one line per heading, each annotated with the
    section's own size (chars, excluding sub-sections) and the first content
    line under it — enough for a caller to decide which section to pull next
    without loading the whole file."""
    lines = body.splitlines()
    hs = headings(body)
    if not hs:
        return "(no headings — small/flat file; read with mode=full)"
    out: list[str] = []
    for idx, (level, title, ln) in enumerate(hs):
        nxt = hs[idx + 1][2] if idx + 1 < len(hs) else len(lines)
        own_lines = lines[ln + 1:nxt]
        own_chars = sum(len(x) + 1 for x in own_lines)
        snippet = ""
        for x in own_lines:
            s = x.strip()
            if s and not s.startswith("#"):
                snippet = s[:snippet_chars] + ("…" if len(s) > snippet_chars else "")
                break
        indent = "  " * (level - 1)
        note = f"  ·  {own_chars}c" if own_chars else ""
        line = f"{indent}- {title}{note}"
        if snippet:
            line += f"\n{indent}  ↳ {snippet}"
        out.append(line)
    return "\n".join(out)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def section_titles(body: str) -> list[str]:
    return [t for _lvl, t, _ln in headings(body)]


def get_section(body: str, name: str) -> str | None:
    """Return the heading matching ``name`` plus its content, up to the next
    heading of the same or higher level (so sub-sections are included). Matching
    is case/punctuation-insensitive: exact title first, then unique substring.
    None if no match or an ambiguous substring (caller should list titles)."""
    hs = headings(body)
    if not hs:
        return None
    lines = body.splitlines()
    want = _norm(name)

    exact = [i for i, (_l, t, _n) in enumerate(hs) if _norm(t) == want]
    partial = [i for i, (_l, t, _n) in enumerate(hs) if want and want in _norm(t)]
    hits = exact or partial
    if len(hits) != 1:
        return None

    idx = hits[0]
    level, _title, start = hs[idx]
    end = len(lines)
    for level2, _t2, ln2 in hs[idx + 1:]:
        if level2 <= level:
            end = ln2
            break
    return "\n".join(lines[start:end]).rstrip("\n")
