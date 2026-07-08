#!/usr/bin/env python3
"""knowledge_topics — single source of truth for knowledge-topic layout (pure stdlib).

Every knowledge topic is a folder: ``knowledge/<slug>/<slug>.md`` (the body)
plus ``knowledge/<slug>/files/`` (attachments, always present). There is no
flat-file form and no "promotion" event — this replaces three independent
flat-vs-folder implementations that used to live in
``hooks/knowledge-inject.py``, ``bin/artifact-route``, and
``bin/knowledge-curate``. One shape, one place that knows it.

``.archive/``, ``.system/``, and ``INBOX.md`` are not topics and are never
yielded by ``iter_topics()``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, Optional


def hermes_home() -> Path:
    # Look for L3_HOME first to decouple it, falling back to HERMES_HOME
    return Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))


def knowledge_root() -> Path:
    return hermes_home() / "knowledge"


def topic_path(slug: str, know: Optional[Path] = None) -> Path:
    """The body file for a topic — always ``knowledge/<slug>/<slug>.md``,
    regardless of whether it exists yet (callers create it on first write)."""
    know = know or knowledge_root()
    return know / slug / f"{slug}.md"


def files_dir(slug: str, know: Optional[Path] = None) -> Path:
    know = know or knowledge_root()
    return know / slug / "files"


def iter_topics(know: Optional[Path] = None) -> Iterator[tuple[str, str, Path]]:
    """Yield (slug, body_text, files_dir) for every live topic folder.
    files_dir is always returned (may be empty or not yet created — callers
    should check .is_dir() before use)."""
    know = know or knowledge_root()
    if not know.is_dir():
        return
    for d in sorted(know.glob("*/")):
        if d.name.startswith("."):
            continue
        body = d / f"{d.name}.md"
        if not body.exists():
            continue
        try:
            yield d.name, body.read_text(encoding="utf-8"), d / "files"
        except Exception:
            continue


def manifest(files: Optional[Path]) -> str:
    """A '## 📎 Attached files' block listing every file under a topic's
    files/ dir, or '' if there are none. Moved from the old
    knowledge-inject.py _manifest() — logic unchanged."""
    if not files or not files.is_dir():
        return ""
    names = sorted(
        str(p.relative_to(files)) for p in files.rglob("*") if p.is_file()
    )
    if not names:
        return ""
    listing = "\n".join(f"- `{files}/{n}`" for n in names)
    return "\n\n## 📎 Attached files (read on demand with read_file)\n" + listing


def manifest_summary(files: Optional[Path], max_names: int = 2) -> str:
    """Compact one-line summary for space-constrained callers (TOC entries):
    '(N files: a.pdf, b.pdf, +K more)' or '' if there are none."""
    if not files or not files.is_dir():
        return ""
    names = sorted(p.name for p in files.rglob("*") if p.is_file())
    if not names:
        return ""
    shown = ", ".join(names[:max_names])
    rest = len(names) - max_names
    more = f", +{rest} more" if rest > 0 else ""
    return f" ({len(names)} files: {shown}{more})"


_SLUG_RE = re.compile(r"[^a-z0-9-]")


def slugify(s: str) -> str:
    return _SLUG_RE.sub("", s.lower().strip().replace(" ", "-").replace("_", "-"))[:40] or "misc"


def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter_block_including_delimiters, body). '' frontmatter if none."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[:end + 4], text[end + 4:].lstrip("\n")
    return "", text


def topic_meta(slug: str, text: str) -> dict:
    fm, _ = split_frontmatter(text)
    title = re.search(r"^title:\s*(.+)$", fm, re.M)
    al = re.search(r"^aliases:\s*\[(.*?)\]", fm, re.M)
    return {
        "slug": slug,
        "title": title.group(1).strip() if title else slug,
        "aliases": al.group(1).strip() if al else "",
    }
