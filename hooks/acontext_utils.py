"""Shared utilities for Acontext hook system."""

import json
import os
from typing import Any, Optional


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences from LLM JSON responses."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1].strip()
            if inner.startswith("json"):
                inner = inner[4:].strip()
            return inner
    return text


def parse_llm_json(text: str) -> Any:
    """Parse JSON from LLM response, handling code fences."""
    cleaned = strip_json_fences(text)
    return json.loads(cleaned)


def get_haiku_client():
    """Get Anthropic client for Haiku calls. Returns None if unavailable.

    Tries ANTHROPIC_API_KEY from env first, falls back to settings.local.json.
    Configures 8-second timeout to prevent hook hangs.
    """
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            api_key = _load_anthropic_key_from_settings()
        if not api_key:
            return None
        return anthropic.Anthropic(api_key=api_key, timeout=8.0)
    except ImportError:
        return None


def _load_anthropic_key_from_settings() -> str:
    """Load ANTHROPIC_API_KEY from settings files as fallback."""
    from pathlib import Path
    for filename in ("settings.local.json", "settings.json"):
        settings_path = Path.home() / ".claude" / filename
        if not settings_path.exists():
            continue
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            key = str(data.get("env", {}).get("ANTHROPIC_API_KEY", "")).strip()
            if key:
                return key
        except Exception:
            continue
    return ""


def load_praxis_env_vars() -> dict:
    """Load Supabase env vars from praxis-engine/.env and settings.local.json.

    Checks two sources for resilience:
    1. ~/praxis-engine/.env (primary — Praxis Engine config)
    2. ~/.claude/settings.local.json env section (fallback — for agent subprocesses)
    """
    from pathlib import Path
    import re

    needed_keys = ("SUPABASE_URL", "SUPABASE_ANON_KEY")
    env_vars = {}

    # Source 1: praxis-engine/.env file
    env_file = Path.home() / "praxis-engine" / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r'^([A-Z_]+)=(.*)', line)
                if match and match.group(1) in needed_keys:
                    val = match.group(2).strip().strip('"').strip("'")
                    if val:
                        env_vars[match.group(1)] = val
        except Exception:
            pass

    # Source 2: settings.local.json (fallback for missing keys)
    if len(env_vars) < len(needed_keys):
        settings_path = Path.home() / ".claude" / "settings.local.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                settings_env = data.get("env", {})
                for key in needed_keys:
                    if key not in env_vars:
                        val = str(settings_env.get(key, "")).strip()
                        if val:
                            env_vars[key] = val
            except Exception:
                pass

    return env_vars
