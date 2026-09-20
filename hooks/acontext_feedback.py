#!/usr/bin/env python3
"""Acontext Smart Write Layer 2: Memory Usefulness Tracking.

Tracks feedback signals from user messages to detect whether injected memories
were useful, outdated, or contradicted. Provides session-level aggregation and
memory scoring updates for adaptive context injection.

Hybrid regex + Claude Haiku pattern for enhanced accuracy.
Fast regex path for obvious cases, Haiku fallback for nuanced detection.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from acontext_config import HAIKU_MODEL, CONTRADICTION_MIN_CONFIDENCE, CONTRADICTION_LLM_MAX_CONFIDENCE
    from acontext_utils import parse_llm_json, get_haiku_client
except ImportError:
    HAIKU_MODEL = "claude-haiku-4-5"
    CONTRADICTION_MIN_CONFIDENCE = 0.50
    CONTRADICTION_LLM_MAX_CONFIDENCE = 0.85
    def parse_llm_json(text: str):
        import json
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            if len(parts) >= 2:
                inner = parts[1].strip()
                if inner.startswith("json"):
                    inner = inner[4:].strip()
                text = inner
        return json.loads(text)
    def get_haiku_client():
        return None

# State management paths
STATE_DIR = Path.home() / ".claude" / "hooks" / ".acontext_state"


# === Contradiction & Correction Detection ===

CONTRADICTION_PATTERNS = [
    # Direct negation
    re.compile(r"\b(no|not?)\b[,\s]+(we\s+)?(don't|do\s+not|doesn't)\s+(use|have|support)", re.IGNORECASE),
    re.compile(r"\b(that'?s?|that\s+is)\s+(not\s+right|incorrect|outdated|wrong|inaccurate)", re.IGNORECASE),
    re.compile(r"\b(we|i)\s+(don't|do\s+not|doesn't|no\s+longer)\s+(use|have|support)", re.IGNORECASE),

    # Changes and deprecation
    re.compile(r"\b(we\s+)?(changed|moved|switched|migrated|deprecated)\s+(that|this|it)", re.IGNORECASE),
    re.compile(r"\b(we\s+)?(removed|deleted|archived|retired)\s+(that|this|it)", re.IGNORECASE),

    # Corrections with "actually"
    re.compile(r"\bactually\b[,\s]+(we\s+)?(use|have|it'?s)", re.IGNORECASE),
    re.compile(r"\b(it'?s?|that'?s?)\s+actually\s+", re.IGNORECASE),

    # File/resource non-existence
    re.compile(r"\b(that\s+)?(file|directory|folder|path|module|config)\s+(doesn't\s+exist|does\s+not\s+exist|is\s+gone|was\s+deleted)", re.IGNORECASE),
    re.compile(r"\b(that|this|it)\s+(doesn't\s+exist\s+anymore|doesn't\s+exist|does\s+not\s+exist|is\s+missing|was\s+removed|was\s+deleted)", re.IGNORECASE),
]

CORRECTION_PATTERNS = [
    re.compile(r"\bactually\b[,\s]+", re.IGNORECASE),
    re.compile(r"\bin\s+reality\b[,\s]+", re.IGNORECASE),
    re.compile(r"\bto\s+clarify\b[,\s]+", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+correct\b", re.IGNORECASE),
    re.compile(r"\bjust\s+to\s+be\s+clear\b[,\s]+", re.IGNORECASE),
]


def _normalize_text(text: str) -> str:
    """Normalize text for comparison - lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


