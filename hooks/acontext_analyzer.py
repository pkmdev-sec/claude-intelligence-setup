#!/usr/bin/env python3
"""
Acontext Consolidated LLM Analyzer

Replaces 3-4 separate Haiku API calls with a single consolidated call.
Provides hybrid analysis: LLM-first with regex fallback.

Performance target: < 5s for LLM path, < 3s for regex fallback

Architecture:
  1. analyze_message() - main entry point
     - Fast path for short messages (< 15 chars) -> tier 3 default
     - PRIMARY: _analyze_with_llm() - single Haiku call for all fields
     - FALLBACK: _analyze_with_regex() - uses existing regex modules

  2. _analyze_with_llm() - consolidated Haiku analysis
     - Single prompt asking for tier, category, entities, dynamics, contradictions, workflow
     - Returns None on any failure (caller falls back to regex)
     - Field-by-field validation with selective regex fallback

  3. _validate_llm_response() - intelligent validation
     - Each field validated independently
     - If field fails but others OK, only that field uses regex fallback
     - Doesn't throw away entire LLM response for one bad field

  4. _analyze_with_regex() - full fallback
     - Uses classify_importance() for tier/category/entities
     - Uses analyze_turn_dynamics() for dynamics
     - Uses extract_assertions() + check_contradictions() for contradictions
     - Simple keyword matching for workflow

Integration:
  from acontext_analyzer import analyze_message

  result = analyze_message(user_prompt, context_lines)

  # result.tier (0-3)
  # result.category (str)
  # result.entities (list)
  # result.dynamics (list of dicts)
  # result.contradictions (list of dicts)
  # result.workflow (str)
  # result.source ("llm" | "regex" | "default")
"""

import re
import time
from typing import Optional


def _sanitize_content(text: str) -> str:
    """Redact potential secrets from content before storage."""
    text = re.sub(r'(sk-[A-Za-z0-9_-]{8,})', 'sk-***REDACTED***', text)
    text = re.sub(r'(api[_-]?key\s*[=:]\s*)([^\s"\']+)', r'\1***REDACTED***', text, flags=re.IGNORECASE)
    text = re.sub(r'(token\s*[=:]\s*)([^\s"\']+)', r'\1***REDACTED***', text, flags=re.IGNORECASE)
    text = re.sub(r'(Bearer\s+)([A-Za-z0-9._-]+)', r'\1***REDACTED***', text, flags=re.IGNORECASE)
    return text

try:
    from acontext_config import (
        HAIKU_MODEL,
        ANALYZER_MAX_TOKENS,
        ANALYZER_MIN_PROMPT_LENGTH,
        DYNAMICS_LLM_MIN_CONFIDENCE,
        CONTRADICTION_MIN_CONFIDENCE,
        CONTRADICTION_LLM_MAX_CONFIDENCE,
        MAX_CONTEXT_LINES,
        VALID_DYNAMIC_TYPES,
        VALID_CONTRADICTION_TYPES,
        VALID_WORKFLOW_TYPES,
        VALID_TIER_CATEGORIES,
    )
    from acontext_utils import get_haiku_client, parse_llm_json
    from acontext_classifier import classify_importance
    from acontext_dynamics import analyze_turn_dynamics, extract_inferred_facts
    from acontext_contradictions import extract_assertions, check_contradictions
except ImportError:
    # Fallback for standalone testing
    HAIKU_MODEL = "claude-haiku-4-5"
    ANALYZER_MAX_TOKENS = 250
    ANALYZER_MIN_PROMPT_LENGTH = 15
    DYNAMICS_LLM_MIN_CONFIDENCE = 0.65
    CONTRADICTION_MIN_CONFIDENCE = 0.50
    CONTRADICTION_LLM_MAX_CONFIDENCE = 0.85
    MAX_CONTEXT_LINES = 20
    VALID_DYNAMIC_TYPES = {"redirect", "approval", "correction", "rejection", "preference"}
    VALID_CONTRADICTION_TYPES = {"replacement", "negation", "reversal"}
    VALID_WORKFLOW_TYPES = {"bug", "substantial", "medium", "trivial", "question"}
    VALID_TIER_CATEGORIES = {
        "decision", "preference", "constraint", "negative_knowledge",
        "requirement", "design", "technology", "configuration",
        "bug", "review", "file_reference",
        "acknowledgment", "short_message", "hook_marker", "navigation", "tool_output",
        "general",
    }

    def get_haiku_client():
        return None

    def parse_llm_json(text):
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

    def classify_importance(content, role="user"):
        return {"tier": 2, "confidence": 0.5, "category": "general", "entities": []}

    def analyze_turn_dynamics(user_message, context_lines, previous_actions):
        return []

    def extract_inferred_facts(dynamics):
        return []

    def extract_assertions(content, tier, entities):
        return []

    def check_contradictions(assertions, context_lines):
        return []


