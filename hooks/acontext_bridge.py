#!/usr/bin/env python3
"""Claude Code hook bridge for Acontext Pro using official SDK.

Syncs key session context to Acontext for long-running conversations,
multi-agent workflows, and cross-session continuity.

Enhanced with Smart Write layers:
  Layer 1: Heuristic tier classification (acontext_classifier)
  Layer 2: Feedback loop tracking (acontext_feedback)
  Layer 3: Contradiction detection (acontext_contradictions)
  Layer 4: Behavioral intelligence (acontext_behavior + acontext_dynamics)
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from acontext import AcontextClient

# Smart Write layers (graceful import — layers degrade independently)
try:
    from acontext_classifier import classify_importance
    _HAS_CLASSIFIER = True
except ImportError:
    _HAS_CLASSIFIER = False

try:
    from acontext_feedback import detect_feedback_signals, FeedbackTracker
    _HAS_FEEDBACK = True
except ImportError:
    _HAS_FEEDBACK = False

try:
    from acontext_contradictions import extract_assertions, check_contradictions, format_contradiction_warnings
    _HAS_CONTRADICTIONS = True
except ImportError:
    _HAS_CONTRADICTIONS = False

try:
    from acontext_behavior import BehaviorStore
    _HAS_BEHAVIOR = True
except ImportError:
    _HAS_BEHAVIOR = False

try:
    from acontext_dynamics import analyze_turn_dynamics, extract_inferred_facts
    _HAS_DYNAMICS = True
except ImportError:
    _HAS_DYNAMICS = False

try:
    from acontext_analyzer import analyze_message as consolidated_analyze, AnalysisResult
    _HAS_ANALYZER = True
except ImportError:
    _HAS_ANALYZER = False

try:
    from acontext_config import HAIKU_MODEL
    from acontext_utils import get_haiku_client
except ImportError:
    HAIKU_MODEL = "claude-haiku-4-5"
    def get_haiku_client():
        return None

_behavior_lock = threading.Lock()

def _get_behavior_store() -> "BehaviorStore | None":
    """Get or create the shared BehaviorStore instance."""
    global _behavior_store
    if not _HAS_BEHAVIOR:
        return None
    if _behavior_store is not None:
        return _behavior_store
    with _behavior_lock:
        if _behavior_store is not None:
            return _behavior_store
        try:
            _behavior_store = BehaviorStore()
            return _behavior_store
        except Exception as e:
            log(f"WARN BehaviorStore init failed: {e}")
            return None

_behavior_store: "BehaviorStore | None" = None

# State management paths
HOME = Path.home()
STATE_DIR = HOME / ".claude" / "hooks" / ".acontext_state"
MAP_FILE = STATE_DIR / "session_map.json"
LOCK_FILE = STATE_DIR / "session_map.lock"
LOG_FILE = STATE_DIR / "bridge.log"

# Secret redaction patterns
SECRET_REDACTIONS = [
    (re.compile(r"(sk-[A-Za-z0-9_-]{8,})"), "sk-***REDACTED***"),
    (re.compile(r"(api[_-]?key\s*[=:]\s*)([^\s\"']+)", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(token\s*[=:]\s*)([^\s\"']+)", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(Bearer\s+)([A-Za-z0-9._-]+)", re.IGNORECASE), r"\1***REDACTED***"),
]


# === Utility functions ===

def now_iso() -> str:
    """Return current UTC time in ISO format."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_state_dir() -> None:
    """Create state directory if it doesn't exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    """Append message to bridge log file."""
    ensure_state_dir()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {sanitize(msg, 500)}\n")


def sanitize(text: str, max_len: int = 1200) -> str:
    """Redact secrets and truncate text."""
    out = text
    for pattern, repl in SECRET_REDACTIONS:
        out = pattern.sub(repl, out)
    if len(out) > max_len:
        return out[: max_len - 3] + "..."
    return out


# === SDK client ===

def _load_key_from_settings() -> str:
    """Load API key directly from settings.local.json as fallback."""
    settings_path = HOME / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return ""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return str(data.get("env", {}).get("ACONTEXT_API_KEY", "")).strip()
    except Exception:
        return ""


def _load_setting_from_file(key: str, default: str = "") -> str:
    """Load any env setting from settings.local.json."""
    settings_path = HOME / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return default
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return str(data.get("env", {}).get(key, default)).strip() or default
    except Exception:
        return default


def get_client() -> AcontextClient:
    """Create Acontext SDK client with credentials from environment or settings file."""
    api_key = os.getenv("ACONTEXT_API_KEY", "").strip() or os.getenv("ACONTEXT_API_TOKEN", "").strip()

    # Secondary: read directly from settings.local.json when env propagation fails
    if not api_key:
        api_key = _load_key_from_settings()
        if api_key:
            log("INFO loaded ACONTEXT_API_KEY from settings.local.json (env was empty)")

    if not api_key:
        raise ValueError("ACONTEXT_API_KEY is not set")

    base_url = os.getenv("ACONTEXT_BASE_URL", "").strip()
    if not base_url:
        base_url = _load_setting_from_file("ACONTEXT_BASE_URL", "https://api.acontext.app/api/v1")

    return AcontextClient(api_key=api_key, base_url=base_url.rstrip("/"))


# === Session mapping (file-locked for multi-instance safety) ===

def with_session_map(update_fn):
    """Execute function with locked access to session map."""
    ensure_state_dir()
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        # Load existing map
        data: dict[str, str] = {}
        if MAP_FILE.exists():
            try:
                loaded = json.loads(MAP_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {str(k): str(v) for k, v in loaded.items()}
            except Exception:
                data = {}

        # Execute update function
        result = update_fn(data)

        # Write back atomically
        tmp = MAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, MAP_FILE)

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def get_mapped_session(claude_session_id: str) -> str | None:
    """Get Acontext session ID for a Claude session ID."""
    if not claude_session_id:
        return None

    def _reader(data: dict[str, str]) -> str | None:
        return data.get(claude_session_id)

    return with_session_map(_reader)


def map_session(claude_session_id: str, acontext_session_id: str) -> None:
    """Map Claude session ID to Acontext session ID."""
    if not claude_session_id or not acontext_session_id:
        return

    def _writer(data: dict[str, str]) -> None:
        data[claude_session_id] = acontext_session_id

    with_session_map(_writer)


# === Acontext operations ===

def create_or_get_session(event: dict[str, Any]) -> str | None:
    """Create new Acontext session or retrieve existing one."""
    claude_session_id = str(event.get("session_id") or "")
    if not claude_session_id:
        return None

    # Check for existing mapping
    existing = get_mapped_session(claude_session_id)
    if existing:
        return existing

    # Create new session
    try:
        client = get_client()
        configs = {
            "source": "claude-code",
            "claude_session_id": claude_session_id,
            "cwd": str(event.get("cwd") or ""),
            "model": str(event.get("model") or ""),
            "created_at": now_iso(),
        }

        user = os.getenv("ACONTEXT_USER") or os.getenv("USER")
        response = client.sessions.create(
            user=user if user else None,
            disable_task_tracking=False,
            configs=configs,
        )

        session_id = response.id
        if not session_id:
            raise RuntimeError("Session create returned no ID")

        map_session(claude_session_id, session_id)
        return session_id

    except ValueError as e:
        # Missing API key
        log(f"INFO {e}")
        return None
    except Exception as e:
        log(f"ERROR create_session failed: {e}")
        return None


def store_message(session_id: str, role: str, content: str, meta: dict[str, Any]) -> None:
    """Store message in Acontext session with tier classification."""
    if not session_id or not content:
        return

    # Layer 1: Classify importance tier
    if _HAS_CLASSIFIER and not content.startswith("[HOOK]"):
        try:
            classification = classify_importance(content, role)
            meta["tier"] = classification["tier"]
            meta["tier_confidence"] = classification["confidence"]
            meta["tier_category"] = classification["category"]
            meta["entities"] = classification["entities"]
        except Exception as e:
            log(f"WARN classifier failed: {e}")

    try:
        client = get_client()
        client.sessions.store_message(
            session_id=session_id,
            format="openai",
            blob={"role": role, "content": sanitize(content, 4000)},
            meta=meta,
        )
    except Exception as e:
        log(f"WARN store_message failed: {e}")


def get_smart_context(session_id: str, token_limit: int | None = None) -> str:
    """Retrieve context using edit strategies for smart compression."""
    if not session_id:
        return ""

    if token_limit is None:
        token_limit = int(os.getenv("ACONTEXT_CONTEXT_TOKEN_LIMIT", "8000"))

    try:
        client = get_client()
        result = client.sessions.get_messages(
            session_id=session_id,
            format="openai",
            edit_strategies=[
                {"type": "remove_tool_result", "params": {"keep_recent_n_tool_results": 3}},
                {"type": "token_limit", "params": {"limit_tokens": token_limit}},
            ],
        )

        # Extract messages from response with validation
        if result is None:
            return ""
        items = getattr(result, 'items', None) or (result.get('items') if isinstance(result, dict) else None)
        if not items:
            return ""
        lines: list[str] = []

        for msg in items:
            if not isinstance(msg, dict):
                continue

            role = str(msg.get("role") or "unknown")
            content = msg.get("content")
            text = _extract_text(content)

            if not text or text.startswith("[HOOK]"):
                continue

            lines.append(f"{role}: {sanitize(text, 180)}")

        if not lines:
            return ""

        return "\n".join(f"- {line}" for line in lines[:10])

    except Exception as e:
        log(f"WARN get_smart_context failed: {e}")
        return ""


def get_session_summary(session_id: str) -> str:
    """Get session summary for subagent context."""
    if not session_id:
        return ""

    try:
        client = get_client()
        summary = client.sessions.get_session_summary(session_id=session_id)

        # Handle various response formats with validation
        if summary is None:
            return ""
        if isinstance(summary, str):
            return summary
        if hasattr(summary, "summary"):
            return str(summary.summary)
        if isinstance(summary, dict) and "summary" in summary:
            return str(summary["summary"])

        return str(summary) if summary else ""

    except Exception as e:
        log(f"WARN get_session_summary failed: {e}")
        return ""


def _suggest_workflow(prompt: str, classification: dict) -> str:
    """Use Claude Haiku to analyze task intent and suggest optimal agent workflow."""
    if len(prompt.strip()) < 20:
        return ""  # Too short to analyze

    try:
        client = get_haiku_client()
        if not client:
            return _suggest_workflow_regex(prompt)

        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": (
                    f'Classify this developer task. Return ONLY one word: '
                    f'bug | substantial | medium | trivial | question\n\n'
                    f'bug = fixing error/failure/crash/unexpected behavior\n'
                    f'substantial = new feature, multi-file change, architecture, complex build\n'
                    f'medium = small feature, 1-3 file change, clear requirement\n'
                    f'trivial = one-line edit, formatting, rename\n'
                    f'question = asking for explanation, not building anything\n\n'
                    f'Task: "{prompt[:200]}"'
                ),
            }],
        )

        task_type = resp.content[0].text.strip().lower().split()[0].rstrip(".")

        if task_type == "bug":
            return (
                "[Workflow] Bug/error task. "
                "Spawn `debugger` for root cause analysis with execution flow tracing. "
                "After fix, spawn `critic` to verify no regressions."
            )
        elif task_type == "substantial":
            return (
                "[Workflow] Substantial build task. "
                "Work directly with accumulated intelligence. "
                "After implementation, spawn `critic` for adversarial review — "
                "it catches bugs you can't find in your own work. "
                "If unfamiliar codebase, spawn `scout` first for structured research."
            )
        elif task_type == "medium":
            return (
                "[Workflow] Implementation task. Work directly. "
                "Consider spawning `critic` after if changes touch 3+ files."
            )
        # trivial and question → no suggestion
        return ""

    except Exception as e:
        log(f"WARN workflow LLM suggestion failed: {e}")
        return _suggest_workflow_regex(prompt)


def _suggest_workflow_regex(prompt: str) -> str:
    """Fallback: regex-based workflow suggestion when Haiku unavailable."""
    prompt_lower = prompt.lower()
    bug_kw = ["bug", "error", "fail", "broken", "crash", "not working"]
    build_kw = ["build", "create", "implement", "add feature", "design", "refactor", "migrate"]
    if any(s in prompt_lower for s in bug_kw):
        return "[Workflow] Bug detected. Spawn `debugger` then `critic`."
    if any(s in prompt_lower for s in build_kw) and len(prompt) > 80:
        return "[Workflow] Substantial task. Work directly, spawn `critic` after."
    return ""


def _workflow_hint_from_type(workflow_type: str, prompt: str) -> str:
    """Convert consolidated workflow type to hint text."""
    hints = {
        "bug": (
            "[Workflow] Bug/error task. "
            "Spawn `debugger` for root cause analysis with execution flow tracing. "
            "After fix, spawn `critic` to verify no regressions."
        ),
        "substantial": (
            "[Workflow] Substantial build task. "
            "Work directly with accumulated intelligence. "
            "After implementation, spawn `critic` for adversarial review — "
            "it catches bugs you can't find in your own work. "
            "If unfamiliar codebase, spawn `scout` first for structured research."
        ),
        "medium": (
            "[Workflow] Implementation task. Work directly. "
            "Consider spawning `critic` after if changes touch 3+ files."
        ),
    }
    return hints.get(workflow_type, "")


def _get_relevant_learnings(prompt: str) -> str:
    """Search Praxis Engine for learnings relevant to the current task.

    Calls the `learn` CLI to search Supabase for WHEN/SCAN/FIX/NOT learnings
    that match the user's prompt. Returns formatted context for injection.
    """
    import subprocess

    learn_bin = str(Path.home() / ".cargo" / "bin" / "learn")
    if not Path(learn_bin).exists():
        return ""

    # Extract 2-3 key terms from prompt for search
    words = [w for w in prompt.lower().split() if len(w) > 3]
    query = " ".join(words[:5]) if words else prompt[:50]

    # Sanitize query to prevent injection (keep only word chars, spaces, hyphens, dots)
    query = re.sub(r'[^\w\s\-.]', '', query)

    timeout = int(os.getenv("PRAXIS_SEARCH_TIMEOUT", "8"))

    try:
        # Load Supabase credentials for learn CLI
        try:
            from acontext_utils import load_praxis_env_vars
            praxis_env = load_praxis_env_vars()
        except ImportError:
            praxis_env = {}

        result = subprocess.run(
            [learn_bin, "search", query, "--raw"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(Path.home()), **praxis_env},
        )
        if result.returncode != 0:
            log(f"WARN praxis search rc={result.returncode}")
            return ""
        if not result.stdout.strip():
            return ""

        output = result.stdout.strip()
        # Only inject if results are relevant (not empty/error)
        if len(output) < 20 or "error" in output.lower()[:50]:
            return ""

        return "Relevant learnings from past experience:\n" + output

    except subprocess.TimeoutExpired:
        log(f"WARN praxis search timeout after {timeout}s")
        return ""
    except (FileNotFoundError, Exception) as e:
        log(f"WARN praxis search failed: {e}")
        return ""


def flush_session(session_id: str) -> None:
    """Flush session for task extraction (non-blocking)."""
    if not session_id:
        return

    try:
        client = get_client()
        client.sessions.flush(session_id=session_id)
    except Exception as e:
        log(f"WARN flush failed: {e}")


def _extract_text(content: Any) -> str:
    """Extract text from OpenAI message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return "\n".join(t for t in texts if t)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