def _extract_entities(text: str) -> set[str]:
    """Extract potential entity tokens from text for alignment detection.

    Entities include:
    - CamelCase or snake_case identifiers
    - File paths with extensions
    - Technology names (React, JWT, PostgreSQL, etc.)
    - Domain-specific terms
    - Capitalized words (technology/product names)
    """
    entities = set()

    # CamelCase identifiers
    entities.update(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text))

    # snake_case identifiers
    entities.update(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text))

    # File paths with extensions
    entities.update(re.findall(r"\b[\w/.-]+\.(?:py|js|ts|jsx|tsx|go|rs|java|cpp|h|md|json|yaml|yml|toml|env)\b", text, re.IGNORECASE))

    # Common tech names (uppercase patterns)
    entities.update(re.findall(r"\b(?:[A-Z]{2,}|[A-Z][a-z]*[A-Z][A-Z]+)\b", text))

    # Capitalized words (likely technology/product names)
    entities.update(re.findall(r"\b[A-Z][a-z]{2,}\b", text))

    # Directory paths and file names
    entities.update(re.findall(r"\b(?:src|lib|dist|build|components|config|auth|api|utils)/[\w/.-]*", text, re.IGNORECASE))

    return {e.lower() for e in entities if len(e) > 2}


def _detect_contradiction(user_prompt: str, context_line: str) -> str | None:
    """Detect if user prompt contradicts a context line.

    Returns the contradiction text if detected, None otherwise.
    """
    prompt_lower = user_prompt.lower()
    context_lower = context_line.lower()

    # Extract entities from context for matching
    context_entities = _extract_entities(context_line)

    # Also extract simple words (3+ chars) from context, excluding common words
    common_words = {"user", "assistant", "using", "found", "have", "that", "this", "with", "from", "the"}
    context_words = set(
        w.lower() for w in re.findall(r"\b\w{3,}\b", context_line)
        if w.lower() not in common_words
    )

    # Check for explicit contradiction patterns
    for pattern in CONTRADICTION_PATTERNS:
        match = pattern.search(prompt_lower)
        if match:
            # Check if any context entity or word appears in the prompt
            # Use substring matching to handle stemming (cache/caching, config/configuration)
            all_context_tokens = context_entities | context_words

            # Try exact match first
            has_match = any(token in prompt_lower for token in all_context_tokens if token and len(token) > 3)

            # Try stem matching (first 4 chars) for words 5+ chars
            # This handles variations like cache/caching, config/configuration
            if not has_match:
                for token in all_context_tokens:
                    if len(token) >= 5:
                        stem = token[:4]
                        if stem in prompt_lower:
                            has_match = True
                            break

            if has_match:
                # Extract surrounding context
                start = max(0, match.start() - 30)
                end = min(len(user_prompt), match.end() + 50)
                return user_prompt[start:end].strip()

    return None


def _detect_correction(user_prompt: str) -> bool:
    """Detect if user prompt contains correction language."""
    prompt_lower = user_prompt.lower()
    return any(pattern.search(prompt_lower) for pattern in CORRECTION_PATTERNS)


def _detect_positive_alignment(user_prompt: str, context_line: str) -> bool:
    """Detect if user prompt builds on or aligns with context line.

    Checks for:
    - Shared entities between prompt and context
    - Follow-up questions on same topic
    - References to context-mentioned concepts
    """
    prompt_entities = _extract_entities(user_prompt)
    context_entities = _extract_entities(context_line)

    # Shared entities indicate alignment
    shared_entities = prompt_entities & context_entities

    # Require at least 1 shared entity and minimum entity quality
    return len(shared_entities) >= 1 and len(context_entities) >= 1


