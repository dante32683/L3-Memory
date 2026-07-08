# L3-Memory

A **Git-backed, Obsidian-compatible, local-first memory system** for AI agents. 

Unlike vector-database memory stores that act as black boxes, L3-Memory structures an agent's memory as a human-readable personal wiki. It organizes durable facts as Markdown topic files (complete with Obsidian-style wikilinks and YAML/JSON frontmatter) and procedural guidelines as executable skills.

---

## Directory Structure

This repository is organized as a monorepo containing the general-use engine and integration templates:

```
L3-Memory/ (GitHub Repo Root)
├── README.md               # You are here
├── .gitignore              # Ignores local databases, cache, and secrets
│
├── core/                   # Standalone, general-use library & CLI
│   ├── bin/                # Core engines
│   │   ├── aux_model.py    # Light API client for LLM curation calls
│   │   ├── doc-drift-check # Audits documented claims against OS realities
│   │   ├── drift-apply     # Deterministically auto-applies fixable doc drift
│   │   ├── hermes-add      # Enforces resource count budgets (skills/crons/hooks)
│   │   ├── knowledge-curate # Curation janitor (INBOX -> topic folders)
│   │   ├── knowledge_topics.py # Layout configuration source of truth
│   │   ├── md_sections.py  # Markdown parsing helper for targeted heading reads
│   │   ├── related_links.py # Double-entry related backlinks reconciler
│   │   └── search_index.py # SQLite FTS5 BM25 index manager
│   │
│   ├── hooks/              # Turn & session hooks
│   │   ├── inbox-capture.py # Captures raw user inputs to INBOX
│   │   ├── knowledge-inject.py # Injects matching matched-topic indexes (TOC/Full)
│   │   └── knowledge_mode.py # Manages session platforms toggle modes (off/toc/full)
│   │
│   └── scripts/            # Cron wrappers
│       ├── doc-drift-check-cron.py
│       └── knowledge-curate-nightly.py
│
└── integrations/           # Platform-specific integrations
    └── hermes/
        ├── install-hermes.py   # Automated installer script for Hermes Agent
        ├── config-patch.yaml   # (Reference) YAML patch for hooks config
        └── plugins/
            ├── knowledge-tools/  # Exposes search_knowledge/save_to_knowledge tools
            └── know-mode/        # /know-mode slash command (toggle off/toc/full injection)
```

---

## Configuration & Environment Variables

L3-Memory is context-aware and checks for `L3_HOME` first, falling back to `HERMES_HOME`, then `~/.hermes`. Put these in your environment (or `$L3_HOME/.env`):

* `L3_HOME`: Path to your database/wiki home.
  * Topics are saved under: `$L3_HOME/knowledge/`
  * System index/inbox reside under: `$L3_HOME/knowledge/.system/`
  * Skills reside under: `$L3_HOME/skills/`
* `L3_AUX_API_KEY`: API key for your LLM completion provider. **Required** — no default.
* `L3_AUX_BASE_URL`: Base URL of an OpenAI-compatible chat-completions endpoint (OpenAI, a local vLLM/Ollama proxy, or any hosted provider using the same wire format). **Required** — no default.
* `L3_AUX_MODEL`: Model name to use for auxiliary calls when none is passed explicitly. **Required** — no default.
* `HERMES_CURATE_MODEL` (optional, falls back to `L3_AUX_MODEL`): Model used for high-quality file integrations (nightly curation rewrites).
* `HERMES_CURATE_ROUTE_MODEL` (optional, falls back to `L3_AUX_MODEL`): Faster/cheaper model used for classification routing — set this separately from `HERMES_CURATE_MODEL` if you want a fast/slow split; otherwise both use the same model.

This project ships with no vendor default — you must point it at a real OpenAI-compatible endpoint before curation, discovery-extract, or the drift resolver can run.

---

## Ingest / Curation Workflow

### 1. Ingestion
Captured lines from user chat messages and tool outputs are staged in:
`$L3_HOME/knowledge/.system/INBOX.md`

### 2. Nightly Curation
Automated curation updates, deduplicates, and files inbox details into topic wikis and skill directories.

### 3. Verification & Self-Healing
Audits system topology against assertions in documentation, automatically applying deterministic code changes for drifted values.

---

## Hermes Agent Integration

L3-Memory currently targets **Hermes Agent** only.

To install and activate the memory system, install the required packages and run the installer script from the root of this cloned repository:

```bash
pip install -r requirements.txt
python3 integrations/hermes/install-hermes.py
```

### What this script does automatically:
1.  **Locates Hermes**: Resolves `$L3_HOME`/`$HERMES_HOME`, falling back to `~/.hermes/`.
2.  **Symlinks Source Files**: Links all core hooks, binaries, scripts, and plugins into the Hermes directories (falling back to file copying on Windows if Developer Mode is disabled).
3.  **Patches Configuration**: Automatically registers the pre-LLM, session-end, and session-finalize hooks inside `config.yaml`, preserving your existing settings and comments.
4.  **Registers Cron Tasks**: Runs `hermes cron create` for the nightly curation and self-healing drift-check jobs — never hand-writes `cron/jobs.json` directly, since that schema is internal to Hermes and easy to get subtly wrong. Idempotent: safe to re-run.
5.  **Restarts Gateway**: Runs `hermes gateway restart` to apply the changes immediately.

Note: `inbox-capture.py` reads Hermes's `state.db` directly (`messages` table, `session_id`/`role`/`content`/`active` columns) to pull each session's user lines. That's a real, intentional coupling to Hermes's own storage schema, not something this repo tries to abstract away.

---

## License

This project is licensed under the MIT License.