# === Hook handlers ===

def _cleanup_stale_files():
    """Remove orphaned temp and state files from crashed sessions."""
    import time
    state_dir = STATE_DIR
    if not state_dir.exists():
        return
    now = time.time()
    one_hour = 3600
    for pattern in ["*.tmp", "feedback_*.json"]:
        for f in state_dir.glob(pattern):
            try:
                if now - f.stat().st_mtime > one_hour:
                    f.unlink()
            except Exception:
                pass


def handle_session_start(event: dict[str, Any]) -> dict[str, Any]:
    """Handle SessionStart event."""
    _cleanup_stale_files()
    session_id = create_or_get_session(event)
    if not session_id:
        return {}

    source = str(event.get("source") or "startup")
    model = str(event.get("model") or "")

    store_message(
        session_id,
        "assistant",
        f"[HOOK][SessionStart] source={source} model={model}",
        {
            "event": "SessionStart",
            "source": source,
            "model": model,
            "cwd": str(event.get("cwd") or ""),
            "at": now_iso(),
        },
    )

    context = (
        f"Acontext memory bridge connected (session {session_id}). "
        "Use persisted context from prior turns to reduce drift and compaction loss."
    )
    return hook_output_for("SessionStart", context)


def handle_user_prompt_submit(event: dict[str, Any]) -> dict[str, Any]:
    """Handle UserPromptSubmit event with consolidated LLM analysis."""
    session_id = create_or_get_session(event)
    if not session_id:
        return {}

    prompt = str(event.get("prompt") or "").strip()

    # Retrieve smart context (needed for analysis + feedback)
    smart_context = get_smart_context(session_id)
    context_lines = [line for line in smart_context.strip().split("\n")
                     if line and "[HOOK]" not in line] if smart_context else []

    # === Consolidated LLM Analysis (replaces separate classifier/dynamics/contradictions) ===
    analysis = None
    if _HAS_ANALYZER and prompt:
        try:
            analysis = consolidated_analyze(prompt, context_lines)
            log(f"INFO analyzer: tier={analysis.tier} cat={analysis.category} "
                f"dynamics={len(analysis.dynamics)} contras={len(analysis.contradictions)} "
                f"workflow={analysis.workflow} source={analysis.source}")
        except Exception as e:
            log(f"WARN consolidated analysis failed: {e}")

    # === Legacy path (when analyzer unavailable) ===
    if analysis is None:
        # Layer 4: Analyze turn dynamics
        if _HAS_DYNAMICS and prompt:
            try:
                dynamics = analyze_turn_dynamics(prompt, context_lines)
                if dynamics:
                    store = _get_behavior_store()
                    claude_sid = str(event.get("session_id") or "")
                    if store and claude_sid:
                        facts = extract_inferred_facts(dynamics)
                        for fact in facts:
                            store.record_turn_dynamic(
                                claude_sid,
                                fact["source_dynamic"],
                                fact["fact"],
                                fact["fact"],
                            )
            except Exception as e:
                log(f"WARN dynamics analysis failed: {e}")

    # === Store dynamics from analyzer into behavioral DB ===
    if analysis and analysis.dynamics:
        store = _get_behavior_store()
        claude_sid = str(event.get("session_id") or "")
        if store and claude_sid:
            try:
                if _HAS_DYNAMICS:
                    facts = extract_inferred_facts(analysis.dynamics)
                else:
                    facts = [{"source_dynamic": d["type"], "fact": d.get("inferred_fact", d.get("fact", ""))}
                             for d in analysis.dynamics if d.get("confidence", 0) >= 0.7]
                for fact in facts:
                    store.record_turn_dynamic(
                        claude_sid,
                        fact["source_dynamic"],
                        fact["fact"],
                        fact["fact"],
                    )
            except Exception as e:
                log(f"WARN dynamics storage failed: {e}")

    # Layer 2: Detect feedback signals (unchanged — not part of consolidated analysis)
    if _HAS_FEEDBACK and prompt and context_lines:
        try:
            claude_sid = str(event.get("session_id") or "")
            tracker = FeedbackTracker(claude_sid)
            signals = detect_feedback_signals(prompt, context_lines)
            tracker.record_turn(signals, context_lines)
        except Exception as e:
            log(f"WARN feedback detection failed: {e}")

    # Store user message with tier metadata from analysis
    if prompt:
        meta: dict[str, Any] = {
            "event": "UserPromptSubmit",
            "cwd": str(event.get("cwd") or ""),
            "at": now_iso(),
        }
        if analysis:
            meta["tier"] = analysis.tier
            meta["tier_category"] = analysis.category
            meta["entities"] = analysis.entities
            meta["analysis_source"] = analysis.source

        store_message(session_id, "user", prompt, meta)

    if not smart_context:
        return {}

    # === Build context injection from analysis ===
    contradiction_warning = ""
    workflow_hint = ""

    if analysis:
        # Contradictions from consolidated analysis
        if analysis.contradictions:
            try:
                if _HAS_CONTRADICTIONS:
                    contradiction_warning = format_contradiction_warnings(analysis.contradictions)
                else:
                    # Minimal formatting fallback
                    warnings = ["Memory Update Detected:"]
                    for c in analysis.contradictions:
                        warnings.append(f"  - {c.get('contradiction_type', 'change')}: "
                                       f"'{c.get('contradicted_line', '')[:60]}' -> "
                                       f"'{c.get('new_assertion', {}).get('assertion', '')[:60]}'")
                    contradiction_warning = "\n".join(warnings)
            except Exception as e:
                log(f"WARN contradiction formatting failed: {e}")

        # Workflow suggestion from consolidated analysis
        if analysis.workflow and analysis.workflow not in ("trivial", "question"):
            workflow_hint = _workflow_hint_from_type(analysis.workflow, prompt)

    else:
        # Legacy contradiction detection
        classification = None
        if _HAS_CLASSIFIER and _HAS_CONTRADICTIONS and prompt:
            try:
                classification = classify_importance(prompt, "user")
                if classification["tier"] <= 1:
                    assertions = extract_assertions(
                        prompt, classification["tier"], classification["entities"]
                    )
                    if assertions:
                        contradictions = check_contradictions(assertions, context_lines)
                        if contradictions:
                            contradiction_warning = format_contradiction_warnings(contradictions)
            except Exception as e:
                log(f"WARN contradiction detection failed: {e}")

        # Legacy workflow suggestion
        if _HAS_CLASSIFIER and prompt and len(prompt) > 30:
            try:
                if classification is None:
                    classification = classify_importance(prompt, "user")
                workflow_hint = _suggest_workflow(prompt, classification)
            except Exception as e:
                log(f"WARN workflow suggestion failed: {e}")

    # Auto-inject relevant Praxis learnings (unchanged)
    praxis_context = ""
    if prompt:
        try:
            praxis_context = _get_relevant_learnings(prompt)
        except Exception as e:
            log(f"WARN praxis learning injection failed: {e}")

    # Build final context
    context_parts = ["Acontext recent persisted context:\n" + smart_context]
    if contradiction_warning:
        context_parts.append(contradiction_warning)
    if praxis_context:
        context_parts.append(praxis_context)
    if workflow_hint:
        context_parts.append(workflow_hint)

    return hook_output_for("UserPromptSubmit", "\n\n".join(context_parts))


