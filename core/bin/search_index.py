#!/usr/bin/env python3
"""search_index — persistent FTS5 BM25 index over knowledge + skills (pure
stdlib, no new dependency: SQLite's FTS5 bm25() ranking function is already
compiled into this instance's sqlite3 build).

Used by ``plugins/knowledge-tools`` (``search_knowledge``'s mode='search')
to rank body-text relevance. It does NOT replace the strong-identity-token
match (slug/alias hit) that already dominates ranking -- that stays the
primary signal; BM25 only replaces the crude
``5*body_word_overlap + 2*substring_hit`` tail the linear scorer used to
compute by hand, and breaks ties among equal strong-hit counts (see
plans/knowledge-retrieval-upgrade.md Phase B for why: a hard "strong matches
always outrank everything, ties broken by slug name" partition was tried
first and regressed queries where an incidental generic alias collision put
the wrong file first -- BM25 as the tiebreaker fixed it without any new
regressions on the real-usage eval set).

Rebuild-on-demand, not live-maintained: the corpus is small (~360 files
today) and already gets touched nightly by knowledge-curate, so the index is
rebuilt from scratch whenever any source file's mtime is newer than the
index's own mtime (single stat-based staleness check) rather than
maintaining incremental writes. Callers only pay the rebuild cost the first
call after something changed.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Iterator

# Column weights for bm25(): title and aliases matches count for more than a
# body-text mention -- these were tuned empirically against the real-usage
# eval set (plans/knowledge-search-eval-set.md), not guessed. NOTE: bm25()
# weight args map ONLY to non-UNINDEXED columns in declared order (title,
# aliases, body -- 3 args, not 5) -- passing the wrong count silently
# misaligns weights against columns instead of erroring; verified by hand
# before relying on it.
TITLE_WEIGHT = 3.0
ALIAS_WEIGHT = 3.0
BODY_WEIGHT = 1.0


def hermes_home() -> Path:
    # Look for L3_HOME first to decouple it, falling back to HERMES_HOME
    return Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))


def index_path() -> Path:
    return hermes_home() / "knowledge" / ".system" / "search-index.db"


def _source_mtimes(kt_mod) -> Iterator[float]:
    """mtime of every file that should count toward staleness: every live
    knowledge topic body (kt_mod.iter_topics() already skips .archive/.system/
    INBOX.md) plus every skill's SKILL.md."""
    home = hermes_home()
    for slug, _text, _files in kt_mod.iter_topics():
        p = kt_mod.topic_path(slug)
        if p.exists():
            yield p.stat().st_mtime
    skills_root = home / "skills"
    if skills_root.is_dir():
        for p in skills_root.rglob("SKILL.md"):
            yield p.stat().st_mtime


def _needs_rebuild(db_path: Path, kt_mod) -> bool:
    if not db_path.exists():
        return True
    db_mtime = db_path.stat().st_mtime
    return any(m > db_mtime for m in _source_mtimes(kt_mod))


def _build(db_path: Path, kt_mod, skill_rows: Iterable[tuple[str, str, str]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE corpus USING fts5("
            "slug UNINDEXED, kind UNINDEXED, title, aliases, body, "
            "tokenize='porter unicode61')"
        )
        rows = []
        for slug, text, _files_dir in kt_mod.iter_topics():
            _fm, body = kt_mod.split_frontmatter(text)
            meta = kt_mod.topic_meta(slug, text)
            rows.append((slug, "knowledge", meta["title"], meta["aliases"], body))
        for name, desc, text in skill_rows:
            rows.append((name, "skill", name, desc, text))
        conn.executemany(
            "INSERT INTO corpus (slug, kind, title, aliases, body) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    tmp_path.replace(db_path)  # atomic swap -- readers never see a partial build


def get_conn(kt_mod, skill_rows_fn: Callable[[], Iterable[tuple[str, str, str]]]) -> sqlite3.Connection:
    """Live connection to the index, rebuilding first if stale.

    kt_mod: the caller's already-loaded bin/knowledge_topics module (avoids a
    second import path).
    skill_rows_fn: zero-arg callable yielding (name, description, full_text)
    for every skill -- knowledge-tools already owns the skill glob + summary
    extraction (``_skills()``/``_skill_summary()``), so this module doesn't
    duplicate it.
    """
    db_path = index_path()
    if _needs_rebuild(db_path, kt_mod):
        _build(db_path, kt_mod, skill_rows_fn())
    return sqlite3.connect(str(db_path))


def search(conn: sqlite3.Connection, qtoks: set[str], kind: str = "both") -> list[tuple[str, str, float]]:
    """[(slug, kind, bm25_score), ...] -- bm25_score is raw SQLite FTS5 output
    (<= 0, more negative = better match); caller combines with the
    strong-identity-token score, does not use this as a standalone ranking."""
    toks = [t for t in qtoks if t]
    if not toks:
        return []
    match = " OR ".join(toks)
    kind_filter = ""
    if kind == "knowledge":
        kind_filter = "AND kind = 'knowledge'"
    elif kind == "skill":
        kind_filter = "AND kind = 'skill'"
    sql = (
        f"SELECT slug, kind, bm25(corpus, {TITLE_WEIGHT}, {ALIAS_WEIGHT}, {BODY_WEIGHT}) AS score "
        f"FROM corpus WHERE corpus MATCH ? {kind_filter} ORDER BY score"
    )
    try:
        cur = conn.execute(sql, (match,))
    except sqlite3.OperationalError:
        return []
    return cur.fetchall()