def _detect_feedback_with_llm(
    user_prompt: str,
    context_lines: list[str],
) -> dict[str, Any] | None:
    """Use Claude Haiku to detect feedback signals when regex is uncertain.

    Only called when regex finds no clear contradictions/corrections but context
    was injected. This catches nuanced references, contradictions without explicit
    negation keywords, and implicit alignments.

    Returns None if Haiku fails or is unavailable (fallback to regex result).
    """
    try:
        client = get_haiku_client()
        if not client:
            return None

        # Build context summary (max 5 lines to stay under token limit)
        context_summary = "\n".join(context_lines[:5])
        if len(context_lines) > 5:
            context_summary += f"\n... ({len(context_lines) - 5} more lines)"

        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Analyze if this user message references or contradicts any of the provided context. "
                    f'Return ONLY JSON: {{"signals": [{{"type": "reference|contradiction|correction|alignment", "detail": "brief"}}]}}\n\n'
                    f"Context lines:\n{context_summary}\n\n"
                    f'User message: "{user_prompt[:200]}"'
                ),
            }],
        )

        raw = resp.content[0].text.strip()
        parsed = parse_llm_json(raw)
        signals = parsed.get("signals", [])

        # Convert LLM signals to feedback format
        contradictions = []
        corrections = []
        positive_count = 0

        for signal in signals:
            sig_type = signal.get("type", "").lower()
            detail = signal.get("detail", "")

            if "contradiction" in sig_type:
                # Associate with first context line as best guess
                if context_lines:
                    contradictions.append({
                        "line": context_lines[0],
                        "contradiction": detail or "User message contradicts context",
                    })
            elif "correction" in sig_type:
                if context_lines:
                    corrections.append({
                        "line": context_lines[0],
                        "correction": detail or "User message contains correction",
                    })
            elif "alignment" in sig_type or "reference" in sig_type:
                positive_count += 1

        # Only return if we found meaningful signals
        if contradictions or corrections or positive_count > 0:
            return {
                "user_contradictions": contradictions,
                "user_corrections": corrections,
                "positive_signals": positive_count,
                "negative_signals": len(contradictions) + len(corrections),
            }

        return None

    except Exception:
        # Silent fallback to regex result
        return None


# === Function 1: Detect Feedback Signals ===

def detect_feedback_signals(
    user_prompt: str,
    injected_context_lines: list[str],
) -> dict[str, Any]:
    """Detect feedback signals from user message about previous memory injection.

    Analyzes the current user message for signals that indicate whether the
    context injected in the previous turn was useful, contradicted, or ignored.

    Args:
        user_prompt: The current user message
        injected_context_lines: The context lines that were injected in the previous turn.
            Each line should be like "- user: We use JWT for auth" or
            "- assistant: Found auth module"

    Returns:
        {
            "user_contradictions": [
                {
                    "line": "user: We use JWT for auth",
                    "contradiction": "no, we don't use JWT anymore"
                }
            ],
            "user_corrections": [
                {
                    "line": "assistant: Using Redis for sessions",
                    "correction": "actually we use sessions now"
                }
            ],
            "positive_signals": int,   # Count of lines user's message aligns with
            "negative_signals": int,    # Count of contradictions + corrections
        }

    Examples:
        >>> signals = detect_feedback_signals(
        ...     "Actually, we don't use JWT anymore. We switched to sessions.",
        ...     ["- user: We use JWT for authentication"]
        ... )
        >>> signals["negative_signals"]
        1
        >>> len(signals["user_contradictions"])
        1
    """
    if not user_prompt or not injected_context_lines:
        return {
            "user_contradictions": [],
            "user_corrections": [],
            "positive_signals": 0,
            "negative_signals": 0,
        }

    contradictions: list[dict[str, str]] = []
    corrections: list[dict[str, str]] = []
    positive_count = 0

    # Check each context line for signals
    for line in injected_context_lines:
        # Strip markdown prefix if present
        clean_line = line.strip()
        if clean_line.startswith("- "):
            clean_line = clean_line[2:]

        # Detect contradiction
        contradiction_text = _detect_contradiction(user_prompt, clean_line)
        if contradiction_text:
            contradictions.append({
                "line": line,
                "contradiction": contradiction_text,
            })
            continue

        # Detect correction (softer signal)
        if _detect_correction(user_prompt):
            # Check if this line's entities appear in prompt
            if _extract_entities(clean_line) & _extract_entities(user_prompt):
                corrections.append({
                    "line": line,
                    "correction": "User message contains correction language",
                })
                continue

        # Detect positive alignment
        if _detect_positive_alignment(user_prompt, clean_line):
            positive_count += 1

    negative_count = len(contradictions) + len(corrections)

    # If regex found clear signals, return immediately (fast path)
    if negative_count > 0 or positive_count > 0:
        return {
            "user_contradictions": contradictions,
            "user_corrections": corrections,
            "positive_signals": positive_count,
            "negative_signals": negative_count,
        }

    # Regex uncertain (no signals detected) — try Haiku fallback
    llm_result = _detect_feedback_with_llm(user_prompt, injected_context_lines)
    if llm_result:
        return llm_result

    # Final fallback: return empty signals (regex found nothing)
    return {
        "user_contradictions": contradictions,
        "user_corrections": corrections,
        "positive_signals": positive_count,
        "negative_signals": negative_count,
    }