def handle_pre_tool_use(event: dict[str, Any]) -> dict[str, Any]:
    """Handle PreToolUse event — inject file-level behavioral warnings."""
    store = _get_behavior_store()
    if not store:
        return {}

    tool_name = event.get("tool", {}).get("name", "") if isinstance(event.get("tool"), dict) else str(event.get("tool_name", ""))
    tool_input = event.get("tool", {}).get("input", {}) if isinstance(event.get("tool"), dict) else event.get("tool_input", {})

    if tool_name not in ("Read", "Edit", "Write"):
        return {}

    file_path = str(tool_input.get("file_path", ""))
    if not file_path:
        return {}

    try:
        warnings = store.get_file_warnings(file_path)
        if not warnings:
            return {}

        warning_text = f"[Behavioral Memory] {file_path}:\n" + "\n".join(f"  - {w}" for w in warnings[:3])
        return hook_output_for("PreToolUse", warning_text)
    except Exception as e:
        log(f"WARN pre_tool_use behavioral lookup failed: {e}")
        return {}


def handle_post_tool_use(event: dict[str, Any]) -> dict[str, Any]:
    """Handle PostToolUse event — capture behavioral signals from tool execution."""
    store = _get_behavior_store()
    if not store:
        return {}

    tool_name = event.get("tool", {}).get("name", "") if isinstance(event.get("tool"), dict) else str(event.get("tool_name", ""))
    tool_input = event.get("tool", {}).get("input", {}) if isinstance(event.get("tool"), dict) else event.get("tool_input", {})
    tool_response = event.get("tool", {}).get("response", {}) if isinstance(event.get("tool"), dict) else event.get("tool_response", {})

    claude_sid = str(event.get("session_id") or "")
    if not claude_sid:
        return {}

    try:
        # Track file operations
        if tool_name in ("Read", "Edit", "Write"):
            file_path = str(tool_input.get("file_path", ""))
            if file_path:
                is_error = bool(tool_response.get("error")) if isinstance(tool_response, dict) else False
                error_msg = str(tool_response.get("error", ""))[:200] if isinstance(tool_response, dict) and is_error else ""
                store.record_file_event(claude_sid, file_path, tool_name, success=not is_error, error_msg=error_msg)

        # Track Bash commands
        elif tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            if command:
                exit_code = 0
                error_msg = ""
                if isinstance(tool_response, dict):
                    exit_code = int(tool_response.get("exitCode", tool_response.get("exit_code", 0)))
                    if exit_code != 0:
                        stderr = str(tool_response.get("stderr", tool_response.get("error", "")))
                        error_msg = stderr[:300]
                store.record_command_event(claude_sid, command, exit_code, error_msg)

    except Exception as e:
        log(f"WARN post_tool_use behavioral capture failed: {e}")

    return {}


