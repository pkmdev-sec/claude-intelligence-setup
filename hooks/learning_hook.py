#!/usr/bin/env python3
"""Stop hook that uses Claude Haiku to detect mistakes and extract learnings."""

import json
import os
import subprocess
import sys
from pathlib import Path


def detect_mistakes_llm(text: str, source: str) -> list[dict]:
    """Use Claude Haiku to detect mistakes in session/memory text."""
    if len(text.strip()) < 50:
        return []

    try:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            # Fallback: load from settings.local.json
            try:
                settings_path = Path.home() / ".claude" / "settings.local.json"
                if settings_path.exists():
                    data = json.loads(settings_path.read_text(encoding="utf-8"))
                    api_key = str(data.get("env", {}).get("ANTHROPIC_API_KEY", "")).strip()
            except Exception:
                pass
        if not api_key:
            return []

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    f"Analyze this development session text for MISTAKES that were made and corrected. "
                    f"A mistake is: wrong approach tried then fixed, error encountered then resolved, "
                    f"incorrect assumption corrected, code that broke then was repaired.\n\n"
                    f"Text:\n{text[:2000]}\n\n"
                    f"Return ONLY JSON array. Empty array if no mistakes found.\n"
                    f'[{{"mistake": "what went wrong", "fix": "what fixed it", "domains": ["relevant", "domains"]}}]'
                ),
            }],
        )

        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

        results = json.loads(raw)
        if not isinstance(results, list):
            return []

        findings = []
        for r in results:
            if isinstance(r, dict) and r.get("mistake"):
                findings.append({
                    "mistake": str(r["mistake"]),
                    "fix": str(r.get("fix", "")),
                    "domains": r.get("domains", []),
                    "source": source,
                })
        return findings

    except Exception:
        return []


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return 0

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    event_name = event.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if event_name != "Stop":
        return 0

    # Collect text to analyze from agent memory directories
    # Check BOTH locations for backward compatibility
    memory_dirs = [
        Path.home() / ".claude" / "agents",        # Current location
        Path.home() / ".claude" / "agent-memory",   # Legacy location
    ]
    all_findings = []

    for memory_dir in memory_dirs:
        if not memory_dir.exists():
            continue
        try:
            entries = list(memory_dir.iterdir())
        except OSError:
            continue
        for agent_dir in entries:
            if not agent_dir.is_dir():
                continue
            memory_file = agent_dir / "MEMORY.md"
            if not memory_file.exists():
                continue
            try:
                content = memory_file.read_text(encoding="utf-8", errors="replace")
                if len(content.strip()) > 50:
                    findings = detect_mistakes_llm(content, agent_dir.name)
                    all_findings.extend(findings)
            except Exception as e:
                _log(f"WARN failed to read {memory_file}: {e}")

    if not all_findings:
        return 0

    # Extract learnings for each mistake found
    extractor = Path.home() / ".claude" / "tools" / "learning-loop" / "extract_learning.py"
    if not extractor.exists():
        return 0

    for mistake in all_findings[:3]:  # Max 3 per session
        try:
            input_data = json.dumps({
                "mistake": mistake["mistake"],
                "context": "",
                "fix_applied": mistake.get("fix", ""),
                "domains": mistake.get("domains", []),
                "source": mistake.get("source", "learning-hook"),
            })

            result = subprocess.run(
                [sys.executable, str(extractor)],
                input=input_data,
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode == 0 and result.stdout.strip():
                output = json.loads(result.stdout)
                if output.get("status") == "created":
                    _log(f"Learning extracted: {output.get('learning', {}).get('slug', '?')}")
        except Exception:
            pass

    return 0


def _log(msg):
    log_file = Path.home() / ".claude" / "hooks" / ".acontext_state" / "bridge.log"
    try:
        with log_file.open("a") as f:
            f.write(f"[learning_hook] {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