# === Function 2: Aggregate Session Feedback ===

def aggregate_session_feedback(
    total_turns: int,
    feedback_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate feedback across a session for the Stop hook.

    Computes session-level metrics from per-turn feedback signals to assess
    overall memory injection quality during the session.

    Args:
        total_turns: Total number of turns in the session
        feedback_history: List of per-turn feedback signal dicts from
            detect_feedback_signals()

    Returns:
        {
            "total_turns": int,
            "total_injections": int,              # Turns where context was injected
            "positive_signal_count": int,         # Total positive alignments
            "negative_signal_count": int,         # Total contradictions + corrections
            "contradiction_count": int,           # Contradictions only
            "correction_count": int,              # Corrections only
            "memory_reference_rate": float,       # positive / total injections
            "session_quality_score": float,       # 0.0-1.0 composite score
        }

    Examples:
        >>> feedback = [
        ...     {"positive_signals": 2, "negative_signals": 0,
        ...      "user_contradictions": [], "user_corrections": []},
        ...     {"positive_signals": 1, "negative_signals": 1,
        ...      "user_contradictions": [{"line": "...", "contradiction": "..."}],
        ...      "user_corrections": []},
        ... ]
        >>> result = aggregate_session_feedback(5, feedback)
        >>> result["memory_reference_rate"]
        0.6
    """
    if not feedback_history:
        return {
            "total_turns": total_turns,
            "total_injections": 0,
            "positive_signal_count": 0,
            "negative_signal_count": 0,
            "contradiction_count": 0,
            "correction_count": 0,
            "memory_reference_rate": 0.0,
            "session_quality_score": 0.0,
        }

    total_injections = len(feedback_history)
    positive_sum = sum(fb.get("positive_signals", 0) for fb in feedback_history)
    negative_sum = sum(fb.get("negative_signals", 0) for fb in feedback_history)

    contradiction_sum = sum(
        len(fb.get("user_contradictions", [])) for fb in feedback_history
    )
    correction_sum = sum(
        len(fb.get("user_corrections", [])) for fb in feedback_history
    )

    # Memory reference rate: what fraction of injected lines were used?
    memory_reference_rate = (
        positive_sum / total_injections if total_injections > 0 else 0.0
    )

    # Session quality score: composite metric
    # - High positive signals = good
    # - Low contradictions = good
    # - Corrections are less severe than contradictions

    if total_injections == 0:
        quality_score = 0.0
    else:
        # Base score from positive signals (0.0 to 0.7)
        positive_component = min(0.7, (positive_sum / total_injections) * 0.7)

        # Penalty from contradictions (up to -0.4)
        contradiction_penalty = min(0.4, (contradiction_sum / total_injections) * 0.4)

        # Penalty from corrections (up to -0.2)
        correction_penalty = min(0.2, (correction_sum / total_injections) * 0.2)

        # Final score: positive component - penalties, clamped to [0, 1]
        quality_score = max(0.0, min(1.0,
            positive_component - contradiction_penalty - correction_penalty + 0.3
        ))

    return {
        "total_turns": total_turns,
        "total_injections": total_injections,
        "positive_signal_count": positive_sum,
        "negative_signal_count": negative_sum,
        "contradiction_count": contradiction_sum,
        "correction_count": correction_sum,
        "memory_reference_rate": round(memory_reference_rate, 3),
        "session_quality_score": round(quality_score, 3),
    }


# === Function 3: Calculate Memory Score Updates ===

def calculate_score_updates(
    session_feedback: dict[str, Any],
    injected_memory_ids: list[str],
) -> list[dict[str, Any]]:
    """Calculate score updates for each memory based on session feedback.

    Determines how to adjust utility scores for memories that were injected
    during the session, based on aggregate feedback signals.

    Args:
        session_feedback: Aggregated feedback from aggregate_session_feedback()
        injected_memory_ids: List of memory IDs that were injected during session

    Returns:
        List of score update directives:
        [
            {
                "memory_id": str,
                "action": "promote" | "demote" | "archive" | "no_change",
                "score_delta": float,      # How much to adjust utility score
                "reason": str,             # Human-readable explanation
            }
        ]

    Scoring logic:
        - High quality score + high reference rate → promote (boost utility)
        - Low quality score + contradictions → demote or archive
        - Mixed signals → small adjustments or no change

    Examples:
        >>> feedback = {
        ...     "session_quality_score": 0.8,
        ...     "memory_reference_rate": 0.6,
        ...     "contradiction_count": 0,
        ... }
        >>> updates = calculate_score_updates(feedback, ["mem_123", "mem_456"])
        >>> updates[0]["action"]
        'promote'
    """
    if not injected_memory_ids:
        return []

    quality = session_feedback.get("session_quality_score", 0.0)
    reference_rate = session_feedback.get("memory_reference_rate", 0.0)
    contradiction_count = session_feedback.get("contradiction_count", 0)
    correction_count = session_feedback.get("correction_count", 0)
    total_injections = session_feedback.get("total_injections", 0)

    updates: list[dict[str, Any]] = []

    for memory_id in injected_memory_ids:
        # Default: no change
        action = "no_change"
        score_delta = 0.0
        reason = "Insufficient signals to adjust score"

        # High quality + high reference → promote
        if quality >= 0.7 and reference_rate >= 0.4:
            action = "promote"
            score_delta = 0.15
            reason = f"Strong positive signals (quality={quality:.2f}, ref_rate={reference_rate:.2f})"

        # Good quality + moderate reference → small boost
        elif quality >= 0.5 and reference_rate >= 0.25:
            action = "promote"
            score_delta = 0.08
            reason = f"Moderate positive signals (quality={quality:.2f}, ref_rate={reference_rate:.2f})"

        # Low quality + contradictions → demote or archive
        elif quality < 0.3 and contradiction_count > 0:
            if contradiction_count >= 2 or (contradiction_count >= 1 and total_injections <= 3):
                action = "archive"
                score_delta = -0.5
                reason = f"Multiple contradictions detected ({contradiction_count}), likely outdated"
            else:
                action = "demote"
                score_delta = -0.2
                reason = f"Contradiction detected (quality={quality:.2f})"

        # Moderate contradictions or corrections → demote
        elif contradiction_count > 0 or (correction_count >= 2):
            action = "demote"
            score_delta = -0.15
            reason = f"Negative signals (contradictions={contradiction_count}, corrections={correction_count})"

        # Low reference rate with no contradictions → small penalty
        elif reference_rate < 0.1 and total_injections >= 3:
            action = "demote"
            score_delta = -0.05
            reason = f"Low reference rate ({reference_rate:.2f}), may not be relevant"

        updates.append({
            "memory_id": memory_id,
            "action": action,
            "score_delta": round(score_delta, 3),
            "reason": reason,
        })

    return updates


# === Session Feedback State Management ===

class FeedbackTracker:
    """Tracks feedback across turns within a session.

    Stores state in a JSON file at:
    ~/.claude/hooks/.acontext_state/feedback_{session_id}.json

    State schema:
    {
        "session_id": str,
        "created_at": str (ISO timestamp),
        "total_turns": int,
        "feedback_history": [
            {
                "turn": int,
                "user_contradictions": [...],
                "user_corrections": [...],
                "positive_signals": int,
                "negative_signals": int,
                "injected_lines": [...]
            }
        ],
        "injected_memory_ids": [str]
    }
    """

    def __init__(self, session_id: str):
        """Initialize feedback tracker for a session.

        Args:
            session_id: Unique session identifier
        """
        if not session_id:
            raise ValueError("session_id is required")

        self.session_id = session_id
        self.state_file = STATE_DIR / f"feedback_{session_id}.json"
        self._ensure_state_dir()
        self._state: dict[str, Any] = self._load_state()

    def _ensure_state_dir(self) -> None:
        """Create state directory if it doesn't exist."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        """Load state from file or create new state."""
        if self.state_file.exists():
            try:
                with self.state_file.open("r", encoding="utf-8") as f:
                    state = json.load(f)
                    # Validate required fields
                    if isinstance(state, dict) and "session_id" in state:
                        return state
            except (json.JSONDecodeError, OSError):
                pass

        # Create new state
        return {
            "session_id": self.session_id,
            "created_at": self._now_iso(),
            "total_turns": 0,
            "feedback_history": [],
            "injected_memory_ids": [],
        }

    def _save_state(self) -> None:
        """Save state to file atomically."""
        tmp_file = self.state_file.with_suffix(".tmp")
        try:
            with tmp_file.open("w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except OSError as e:
            # Clean up temp file on failure
            if tmp_file.exists():
                tmp_file.unlink()
            raise OSError(f"Failed to save feedback state: {e}") from e

    def _now_iso(self) -> str:
        """Return current UTC time in ISO format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def record_turn(
        self,
        feedback_signals: dict[str, Any],
        injected_lines: list[str],
        injected_memory_ids: list[str] | None = None,
    ) -> None:
        """Record feedback from one turn.

        Args:
            feedback_signals: Output from detect_feedback_signals()
            injected_lines: Context lines that were injected
            injected_memory_ids: Optional list of memory IDs injected
        """
        self._state["total_turns"] += 1

        turn_record = {
            "turn": self._state["total_turns"],
            "user_contradictions": feedback_signals.get("user_contradictions", []),
            "user_corrections": feedback_signals.get("user_corrections", []),
            "positive_signals": feedback_signals.get("positive_signals", 0),
            "negative_signals": feedback_signals.get("negative_signals", 0),
            "injected_lines": injected_lines,
            "timestamp": self._now_iso(),
        }

        self._state["feedback_history"].append(turn_record)

        # Track memory IDs
        if injected_memory_ids:
            existing_ids = set(self._state["injected_memory_ids"])
            existing_ids.update(injected_memory_ids)
            self._state["injected_memory_ids"] = list(existing_ids)

        self._save_state()

    def get_aggregate(self) -> dict[str, Any]:
        """Get aggregated feedback for the session.

        Returns:
            Aggregated feedback dict from aggregate_session_feedback()
        """
        return aggregate_session_feedback(
            total_turns=self._state["total_turns"],
            feedback_history=self._state["feedback_history"],
        )

    def get_score_updates(self) -> list[dict[str, Any]]:
        """Get score update recommendations for all injected memories.

        Returns:
            List of score updates from calculate_score_updates()
        """
        aggregate = self.get_aggregate()
        return calculate_score_updates(
            session_feedback=aggregate,
            injected_memory_ids=self._state["injected_memory_ids"],
        )

    def cleanup(self) -> None:
        """Remove state file (call on Stop hook)."""
        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except OSError:
                pass  # Best effort cleanup


# === Main Test Block ===

def _run_tests() -> None:
    """Run comprehensive tests with example signals."""
    print("=" * 70)
    print("Acontext Feedback Module Tests")
    print("=" * 70)

    # Test 1: Detect contradiction
    print("\n[Test 1] Contradiction Detection")
    print("-" * 70)
    user_msg = "Actually, we don't use JWT anymore. We switched to sessions."
    context = ["- user: We use JWT for authentication"]
    signals = detect_feedback_signals(user_msg, context)
    print(f"User message: {user_msg}")
    print(f"Context: {context}")
    print(f"Contradictions: {len(signals['user_contradictions'])}")
    print(f"Negative signals: {signals['negative_signals']}")
    assert signals['negative_signals'] > 0, "Should detect contradiction"
    print("✓ Passed")

    # Test 2: Detect positive alignment
    print("\n[Test 2] Positive Alignment Detection")
    print("-" * 70)
    user_msg = "How do I configure the JWT secret in the auth module?"
    context = [
        "- user: We use JWT for authentication",
        "- assistant: Found auth module at src/auth.py"
    ]
    signals = detect_feedback_signals(user_msg, context)
    print(f"User message: {user_msg}")
    print(f"Context lines: {len(context)}")
    print(f"Positive signals: {signals['positive_signals']}")
    assert signals['positive_signals'] > 0, "Should detect alignment"
    print("✓ Passed")

    # Test 3: Aggregate session feedback
    print("\n[Test 3] Session Aggregation")
    print("-" * 70)
    feedback_history = [
        {"positive_signals": 2, "negative_signals": 0, "user_contradictions": [], "user_corrections": []},
        {"positive_signals": 1, "negative_signals": 0, "user_contradictions": [], "user_corrections": []},
        {"positive_signals": 0, "negative_signals": 1, "user_contradictions": [{"line": "...", "contradiction": "..."}], "user_corrections": []},
    ]
    aggregate = aggregate_session_feedback(5, feedback_history)
    print(f"Total turns: {aggregate['total_turns']}")
    print(f"Total injections: {aggregate['total_injections']}")
    print(f"Positive signals: {aggregate['positive_signal_count']}")
    print(f"Negative signals: {aggregate['negative_signal_count']}")
    print(f"Memory reference rate: {aggregate['memory_reference_rate']}")
    print(f"Quality score: {aggregate['session_quality_score']}")
    assert 0.0 <= aggregate['session_quality_score'] <= 1.0, "Quality score out of bounds"
    print("✓ Passed")

    # Test 4: Calculate score updates
    print("\n[Test 4] Score Update Calculation")
    print("-" * 70)
    session_feedback = {
        "session_quality_score": 0.75,
        "memory_reference_rate": 0.5,
        "contradiction_count": 0,
        "correction_count": 0,
        "total_injections": 3,
    }
    updates = calculate_score_updates(session_feedback, ["mem_123", "mem_456"])
    print(f"Session quality: {session_feedback['session_quality_score']}")
    print(f"Memory reference rate: {session_feedback['memory_reference_rate']}")
    print(f"Updates generated: {len(updates)}")
    for upd in updates:
        print(f"  {upd['memory_id']}: {upd['action']} ({upd['score_delta']:+.2f}) - {upd['reason']}")
    assert all(u['action'] == 'promote' for u in updates), "Should promote high-quality memories"
    print("✓ Passed")

    # Test 5: FeedbackTracker state management
    print("\n[Test 5] FeedbackTracker State Management")
    print("-" * 70)
    test_session_id = "test_session_" + str(os.getpid())
    tracker = FeedbackTracker(test_session_id)

    # Record turns
    tracker.record_turn(
        {"positive_signals": 2, "negative_signals": 0, "user_contradictions": [], "user_corrections": []},
        ["- user: We use React", "- assistant: Found React config"],
        ["mem_001", "mem_002"]
    )
    tracker.record_turn(
        {"positive_signals": 1, "negative_signals": 1, "user_contradictions": [{"line": "...", "contradiction": "..."}], "user_corrections": []},
        ["- user: Using PostgreSQL"],
        ["mem_003"]
    )

    aggregate = tracker.get_aggregate()
    print(f"Tracked turns: {aggregate['total_turns']}")
    print(f"Quality score: {aggregate['session_quality_score']}")

    updates = tracker.get_score_updates()
    print(f"Memory IDs tracked: {len(tracker._state['injected_memory_ids'])}")
    print(f"Score updates: {len(updates)}")

    # Cleanup
    tracker.cleanup()
    assert not tracker.state_file.exists(), "State file should be removed"
    print("✓ Passed")

    print("\n" + "=" * 70)
    print("All tests passed!")
    print("=" * 70)


if __name__ == "__main__":
    _run_tests()
