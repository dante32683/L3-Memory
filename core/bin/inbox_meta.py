"""Sidecar provenance records for knowledge/.system/INBOX.md.

INBOX.md stays human-readable and append-only. This module writes a parallel
JSONL stream keyed by block id and content hash so later curation/audit code can
tell where each pending fact came from without parsing extra Markdown syntax.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def line_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def block_id(timestamp: str, source: str, session_id: str) -> str:
    raw = f"{timestamp}|{source}|{session_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def append_records(
    inbox_path: Path,
    *,
    block_id: str,
    timestamp: str,
    source: str,
    source_class: str,
    trust: str,
    session_id: str,
    lines: Iterable[str],
    platform: str | None = None,
    max_lines: int | None = None,
    omitted_lines: int = 0,
) -> None:
    records = []
    for ordinal, text in enumerate(lines, 1):
        records.append({
            "schema": "inbox-meta-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "inbox": str(inbox_path),
            "block_id": block_id,
            "block_timestamp": timestamp,
            "source": source,
            "source_class": source_class,
            "trust": trust,
            "session_id": session_id,
            "platform": platform,
            "ordinal": ordinal,
            "line_hash": line_hash(text),
            "text_len": len(text),
            "max_lines": max_lines,
            "omitted_lines": omitted_lines,
        })
    if not records:
        return
    meta_path = inbox_path.with_name("INBOX.meta.jsonl")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with meta_path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
