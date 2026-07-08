#!/usr/bin/env python3
"""install-hermes.py — Installer for integrating L3-Memory into Hermes Agent.

Links core files into $HERMES_HOME, patches config.yaml to register the
lifecycle hooks, registers the nightly cron jobs via `hermes cron create`,
and restarts the gateway.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    home_dir = None
    for ev in ("L3_HOME", "HERMES_HOME"):
        val = os.environ.get(ev)
        if val:
            home_dir = Path(val)
            break
    if not home_dir:
        home_dir = Path.home() / ".hermes"

    if not home_dir.is_dir():
        print(f"Error: Target directory not found at {home_dir}", file=sys.stderr)
        print("Please set L3_HOME or HERMES_HOME if it resides elsewhere.", file=sys.stderr)
        sys.exit(1)

    repo_dir = Path(__file__).resolve().parent.parent.parent

    print("Installing L3-Memory into Hermes Agent...")
    print(f"Agent Home:  {home_dir}")
    print(f"Repository:  {repo_dir}")

    # Ensure directories exist
    for sub in ("bin", "hooks", "plugins", "scripts", "cron"):
        (home_dir / sub).mkdir(parents=True, exist_ok=True)

    def link_or_copy(src: Path, dst: Path):
        if dst.exists() or dst.is_symlink():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()

        try:
            os.symlink(src, dst, target_is_directory=src.is_dir())
            print(f"Linked: {src.name} -> {dst}")
        except OSError:
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            print(f"Copied: {src.name} -> {dst} (symlink fallback)")

    # 1. Link/copy core bin files
    print("\nProcessing bin files...")
    for f in (repo_dir / "core" / "bin").iterdir():
        if f.is_file():
            link_or_copy(f, home_dir / "bin" / f.name)

    # 2. Link/copy core hook files
    print("\nProcessing hook files...")
    for f in (repo_dir / "core" / "hooks").iterdir():
        if f.is_file():
            link_or_copy(f, home_dir / "hooks" / f.name)

    # 3. Link/copy cron scripts
    print("\nProcessing script files...")
    for f in (repo_dir / "core" / "scripts").iterdir():
        if f.is_file():
            link_or_copy(f, home_dir / "scripts" / f.name)

    # 4. Link/copy plugins (folders)
    print("\nProcessing plugins...")
    for d in (repo_dir / "integrations" / "hermes" / "plugins").iterdir():
        if d.is_dir():
            link_or_copy(d, home_dir / "plugins" / d.name)

    # 5. Auto-patch Config File
    print("\nConfiguring lifecycle hooks...")
    patch_config(home_dir)

    # 6. Auto-schedule internal Cron Jobs
    print("\nConfiguring internal cron scheduler...")
    setup_cron_jobs(home_dir)

    # 7. Restart Gateway Daemon
    print("\nApplying gateway changes...")
    restart_gateway()

    print("\nSuccess! L3-Memory is fully installed and active in Hermes Agent.")


def patch_config(home_dir: Path):
    """Patches config.yaml to register the lifecycle hooks."""
    yaml_config = home_dir / "config.yaml"

    target_hooks = {
        "on_session_end": [
            f"python3 {home_dir}/hooks/inbox-capture.py",
            f"python3 {home_dir}/bin/discovery-extract",
        ],
        "on_session_finalize": [
            f"python3 {home_dir}/hooks/inbox-capture.py",
            f"python3 {home_dir}/bin/discovery-extract",
        ],
        "pre_llm_call": [f"python3 {home_dir}/hooks/knowledge-inject.py"],
    }

    if yaml_config.exists():
        patch_yaml_config(yaml_config, target_hooks)
    else:
        print("Warning: Could not find config.yaml. Please register hooks manually.")


def patch_yaml_config(config_path: Path, target_hooks: dict):
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    import yaml

    try:
        cfg = yaml.safe_load(text) or {}
    except Exception:
        print("Warning: Could not parse config YAML safely. Skipping config patch.", file=sys.stderr)
        return

    hooks_cfg = cfg.get("hooks") or {}
    modified = False

    for event, cmds in target_hooks.items():
        existing_cmds = [h.get("command") for h in hooks_cfg.get(event, []) if isinstance(h, dict)]

        for cmd in cmds:
            if cmd in existing_cmds:
                continue

            event_line_idx = -1
            hooks_line_idx = -1

            for idx, line in enumerate(lines):
                if line.strip().startswith("hooks:"):
                    hooks_line_idx = idx
                elif hooks_line_idx != -1 and line.strip().startswith(f"{event}:"):
                    event_line_idx = idx
                    break

            if event_line_idx != -1:
                indent = len(lines[event_line_idx]) - len(lines[event_line_idx].lstrip())
                new_line = " " * (indent + 2) + f"- command: {cmd}"
                lines.insert(event_line_idx + 1, new_line)
                modified = True
            elif hooks_line_idx != -1:
                indent = len(lines[hooks_line_idx]) - len(lines[hooks_line_idx].lstrip())
                new_lines = [" " * (indent + 2) + f"{event}:", " " * (indent + 4) + f"- command: {cmd}"]
                for offset, nl in enumerate(new_lines, 1):
                    lines.insert(hooks_line_idx + offset, nl)
                modified = True

    if modified:
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Config: Patched {config_path.name} to include hooks.")
    else:
        print("Config: Hooks already registered in YAML configuration.")


def setup_cron_jobs(home_dir: Path):
    """Register the nightly jobs through `hermes cron create` (never by
    hand-writing cron/jobs.json — that schema is internal to Hermes and
    hand-written entries are liable to be missing fields the scheduler
    expects). Idempotent: skips any job name already present."""
    try:
        listing = subprocess.run(
            ["hermes", "cron", "list", "--all"],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception as e:
        print(f"Warning: could not run `hermes cron list` ({e}); skipping cron setup. "
              "Register the jobs manually, see README.", file=sys.stderr)
        return
    existing_names = {ln.split("Name:", 1)[1].strip() for ln in listing.splitlines() if "Name:" in ln}

    target_jobs = [
        ("knowledge-curate-nightly", "0 2 * * *", "knowledge-curate-nightly.py"),
        ("doc-drift-check-cron", "0 3 */2 * *", "doc-drift-check-cron.py"),
    ]

    for name, schedule, script in target_jobs:
        if name in existing_names:
            print(f"Cron: '{name}' already registered, skipping.")
            continue
        proc = subprocess.run(
            ["hermes", "cron", "create", schedule,
             "--name", name, "--script", script, "--no-agent", "--deliver", "local"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print(f"Cron: registered '{name}' ({schedule}).")
        else:
            print(f"Warning: failed to register cron job '{name}': {proc.stderr.strip()}", file=sys.stderr)

    print("Note: jobs deliver to 'local' by default — retarget with "
          "`hermes cron edit <id> --deliver telegram` (or discord/signal/etc) if you want alerts elsewhere.")


def restart_gateway():
    try:
        proc = subprocess.run(["hermes", "gateway", "restart"], capture_output=True, text=True)
        if proc.returncode == 0:
            print("Gateway: restarted Hermes gateway service.")
        else:
            print(f"Warning: Failed to restart gateway (exit code {proc.returncode}). Please restart manually.")
    except Exception:
        # CLI command not in path, skip silently
        pass


if __name__ == "__main__":
    main()
