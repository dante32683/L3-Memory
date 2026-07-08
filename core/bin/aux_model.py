#!/usr/bin/env python3
"""aux_model — minimal, dependency-light client for auxiliary model calls.

Used by the autonomous knowledge-curation jobs. Talks to any OpenAI-compatible
chat completions endpoint (OpenAI, a local vLLM/Ollama proxy, or any hosted
provider that speaks the same wire format).

Reads API keys from env or $L3_HOME/.env.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("L3_HOME", os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))))


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    envf = HERMES_HOME / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith(name):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def ask(prompt: str, *, temperature: float = 0.2, timeout: int = 120,
        retries: int = 4, provider: str = "default", model: str = "",
        **_kw) -> str:
    """Call an OpenAI-compatible chat completions endpoint.

    ``model`` (optional) overrides the default; falls back to the
    ``L3_AUX_MODEL`` env var. There is no built-in vendor default — you must
    configure ``L3_AUX_BASE_URL`` / ``L3_AUX_API_KEY`` / ``L3_AUX_MODEL`` (or
    pass ``model=`` explicitly) for whichever provider you use."""
    base = _env("L3_AUX_BASE_URL")
    if not base:
        raise RuntimeError("L3_AUX_BASE_URL not found in env or .env")
    key = _env("L3_AUX_API_KEY")
    if not key:
        raise RuntimeError("L3_AUX_API_KEY not found in env or .env")
    chosen_model = model or os.environ.get("L3_AUX_MODEL", "")
    if not chosen_model:
        raise RuntimeError("No model specified: pass model= or set L3_AUX_MODEL")
    body = json.dumps({
        "model": chosen_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    else:
        if last_exc:
            raise last_exc
        return ""
    _log_usage(data, chosen_model, provider)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError):
        return ""


def _log_usage(data: dict, model: str, provider: str) -> None:
    """Append one JSONL line of real token usage per aux call. The `usage` object
    is already in the response we'd otherwise discard, so this costs no extra
    call — it just turns 'estimated ~45k/run' into a measured number. Best-effort:
    any failure is swallowed (never break a curate/discovery run over logging)."""
    try:
        usage = data.get("usage") or {}
        if not usage:
            return
        logf = HERMES_HOME / "logs" / "aux-usage.jsonl"
        logf.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider": provider,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        with logf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _salvage_json(text: str):
    """Extract the last balanced {...} or [...] value from chatty output."""
    text = text.strip()
    # strip code fences if present
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # scan for the last balanced top-level structure
    best = None
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        depth = 0
        start = -1
        in_str = False
        esc = False
        for i, c in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == open_ch:
                if depth == 0:
                    start = i
                depth += 1
            elif c == close_ch and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        cand = json.loads(text[start:i + 1])
                        best = cand  # keep last balanced match
                    except Exception:
                        pass
    return best


def ask_json(prompt: str, *, default=None, **kw):
    """Ask and return parsed JSON, salvaging from chatty output."""
    try:
        raw = ask(prompt, **kw)
    except Exception:
        return default
    out = _salvage_json(raw)
    return out if out is not None else default


if __name__ == "__main__":
    import sys
    # CLI: optional --model <name> flag
    model = ""
    if len(sys.argv) > 1 and sys.argv[1] == "--model":
        model = sys.argv[2] if len(sys.argv) > 2 else ""
        prompt = sys.argv[3] if len(sys.argv) > 3 else "say OK"
    else:
        prompt = sys.argv[1] if len(sys.argv) > 1 else "say OK"
    print(ask(prompt, model=model))