def handle_subagent_start(event: dict[str, Any]) -> dict[str, Any]:
    """Handle SubagentStart event."""
    session_id = create_or_get_session(event)
    if not session_id:
        return {}

    agent_type = str(event.get("agent_type") or "subagent")
    agent_id = str(event.get("agent_id") or "")

    # Store subagent start marker
    store_message(
        session_id,
        "assistant",
        f"[HOOK][SubagentStart] agent_type={agent_type}",
        {
            "event": "SubagentStart",
            "agent_type": agent_type,
            "agent_id": agent_id,
            "at": now_iso(),
        },
    )

    # Try session summary first, fall back to smart context
    summary = get_session_summary(session_id)
    context_parts = []

    if summary:
        context_parts.append(
            "Acontext session summary for subagent context:\n"
            + sanitize(summary, 3000)
        )
    else:
        smart_context = get_smart_context(session_id, token_limit=6000)
        if smart_context:
            context_parts.append(
                "Acontext shared context for this subagent. Use this to align with latest "
                "project decisions, requirements, and progress.\n"
                + smart_context
            )

    # Inject Praxis learnings relevant to subagent's role
    try:
        query_parts = [agent_type]
        if summary:
            words = [w for w in summary.lower().split() if len(w) > 4 and w.isalpha()]
            query_parts.extend(list(dict.fromkeys(words))[:3])
        learnings = _get_relevant_learnings(" ".join(query_parts[:5]))
        if learnings:
            context_parts.append(learnings)
            log(f"INFO subagent {agent_type} received praxis learnings")
    except Exception as e:
        log(f"WARN subagent praxis injection failed: {e}")

    if not context_parts:
        return {}

    return hook_output_for("SubagentStart", "\n\n".join(context_parts))