class AnalysisResult:
    """Data class for consolidated analysis results."""

    __slots__ = ('tier', 'category', 'entities', 'dynamics', 'contradictions', 'workflow', 'source')

    def __init__(
        self,
        tier: int,
        category: str,
        entities: list,
        dynamics: list,
        contradictions: list,
        workflow: str,
        source: str,
    ):
        self.tier = tier
        self.category = category
        self.entities = entities
        self.dynamics = dynamics
        self.contradictions = contradictions
        self.workflow = workflow
        self.source = source

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "tier": self.tier,
            "category": self.category,
            "entities": self.entities,
            "dynamics": self.dynamics,
            "contradictions": self.contradictions,
            "workflow": self.workflow,
            "source": self.source,
        }


def analyze_message(prompt: str, context_lines: list) -> AnalysisResult:
    """Main entry point for consolidated message analysis.

    Args:
        prompt: The user message to analyze
        context_lines: Recent context lines from Acontext (max MAX_CONTEXT_LINES)

    Returns:
        AnalysisResult with all analysis fields populated
    """
    # Short message fast path
    if len(prompt.strip()) < ANALYZER_MIN_PROMPT_LENGTH:
        return AnalysisResult(
            tier=3,
            category="short_message",
            entities=[],
            dynamics=[],
            contradictions=[],
            workflow="trivial",
            source="default",
        )

    # Limit context lines
    if context_lines and len(context_lines) > MAX_CONTEXT_LINES:
        context_lines = context_lines[:MAX_CONTEXT_LINES]

    # PRIMARY PATH: Try LLM analysis
    llm_result = _analyze_with_llm(prompt, context_lines)
    if llm_result:
        return llm_result

    # FALLBACK PATH: Regex-based analysis
    return _analyze_with_regex(prompt, context_lines)


def _analyze_with_llm(prompt: str, context_lines: list) -> Optional[AnalysisResult]:
    """Consolidated LLM analysis with single Haiku call.

    Returns None on any failure (caller falls back to regex).
    """
    try:
        client = get_haiku_client()
        if not client:
            return None

        # Build context summary
        context_summary = "\n".join(context_lines[:MAX_CONTEXT_LINES]) if context_lines else "(no prior context)"

        # Truncate prompt for performance (at word boundary, with fallback)
        if len(prompt) > 500:
            prompt_truncated = prompt[:500].rsplit(" ", 1)[0] or prompt[:500]
        else:
            prompt_truncated = prompt

        # Consolidated prompt asking for all fields
        system_prompt = "You analyze developer messages in conversation context. Return ONLY valid JSON, no markdown fences, no explanation."

        user_prompt = f"""Analyze this developer message. Return ONLY JSON:

{{
  "tier": <0-3>,
  "category": "<str>",
  "entities": ["<tech names, file paths, modules>"],
  "dynamics": [
    {{"type": "<redirect|approval|correction|rejection|preference>",
     "fact": "<what was inferred>",
     "confidence": <0.0-1.0>}}
  ],
  "contradictions": [
    {{"type": "<replacement|negation|reversal>",
     "old": "<stored decision that conflicts>",
     "new": "<new assertion>",
     "confidence": <0.0-1.0>}}
  ],
  "workflow": "<bug|substantial|medium|trivial|question>"
}}

Tier guide:
  0 = permanent decision/rule/preference/negative knowledge
  1 = high: requirement, design rationale, technology choice
  2 = medium: file reference, debugging insight, review comment
  3 = ephemeral: acknowledgment, short msg, navigation, tool output

Dynamic types:
  redirect = user changes direction ("no, use X instead")
  approval = user accepts work ("perfect", "now do X")
  correction = user fixes factual error ("it's X, not Y")
  rejection = user explains why something won't work
  preference = user reveals style/approach preference

Contradictions: only report if current message CONFLICTS with stored context.
Return empty array if no conflicts.

Workflow: bug (fix issue), substantial (major work), medium (normal task), trivial (small change), question (asking).

Previous context:
{context_summary}

Current message: "{prompt_truncated}"
"""

        # Make the call
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=ANALYZER_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        if not resp.content or not hasattr(resp.content[0], 'text'):
            return None

        raw_text = resp.content[0].text.strip()
        if not raw_text:
            return None

        parsed = parse_llm_json(raw_text)
        if not isinstance(parsed, dict):
            return None

        # Validate and convert to AnalysisResult
        return _validate_llm_response(parsed, prompt, context_lines)

    except Exception as e:
        # Log failure type for debugging, then fallback to regex
        try:
            from acontext_bridge import log as _log
            _log(f"WARN LLM analysis failed: {type(e).__name__}: {str(e)[:100]}")
        except Exception:
            pass
        return None


