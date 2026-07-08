#!/usr/bin/env python3
"""Evaluate the installed L3/Hermes retrieval path against fixed regression cases.

This intentionally imports the live installed hook/plugin code instead of
reimplementing retrieval in the test. Use --hermes-home to point at a different
install tree.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path


EVAL = [
    ("hermes doc drift check drift apply cron hermes-knowledge hermes-crons", ["hermes-knowledge"]),
    ("telegram slash commands Hermes config commands setting", ["hermes-config-edit", "hermes-plugins"]),
    ("ref-provider-cost-analysis", ["ref-provider-cost-analysis"]),
    ("ethst-20 ethnic studies Canvas course assignments due Tuesday", ["ethst-20"]),
    ("hermes-knowledge knowledge curate pipeline", ["hermes-knowledge"]),
    ("cron script chmod executable scheduling tool no_agent scripts", ["hermes-crons"]),
    ("ref-provider-cost-analysis opencode-go", ["ref-provider-cost-analysis"]),
    ("hermes-crons gmail triage noon", ["hermes-crons"]),
    ("hermes cron no_agent gmail script cron scripts", ["hermes-crons", "dante-google-account"]),
    ("Python C# Revit Rhino Grasshopper ANSYS learning portfolio skills", ["portfolio"]),
    ("python-style phys-40 lab code style", ["python-style"]),
    ("hermes knowledge archive curator files folders stale repeated obsolete", ["hermes-knowledge", "hermes-backup"]),
    ("ethst-20 due today reading ethnic studies", ["ethst-20"]),
    ("Baldur's Gate 3 BG3 build character", ["gaming"]),
    ("samba v2ray gateway", ["school-proxy-transport"]),
    ("samba v2ray gateway firewall docker", ["school-proxy-transport"]),
    ("v2ray samba gateway network configuration", ["school-proxy-transport"]),
    ("deepseek provider rates pricing", ["ref-provider-rates"]),
    ("qwen reasoning levels opencode API", ["ref-reasoning-params"]),
    ("openai-codex gpt-5.5 pricing subscription tokens cost", ["ref-provider-cost-analysis"]),
    ("hermes-gate", ["hermes-gate"]),
    ("phys 40 lab 2.3 projectile motion", ["phys-40"]),
    ("file naming conventions", ["drive-office-folder"]),
    ("drive-office-folder microsoft-office docx lock sync", ["drive-office-folder"]),
    ("office-document-styles lab report template physics", ["office-document-styles"]),
    ("hermes-documents docx equation inline values units", ["hermes-documents"]),
    ("google-drive-structure artifacts folder", ["google-drive-structure"]),
    ("ref-canvas-tools", ["ref-canvas-tools"]),
    ("google-oauth-watchdog cron job", ["hermes-crons"]),
    ("dante google account OAuth calendar credentials raw API", ["dante-google-account"]),
    ("hermes-backup skills backup restore", ["hermes-backup"]),
    ("python uncertainties pylance physics lab python learned recently", ["python-uncertainties-pylance"]),
    ("calendars Google calendar API raw event location conference location marker", ["calendars"]),
    ("civil software learning long term goals tasks Baja SAE Canvas SRJC", ["civil-software-learning"]),
    ("baja-sae ANSYS Miles simulations subteam", ["baja-sae"]),
    ("Lenovo Slim 7i Aura desktop RTX 4070 Parsec dantepc", ["civil-software-learning"]),
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def search_rank(ktools, query: str, expected: list[str]) -> tuple[int | None, list[str]]:
    out = json.loads(ktools._do_search(query, "both"))
    results = out.get("results") or out.get("candidates") or []
    slugs = [r.get("slug") for r in results]
    for i, slug in enumerate(slugs, 1):
        if slug in expected:
            return i, slugs
    return None, slugs


def toc_status(ki, topics, query: str, expected: list[str]) -> tuple[str, int | None]:
    toks = ki._tokens(query)
    scored = []
    for _slug, idx in topics:
        score = ki._score(idx, toks)
        if score > 0:
            scored.append((score, idx))
    scored.sort(key=lambda t: (-t[0], t[1]["slug"]))
    entries = ki._render_toc_entries(scored)
    shown = [line.split("**")[1] for line in entries.splitlines() if line.startswith("- **")]
    dropped = [
        item.strip()
        for line in entries.splitlines()
        if line.startswith("Other matched")
        for item in line.split(":", 1)[-1].split(",")
    ]
    for slug in expected:
        if slug in shown:
            return "shown", shown.index(slug) + 1
    for slug in expected:
        if slug in dropped:
            return "dropped", None
    return "nomatch", None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", default=os.path.expanduser("~/.hermes"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args()

    home = Path(args.hermes_home).expanduser().resolve()
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(home / "bin"))
    sys.path.insert(0, str(home / "hooks"))

    ki = load_module("kinject_eval", home / "hooks" / "knowledge-inject.py")
    kt = __import__("knowledge_topics")
    ktools = load_module("knowledge_tools_eval", home / "plugins" / "knowledge-tools" / "__init__.py")

    topics = []
    for slug, text, _files_dir in kt.iter_topics():
        idx = ki._identity(slug, text)
        idx["text"] = text
        idx["files_summary"] = ""
        topics.append((slug, idx))

    top1 = top3 = found = 0
    mrr_sum = 0.0
    search_misses = []
    toc_counts = Counter()
    toc_misses = []

    for query, expected in EVAL:
        rank, slugs = search_rank(ktools, query, expected)
        if rank:
            found += 1
            mrr_sum += 1 / rank
            top1 += rank == 1
            top3 += rank <= 3
        if not rank or rank > 3:
            search_misses.append({"query": query, "expected": expected, "rank": rank, "got": slugs[:4]})

        status, toc_rank = toc_status(ki, topics, query, expected)
        toc_counts[status] += 1
        if status != "shown":
            toc_misses.append({"query": query, "expected": expected, "status": status, "rank": toc_rank})

    n = len(EVAL)
    summary = {
        "queries": n,
        "search": {
            "top1": top1,
            "top3": top3,
            "found": found,
            "mrr": round(mrr_sum / n, 3),
            "misses": search_misses,
        },
        "toc": {
            "shown": toc_counts["shown"],
            "dropped": toc_counts["dropped"],
            "nomatch": toc_counts["nomatch"],
            "misses": toc_misses,
        },
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Search: top-1 {top1}/{n}, top-3 {top3}/{n}, found {found}/{n}, MRR {mrr_sum / n:.3f}")
        for row in search_misses:
            print(f"  rank={row['rank']} expected={row['expected']} got={row['got']} q={row['query']!r}")
        print(f"TOC: shown {toc_counts['shown']}/{n}, dropped {toc_counts['dropped']}/{n}, nomatch {toc_counts['nomatch']}/{n}")
        for row in toc_misses:
            print(f"  {row['status']} expected={row['expected']} q={row['query']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