def handle_stop(event: dict[str, Any]) -> dict[str, Any]:
    """Handle Stop event - flush session + aggregate feedback."""
    session_id = create_or_get_session(event)
    if not session_id:
        return {}

    stop_active = bool(event.get("stop_hook_active") or False)

    # Layer 2: Aggregate and store session feedback
    feedback_summary = {}
    if _HAS_FEEDBACK:
        try:
            claude_sid = str(event.get("session_id") or "")
            tracker = FeedbackTracker(claude_sid)
            feedback_summary = tracker.get_aggregate()
            tracker.cleanup()
        except Exception as e:
            log(f"WARN feedback aggregation failed: {e}")

    # Store stop event with feedback data
    stop_meta: dict[str, Any] = {
        "event": "Stop",
        "stop_hook_active": stop_active,
        "at": now_iso(),
    }
    if feedback_summary:
        stop_meta["session_feedback"] = feedback_summary

    store_message(
        session_id,
        "assistant",
        f"[HOOK][Stop] stop_hook_active={str(stop_active).lower()}",
        stop_meta,
    )

    # Log feedback summary for visibility
    if feedback_summary:
        ref_rate = feedback_summary.get("memory_reference_rate", 0)
        quality = feedback_summary.get("session_quality_score", 0)
        neg = feedback_summary.get("negative_signal_count", 0)
        log(f"INFO session feedback: ref_rate={ref_rate:.2f} quality={quality:.2f} negatives={neg}")

    # Layer 4: Store behavioral summary with session
    store = _get_behavior_store()
    claude_sid = str(event.get("session_id") or "")
    if store and claude_sid:
        try:
            behavioral = store.get_session_behavioral_summary(claude_sid)
            if behavioral:
                hot = behavioral.get("hot_files", [])
                decisions = behavioral.get("inferred_decisions", [])
                prefs = behavioral.get("inferred_preferences", [])
                validated = behavioral.get("validated_approaches", [])
                log(f"INFO behavioral: hot_files={len(hot)} decisions={len(decisions)} "
                    f"preferences={len(prefs)} validated={len(validated)}")

                # Store behavioral summary as a high-value message for future retrieval
                if decisions or prefs or validated:
                    summary_parts = []
                    if decisions:
                        summary_parts.append("Decisions: " + "; ".join(decisions[:5]))
                    if prefs:
                        summary_parts.append("Preferences: " + "; ".join(prefs[:5]))
                    if validated:
                        summary_parts.append("Validated: " + "; ".join(
                            f"{v['file']} passed {v['test_command']}" for v in validated[:3]
                        ) if isinstance(validated[0], dict) else "; ".join(str(v) for v in validated[:3]))

                    store_message(
                        session_id,
                        "assistant",
                        "[BEHAVIORAL] " + " | ".join(summary_parts),
                        {
                            "event": "Stop",
                            "tier": 0,
                            "tier_category": "behavioral_summary",
                            "behavioral_data": behavioral,
                            "at": now_iso(),
                        },
                    )
        except Exception as e:
            log(f"WARN behavioral summary failed: {e}")

    # Flush for task extraction (non-blocking)
    flush_session(session_id)

    # Clean up BehaviorStore
    if store:
        try:
            store.close()
        except Exception:
            pass

    return {}


