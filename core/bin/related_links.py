#!/usr/bin/env python3
"""related_links — deterministic `## Related` wikilink-block handling (pure stdlib).

Shared by BOTH `bin/knowledge-curate` (protect the block across an LLM body
rewrite + populate/dedup links) and `bin/doc-drift-check` (Phase 3 stale-link
detection). One implementation so the writer and the checker can never drift.

KNOWLEDGE-TOPIC-ONLY: `## Related` links exist strictly between knowledge
topics. Skills never participate — every skill file is literally named
SKILL.md, and Obsidian's wikilink resolver only ever matches a link against a
real filename, never frontmatter `aliases:`, so a [[skill-name]] link can
never resolve in the vault. See [[hermes-knowledge]].

The `## Related` block is a STRUCTURALLY PROTECTED trailing section: the LAST
heading whose text starts with "Related" and everything after it. Contract:
  * Before any LLM rewrite of a file body, `split_related()` peels the block off
    so it is never sent to the model (the model cannot be trusted to reproduce a
    list it only saw in passing — see the knowledge-skill-pipeline plan, Phase 4).
  * After the model returns the rewritten body, `join_related()` re-attaches the
    ORIGINAL block verbatim.
  * Any intentional link change goes ONLY through `add_link()` / `remove_link()`,
    which edit that block mechanically (deduped) — never a body rewrite.

Wikilink form: `- [[slug]]` bullets under a `## Related` heading. `[[slug|alias]]`
display form is tolerated on read (target = the part before `|`).
"""
from __future__ import annotations

import re

# The LAST heading (any level) whose text is EXACTLY "Related" (or the legacy
# "Related topics") — nothing more. Everything from that heading to EOF is the
# protected block. The exact-match anchor is load-bearing: a heading like
# "## Related Services" (a real content section, e.g. in streaming.md) must NOT be
# mistaken for a cross-link block, or its content would be wrongly peeled off.
_HEADING = re.compile(r"^#{1,6}[ \t]+Related(?:[ \t]+topics)?[ \t]*$", re.I | re.M)
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

DEFAULT_HEADING = "## Related"


def split_related(body: str) -> tuple[str, str]:
    """Return (main_body, related_block). related_block includes its heading line
    and everything after it (the LAST `## Related` heading); '' if there is none.
    Trailing newlines are normalised off both parts."""
    matches = list(_HEADING.finditer(body))
    if not matches:
        return body.rstrip("\n"), ""
    last = matches[-1]
    return body[:last.start()].rstrip("\n"), body[last.start():].strip("\n")


def join_related(main_body: str, related_block: str) -> str:
    """Re-attach a related block to a body, with exactly one blank-line gap.
    Always returns a single trailing newline."""
    main = main_body.rstrip("\n")
    if not related_block.strip():
        return main + "\n"
    return main + "\n\n" + related_block.strip("\n") + "\n"


def related_targets(related_block: str) -> list[str]:
    """Every wikilink target (the part before any `|`) in reading order."""
    return [m.group(1).strip() for m in _WIKILINK.finditer(related_block)]


def _norm(slug: str) -> str:
    return slug.strip().lower()


def add_link(body: str, target: str, heading: str = DEFAULT_HEADING) -> tuple[str, bool]:
    """Append `- [[target]]` to body's Related block, creating the block if
    absent. Deduped case-insensitively against existing targets. No-op (returns
    the body unchanged, changed=False) if the link is already present or target
    is empty. Returns (new_body, changed)."""
    target = target.strip().lstrip("[").rstrip("]").strip()
    if not target:
        return body, False
    main, related = split_related(body)
    if _norm(target) in {_norm(t) for t in related_targets(related)}:
        return body, False
    related = (related or heading).rstrip("\n") + f"\n- [[{target}]]"
    return join_related(main, related), True


def remove_link(body: str, target: str) -> tuple[str, bool]:
    """Drop the `- [[target]]` bullet from body's Related block. If that empties
    the block of all links, the whole block (heading included) is removed. Returns
    (new_body, changed)."""
    main, related = split_related(body)
    if not related:
        return body, False
    kept, changed = [], False
    for line in related.splitlines():
        tgts = related_targets(line)
        if line.lstrip().startswith(("-", "*")) and tgts and _norm(tgts[0]) == _norm(target):
            changed = True
            continue
        kept.append(line)
    if not changed:
        return body, False
    rebuilt = "\n".join(kept)
    if not _WIKILINK.search(rebuilt):          # only the heading (or less) left → drop it
        return join_related(main, ""), True
    return join_related(main, rebuilt), True