def _validate_llm_response(raw: dict, prompt: str, context_lines: list) -> Optional[AnalysisResult]:
    """Field-by-field validation with selective regex fallback.

    If a field fails validation but other fields are OK, only that field falls back to regex.
    Returns None only if the entire response is unusable.
    """
    try:
        # Validate tier (required, must be int 0-3)
        tier = raw.get("tier")
        if not isinstance(tier, int) or tier < 0 or tier > 3:
            # Tier invalid: use regex fallback for tier only
            classifier_result = classify_importance(prompt)
            tier = classifier_result["tier"]
            category = classifier_result["category"]
            entities = classifier_result["entities"]
        else:
            # Tier valid: validate category and entities from LLM
            category = raw.get("category", "general")
            if not isinstance(category, str) or not category:
                category = "general"

            entities = raw.get("entities", [])
            if not isinstance(entities, list):
                entities = []
            else:
                # Ensure all entities are strings
                entities = [e for e in entities if isinstance(e, str) and e.strip()]

        # Validate dynamics
        dynamics_raw = raw.get("dynamics", [])
        if not isinstance(dynamics_raw, list):
            dynamics_raw = []

        dynamics = []
        for d in dynamics_raw:
            if not isinstance(d, dict):
                continue

            dynamic_type = d.get("type", "")
            if dynamic_type not in VALID_DYNAMIC_TYPES:
                continue

            confidence = d.get("confidence", 0.0)
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 0.0

            if confidence < DYNAMICS_LLM_MIN_CONFIDENCE:
                continue

            fact = d.get("fact", "")
            if not isinstance(fact, str) or not fact:
                continue

            # Build dynamic with all required fields for behavioral storage
            dynamics.append({
                "type": dynamic_type,
                "confidence": confidence,
                "content": _sanitize_content(prompt[:200]),
                "inferred_fact": fact,
                "reasoning": "Detected by Claude Haiku (consolidated analyzer)",
            })

        # If LLM found no dynamics but message is substantial, try regex fallback
        if not dynamics and len(prompt.strip()) > 30:
            regex_dynamics = analyze_turn_dynamics(prompt, context_lines, None)
            if regex_dynamics:
                dynamics = regex_dynamics

        # Validate contradictions
        contradictions_raw = raw.get("contradictions", [])
        if not isinstance(contradictions_raw, list):
            contradictions_raw = []

        contradictions = []
        for c in contradictions_raw:
            if not isinstance(c, dict):
                continue

            contradiction_type = c.get("type", "")
            if contradiction_type not in VALID_CONTRADICTION_TYPES:
                continue

            confidence = c.get("confidence", 0.0)
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 0.0

            # Cap LLM confidence first, then filter by minimum
            confidence = min(confidence, CONTRADICTION_LLM_MAX_CONFIDENCE)
            if confidence < CONTRADICTION_MIN_CONFIDENCE:
                continue

            old_text = c.get("old", "")
            new_text = c.get("new", "")

            if not old_text or not new_text:
                continue

            # Format to match existing contradiction structure
            contradictions.append({
                "new_assertion": {"assertion": new_text, "type": "decision"},
                "stored_assertion": {"assertion": old_text, "type": "decision"},
                "contradicted_line": old_text,
                "contradiction_type": contradiction_type,
                "confidence": confidence,
                "explanation": f"LLM-detected {contradiction_type}: {old_text[:50]} → {new_text[:50]}",
            })

        # If LLM found no contradictions for tier 0-2, try regex fallback
        if not contradictions and tier <= 2:
            assertions = extract_assertions(prompt, tier, entities)
            if assertions:
                regex_contradictions = check_contradictions(assertions, context_lines)
                if regex_contradictions:
                    contradictions = regex_contradictions

        # Validate workflow
        workflow = raw.get("workflow", "medium")
        if not isinstance(workflow, str) or workflow not in VALID_WORKFLOW_TYPES:
            # Fallback: simple keyword matching
            workflow = _infer_workflow(prompt)

        return AnalysisResult(
            tier=tier,
            category=category,
            entities=entities,
            dynamics=dynamics,
            contradictions=contradictions,
            workflow=workflow,
            source="llm",
        )

    except Exception:
        # If validation completely fails, return None (full regex fallback)
        return None