def hook_output_for(event_name: str, additional_context: str) -> dict[str, Any]:
    """Format hook output response."""
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": additional_context,
        },
        "suppressOutput": True,
    }


# === Main dispatcher ===

def dispatch(event_name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Dispatch event to appropriate handler."""
    handlers = {
        "SessionStart": handle_session_start,
        "UserPromptSubmit": handle_user_prompt_submit,
        "PreToolUse": handle_pre_tool_use,
        "PostToolUse": handle_post_tool_use,
        "SubagentStart": handle_subagent_start,
        "Stop": handle_stop,
    }

    handler = handlers.get(event_name)
    if not handler:
        return {}

    return handler(event)


def main() -> int:
    """Main entry point for hook script."""
    # Read event from stdin
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"WARN failed to parse hook stdin as JSON: {raw[:200] if raw else '(empty)'}")
        return 0

    # Get event name from stdin data or argv
    arg_event = sys.argv[1] if len(sys.argv) > 1 else ""
    event_name = str(event.get("hook_event_name") or arg_event)

    if not event_name:
        log("WARN event_name missing")
        return 0

    try:
        result = dispatch(event_name, event)
        if result:
            sys.stdout.write(json.dumps(result))
        return 0
    except ValueError as e:
        # Missing API key - graceful no-op
        log(f"INFO {e}; hook no-op")
        return 0
    except Exception as e:
        log(f"ERROR {event_name} failed: {e}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