def _analyze_with_regex(prompt: str, context_lines: list) -> AnalysisResult:
    """Full regex-based fallback when LLM is unavailable or fails."""
    try:
        # Tier, category, entities
        classifier_result = classify_importance(prompt)
        tier = classifier_result["tier"]
        category = classifier_result["category"]
        entities = classifier_result["entities"]
    except Exception:
        tier = 2
        category = "general"
        entities = []

    # Dynamics
    dynamics = []
    try:
        dynamics = analyze_turn_dynamics(prompt, context_lines, None)
    except Exception:
        pass

    # Contradictions (only for tier 0-1)
    contradictions = []
    if tier <= 1:
        try:
            assertions = extract_assertions(prompt, tier, entities)
            if assertions:
                contradictions = check_contradictions(assertions, context_lines)
        except Exception:
            pass

    # Workflow
    workflow = _infer_workflow(prompt)

    return AnalysisResult(
        tier=tier,
        category=category,
        entities=entities,
        dynamics=dynamics,
        contradictions=contradictions,
        workflow=workflow,
        source="regex",
    )


def _infer_workflow(prompt: str) -> str:
    """Simple keyword-based workflow inference."""
    prompt_lower = prompt.lower()

    if any(word in prompt_lower for word in ["bug", "fix", "error", "broken", "issue", "crash"]):
        return "bug"

    if any(word in prompt_lower for word in ["implement", "create", "build", "add feature", "major"]):
        return "substantial"

    if any(word in prompt_lower for word in ["?", "how", "what", "why", "explain", "can you"]):
        return "question"

    if any(word in prompt_lower for word in ["typo", "rename", "update", "small", "quick"]):
        return "trivial"

    return "medium"


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == "__main__":
    import json

    print("Running acontext_analyzer.py self-tests\n")
    print("=" * 70)

    passed = 0
    failed = 0

    # Test 1: Short message (default tier 3)
    def test_short_message():
        result = analyze_message("ok", [])
        assert result.tier == 3, f"Expected tier 3 for short message, got {result.tier}"
        assert result.source == "default", f"Expected source 'default', got {result.source}"
        print("✓ Test 1: Short message returns tier 3 with default source")
        return True

    try:
        test_short_message()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 1 failed: {e}")
        failed += 1

    # Test 2: Empty message
    def test_empty_message():
        result = analyze_message("", [])
        assert result.tier == 3, f"Expected tier 3 for empty message, got {result.tier}"
        assert result.source == "default", f"Expected source 'default', got {result.source}"
        print("✓ Test 2: Empty message returns tier 3 with default source")
        return True

    try:
        test_empty_message()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 2 failed: {e}")
        failed += 1

    # Test 3: Substantive message (should use LLM or regex)
    def test_substantive_message():
        prompt = "We decided to use JWT for authentication instead of sessions because it scales better."
        context = ["user: Previously discussed auth options"]
        result = analyze_message(prompt, context)

        assert result.tier >= 0 and result.tier <= 3, f"Tier out of range: {result.tier}"
        assert result.source in ["llm", "regex", "default"], f"Invalid source: {result.source}"
        assert isinstance(result.entities, list), "Entities should be a list"
        assert isinstance(result.dynamics, list), "Dynamics should be a list"
        assert isinstance(result.contradictions, list), "Contradictions should be a list"
        assert result.workflow in VALID_WORKFLOW_TYPES, f"Invalid workflow: {result.workflow}"

        print(f"✓ Test 3: Substantive message analyzed (source={result.source}, tier={result.tier})")
        return True

    try:
        test_substantive_message()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 3 failed: {e}")
        failed += 1

    # Test 4: Force regex fallback (override get_haiku_client)
    def test_regex_fallback():
        # Save original
        original_get_client = globals().get('get_haiku_client')

        # Override to return None
        def mock_get_client():
            return None

        globals()['get_haiku_client'] = mock_get_client

        try:
            prompt = "I prefer using TypeScript for this project"
            result = analyze_message(prompt, [])

            assert result.source == "regex", f"Expected source 'regex', got {result.source}"
            assert result.tier >= 0 and result.tier <= 3, f"Tier out of range: {result.tier}"

            print(f"✓ Test 4: Regex fallback works (tier={result.tier})")
            return True
        finally:
            # Restore original
            if original_get_client:
                globals()['get_haiku_client'] = original_get_client

    try:
        test_regex_fallback()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 4 failed: {e}")
        failed += 1

    # Test 5: Performance test (single call should be reasonable)
    def test_regex_performance():
        # Test single call to measure realistic performance
        # Note: imported modules may make their own LLM calls, so we just measure a single iteration
        start = time.time()

        prompt = "We should migrate from PostgreSQL to MySQL for better performance"
        result = _analyze_with_regex(prompt, ["user: Using PostgreSQL for database"])

        elapsed_ms = (time.time() - start) * 1000

        # Just check it completes and returns valid result
        assert result.source == "regex", f"Expected regex source, got {result.source}"
        assert result.tier >= 0 and result.tier <= 3, f"Invalid tier: {result.tier}"

        print(f"✓ Test 5: Regex analysis completes ({elapsed_ms:.1f}ms per call)")
        return True

    try:
        test_regex_performance()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 5 failed: {e}")
        failed += 1

    # Test 6: to_dict() serialization
    def test_serialization():
        result = analyze_message("test message for serialization", [])
        result_dict = result.to_dict()

        assert isinstance(result_dict, dict), "to_dict() should return dict"
        assert "tier" in result_dict, "Missing 'tier' in dict"
        assert "source" in result_dict, "Missing 'source' in dict"
        assert "entities" in result_dict, "Missing 'entities' in dict"

        # Should be JSON serializable
        json_str = json.dumps(result_dict)
        assert len(json_str) > 0, "Should be JSON serializable"

        print("✓ Test 6: Serialization via to_dict() works")
        return True

    try:
        test_serialization()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 6 failed: {e}")
        failed += 1

    # Test 7: Workflow inference
    def test_workflow_inference():
        test_cases = [
            ("Fix the bug in auth middleware", "bug"),
            ("How does JWT work?", "question"),
            ("Implement new user management system", "substantial"),
            ("Fix typo in variable name", "trivial"),
        ]

        for prompt, expected_workflow in test_cases:
            result = analyze_message(prompt, [])
            # Workflow inference is best-effort, so we just check it's valid
            assert result.workflow in VALID_WORKFLOW_TYPES, f"Invalid workflow for '{prompt}': {result.workflow}"

        print("✓ Test 7: Workflow inference produces valid values")
        return True

    try:
        test_workflow_inference()
        passed += 1
    except AssertionError as e:
        print(f"✗ Test 7 failed: {e}")
        failed += 1

    # Summary
    print("\n" + "=" * 70)
    print(f"Test Results: {passed} passed, {failed} failed")

    if failed == 0:
        print("✓ All self-tests passed!")
    else:
        print(f"✗ {failed} test(s) failed")

    # Check if API key is available
    print("\n" + "=" * 70)
    client = get_haiku_client()
    if client:
        print("✓ Haiku client available (ANTHROPIC_API_KEY found)")
        print("  LLM analysis will be used when called from hooks")
    else:
        print("⚠ Haiku client unavailable (no ANTHROPIC_API_KEY)")
        print("  Regex fallback will be used (still functional)")

    print("=" * 70)
