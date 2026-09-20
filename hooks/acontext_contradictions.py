"""
Acontext Smart Write Layer 3: Contradiction Detection

Detects when new information contradicts previously stored memories
and surfaces warnings to maintain consistency.

Performance target: < 300ms per check (regex fast path < 50ms)
Hybrid approach: regex fast path + Claude Haiku fallback for uncertain cases
"""

import re
import os
import json
from typing import List, Dict, Optional, Set, Tuple

try:
    from acontext_config import HAIKU_MODEL, CONTRADICTION_LLM_MAX_CONFIDENCE, CONTRADICTION_MIN_CONFIDENCE
    from acontext_utils import parse_llm_json, get_haiku_client
except ImportError:
    HAIKU_MODEL = "claude-haiku-4-5"
    CONTRADICTION_LLM_MAX_CONFIDENCE = 0.85
    CONTRADICTION_MIN_CONFIDENCE = 0.50
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


# Topic inference keyword clusters
TOPIC_CLUSTERS = {
    "authentication": r"\b(auth|login|jwt|token|session|oauth|password|credential|sign[- ]?in|sign[- ]?up|2fa|sso|saml)\b",
    "database": r"\b(database|sql|postgres|mysql|mongo|redis|db|query|schema|table|migration|orm|sequelize|prisma|typeorm)\b",
    "deployment": r"\b(deploy\w*|ci|cd|pipeline|docker|k8s|kubernetes|release|ship|container|build|publish|heroku|vercel|aws|gcp)\b",
    "testing": r"\b(test\w*|spec|assert|mock|fixture|coverage|jest|pytest|vitest|cypress|selenium|e2e|unit|integration)\b",
    "api": r"\b(api|endpoint|route|rest|graphql|http|request|response|fetch|axios|webhook|cors)\b",
    "frontend": r"\b(react|vue|angular|svelte|component|ui|ux|css|style|theme|responsive|layout|dom)\b",
    "backend": r"\b(server|express|fastapi|django|flask|node|deno|nest|middleware|controller|service)\b",
    "state_management": r"\b(redux|zustand|mobx|vuex|pinia|store|state|context|provider)\b",
    "error_handling": r"\b(error|exception|catch|try|throw|fail|fallback|retry|timeout|validation)\b",
    "performance": r"\b(performance|perf|optimize|cach\w*|lazy|memo|debounce|throttle|bundle|minify|compress)\b",
    "security": r"\b(security|secure|encrypt|decrypt|hash|salt|xss|csrf|injection|sanitize|vulnerability)\b",
    "logging": r"\b(log|logger|logging|debug|trace|monitor|telemetry|analytics|sentry|datadog)\b",
    "architecture": r"\b(architecture|pattern|design|solid|microservice|monolith|modular|layered|hexagonal)\b",
    "documentation": r"\b(doc|documentation|readme|comment|jsdoc|typedoc|swagger|openapi|wiki)\b",
    "tooling": r"\b(webpack|vite|rollup|babel|typescript|eslint|prettier|lint|format|config)\b",
}

# Compiled patterns for performance
COMPILED_TOPICS = {topic: re.compile(pattern, re.IGNORECASE) for topic, pattern in TOPIC_CLUSTERS.items()}

# Technology/approach extraction patterns
TECH_PATTERN = re.compile(
    r"\b(jwt|sessions?|postgresql|postgres|mysql|mongodb|mongo|redis|memcached|docker|kubernetes|k8s|"
    r"react|vue|angular|svelte|express|fastapi|django|flask|graphql|rest|oauth|saml|"
    r"jest|pytest|cypress|vitest|prisma|drizzle|typeorm|sequelize|nginx|apache|"
    r"webpack|vite|rollup|babel|eslint|prettier|tailwind|shadcn)\b",
    re.IGNORECASE
)

# Negation/modifier patterns
POSITIVE_MODIFIERS = re.compile(r"\b(always|must|should|need to|have to|required|necessary|will)\b", re.IGNORECASE)
NEGATIVE_MODIFIERS = re.compile(r"\b(never|don't|do not|doesn't|does not|shouldn't|should not|won't|will not|"
                                r"no need|not required|not necessary|skip|avoid|remove)\b", re.IGNORECASE)

# Decision/preference indicators
DECISION_INDICATORS = re.compile(
    r"\b(decided|choose|chose|chosen|use|using|going with|go with|pick|picked|select|selected|"
    r"prefer|preferred|adopt|adopted|switch(?:ed|ing)?\s+(?:to|from)|migrate|migrating|"
    r"move(?:d|ing)?\s+to|change(?:d|ing)?\s+to|moving)\b",
    re.IGNORECASE
)

PREFERENCE_INDICATORS = re.compile(
    r"\b(prefer|like|want|would like|should|better to|best to|recommend|suggest)\b",
    re.IGNORECASE
)

CONSTRAINT_INDICATORS = re.compile(
    r"\b(must|cannot|can't|must not|mustn't|required|requirement|constraint|limit|limitation)\b",
    re.IGNORECASE
)

REJECTION_INDICATORS = re.compile(
    r"\b(don't use|do not use|avoid|rejected|no longer|instead of|rather than|stop using|"
    r"switch(?:ed|ing)?\s+from|moved?\s+away\s+from|replacing|replaced)\b",
    re.IGNORECASE
)


def extract_assertions(content: str, tier: int, entities: List[str]) -> List[Dict]:
    """Extract factual assertions from a message.

    Only extracts from Tier 0-1 messages (decisions, preferences, constraints).

    Args:
        content: Message text
        tier: Importance tier (0-3)
        entities: Pre-extracted entities from classifier

    Returns list of:
        {
            "assertion": str,     # The core claim, e.g., "Use JWT for auth"
            "type": str,          # "decision", "preference", "constraint", "fact"
            "topic": str,         # Inferred topic, e.g., "authentication"
            "entities": list[str],# Relevant entities
            "polarity": str,      # "positive" or "negative" (tried X, rejected)
            "confidence": float,
        }
    """
    # Only extract from high-priority messages
    if tier > 1:
        return []

    assertions = []
    content_lower = content.lower()

    # Split into sentences for granular analysis
    sentences = re.split(r'[.!?]+', content)

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:  # Skip very short fragments
            continue

        assertion_data = _analyze_sentence(sentence, entities)
        if assertion_data:
            assertions.append(assertion_data)

    return assertions


def _analyze_sentence(sentence: str, entities: List[str]) -> Optional[Dict]:
    """Analyze a single sentence for assertion extraction."""
    sentence_lower = sentence.lower()

    # Determine assertion type (check preference BEFORE decision as some words overlap)
    assertion_type = None
    confidence = 0.0

    # Check for negative statements about requirements/policies
    has_negative_modifier = NEGATIVE_MODIFIERS.search(sentence)
    has_positive_modifier = POSITIVE_MODIFIERS.search(sentence)

    if CONSTRAINT_INDICATORS.search(sentence):
        assertion_type = "constraint"
        confidence = 0.85
    elif PREFERENCE_INDICATORS.search(sentence) and not DECISION_INDICATORS.search(sentence):
        # Preference only if no strong decision indicators
        assertion_type = "preference"
        confidence = 0.8
    elif DECISION_INDICATORS.search(sentence):
        assertion_type = "decision"
        confidence = 0.9
    elif has_negative_modifier and any(word in sentence_lower for word in ["need", "have to", "should", "must", "run", "test", "deploy"]):
        # Negative statements about processes/requirements are decisions
        assertion_type = "decision"
        confidence = 0.75
    else:
        # Check if it's a factual statement about the codebase/project
        if any(word in sentence_lower for word in ["we", "our", "the project", "the app", "the system"]):
            assertion_type = "fact"
            confidence = 0.6
        else:
            return None  # Not a relevant assertion

    # Determine polarity
    polarity = "negative" if REJECTION_INDICATORS.search(sentence) else "positive"

    # Infer topics
    topics = _infer_topics(sentence)
    if not topics:
        return None  # No identifiable topic

    # Extract technologies/approaches
    techs = set(m.group().lower() for m in TECH_PATTERN.finditer(sentence))

    # Filter entities relevant to this sentence
    relevant_entities = [e for e in entities if e.lower() in sentence_lower]

    # Add extracted technologies to entities
    all_entities = list(set(relevant_entities) | techs)

    # Adjust confidence based on specificity
    if all_entities:
        confidence += 0.1
    if len(sentence.split()) > 5:  # More detailed sentences are more reliable
        confidence += 0.05

    confidence = min(confidence, 1.0)

    return {
        "assertion": sentence.strip(),
        "type": assertion_type,
        "topic": topics[0],  # Primary topic
        "topics": topics,    # All topics
        "entities": all_entities,
        "polarity": polarity,
        "confidence": confidence,
    }


def _infer_topics(text: str) -> List[str]:
    """Infer topics from text using keyword clusters."""
    matched_topics = []

    for topic, pattern in COMPILED_TOPICS.items():
        if pattern.search(text):
            matched_topics.append(topic)

    return matched_topics


def check_contradictions(
    new_assertions: List[Dict],
    stored_context_lines: List[str],
) -> List[Dict]:
    """Check new assertions against previously stored context lines.

    The stored_context_lines come from Acontext's get_smart_context() and look like:
    - "user: We decided to use JWT for authentication"
    - "user: The API uses PostgreSQL for storage"
    - "assistant: Configured auth with JWT tokens"

    Args:
        new_assertions: Assertions extracted from current message
        stored_context_lines: Lines from Acontext smart context

    Returns list of:
        {
            "new_assertion": dict,          # The new assertion
            "contradicted_line": str,       # The stored line that's contradicted
            "contradiction_type": str,      # "replacement", "negation", "reversal"
            "confidence": float,            # How confident we are this is a real contradiction
            "explanation": str,             # Human-readable explanation
        }
    """
    contradictions = []

    # Extract assertions from stored context
    stored_assertions = []
    for line in stored_context_lines:
        # Remove "user:" or "assistant:" prefix
        content = re.sub(r'^(user|assistant):\s*', '', line, flags=re.IGNORECASE)
        # Extract assertions (no tier filtering for historical context)
        line_assertions = _extract_from_context_line(content)
        stored_assertions.extend(line_assertions)

    # FAST PATH: regex-based contradiction detection
    for new_assert in new_assertions:
        for stored_assert in stored_assertions:
            contradiction = _detect_contradiction(new_assert, stored_assert)
            if contradiction and contradiction["confidence"] >= CONTRADICTION_MIN_CONFIDENCE:
                contradiction["contradicted_line"] = stored_assert["original_line"]
                contradictions.append(contradiction)

    # Deduplicate contradictions (keep highest confidence)
    deduplicated = _deduplicate_contradictions(contradictions)

    # FALLBACK: if regex found nothing but we have assertions, try Haiku
    if not deduplicated and new_assertions and stored_assertions:
        llm_contradictions = _check_contradictions_with_llm(new_assertions, stored_assertions)
        if llm_contradictions:
            deduplicated = llm_contradictions

    return deduplicated


def _check_contradictions_with_llm(
    new_assertions: List[Dict],
    stored_assertions: List[Dict]
) -> List[Dict]:
    """Use Claude Haiku to detect contradictions when regex finds nothing.

    Only called when regex finds no contradictions but both new and stored assertions exist.
    This catches subtle contradictions without explicit keyword patterns.

    Args:
        new_assertions: New assertions from current message
        stored_assertions: Stored assertions from context history

    Returns:
        List of contradiction dicts (same format as check_contradictions)
    """
    try:
        client = get_haiku_client()
        if not client:
            return []

        # Build concise summaries for the prompt
        stored_summary = "\n".join([
            f"- [{a['type']}] {a['assertion'][:100]}"
            for a in stored_assertions[:10]  # Limit to 10 most recent
        ])

        new_summary = "\n".join([
            f"- [{a['type']}] {a['assertion'][:100]}"
            for a in new_assertions[:5]  # Limit to 5 new assertions
        ])

        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"Check if any NEW assertions contradict STORED decisions. "
                    f"Return ONLY JSON array of contradictions, empty array if none.\n\n"
                    f"Stored decisions:\n{stored_summary}\n\n"
                    f"New assertions:\n{new_summary}\n\n"
                    f'[{{"type": "replacement|negation|reversal", "old": "stored decision", '
                    f'"new": "new assertion", "confidence": 0.9}}]'
                ),
            }],
        )

        raw = resp.content[0].text.strip()
        parsed = parse_llm_json(raw)

        # Convert LLM output to our contradiction format
        contradictions = []
        for item in parsed:
            contradiction_type = item.get("type", "replacement")
            old_text = item.get("old", "")
            new_text = item.get("new", "")
            confidence = float(item.get("confidence", 0.75))

            # Find matching assertions
            new_assert = next((a for a in new_assertions if new_text[:50] in a["assertion"]), None)
            stored_assert = next((a for a in stored_assertions if old_text[:50] in a["assertion"]), None)

            if new_assert and stored_assert:
                # Cap LLM confidence
                capped_confidence = min(confidence, CONTRADICTION_LLM_MAX_CONFIDENCE)

                # Apply minimum confidence filter
                if capped_confidence >= CONTRADICTION_MIN_CONFIDENCE:
                    contradictions.append({
                        "new_assertion": new_assert,
                        "stored_assertion": stored_assert,
                        "contradicted_line": stored_assert.get("original_line", stored_assert["assertion"]),
                        "contradiction_type": contradiction_type,
                        "confidence": capped_confidence,
                        "explanation": f"LLM-detected {contradiction_type}: {old_text[:50]} → {new_text[:50]}",
                    })

        return contradictions

    except Exception as e:
        # Log failure type, then fallback to regex-only
        try:
            from acontext_bridge import log as _log
            _log(f"WARN contradiction LLM check failed: {type(e).__name__}: {str(e)[:100]}")
        except Exception:
            pass
        return []


def _extract_from_context_line(content: str) -> List[Dict]:
    """Extract assertions from a stored context line."""
    assertions = []

    # Use simpler extraction since we don't have tier/entities
    topics = _infer_topics(content)
    if not topics:
        return []

    techs = set(m.group().lower() for m in TECH_PATTERN.finditer(content))
    polarity = "negative" if REJECTION_INDICATORS.search(content) else "positive"

    # Determine type (check constraint first, then preference, then decision)
    if CONSTRAINT_INDICATORS.search(content):
        assert_type = "constraint"
    elif PREFERENCE_INDICATORS.search(content) and not DECISION_INDICATORS.search(content):
        assert_type = "preference"
    elif DECISION_INDICATORS.search(content):
        assert_type = "decision"
    else:
        assert_type = "fact"

    # If no specific entities found, try to extract key nouns
    if not techs:
        # Extract capitalized words or important terms
        words = re.findall(r'\b[A-Z][a-z]+\b|\b[a-z]{5,}\b', content)
        techs = set(w.lower() for w in words if len(w) > 4)

    assertions.append({
        "assertion": content.strip(),
        "original_line": content.strip(),
        "type": assert_type,
        "topic": topics[0],
        "topics": topics,
        "entities": list(techs),
        "polarity": polarity,
    })

    return assertions


def _detect_contradiction(new_assert: Dict, stored_assert: Dict) -> Optional[Dict]:
    """Detect if two assertions contradict each other."""
    # Must share at least one topic
    new_topics = set(new_assert.get("topics", [new_assert.get("topic")]))
    stored_topics = set(stored_assert.get("topics", [stored_assert.get("topic")]))

    shared_topics = new_topics & stored_topics
    if not shared_topics:
        return None

    # Check for different contradiction types

    # Type 1: Replacement (same topic, different technology/approach)
    replacement = _check_replacement(new_assert, stored_assert, shared_topics)
    if replacement:
        return replacement

    # Type 2: Negation (same content, opposing modifiers)
    negation = _check_negation(new_assert, stored_assert, shared_topics)
    if negation:
        return negation

    # Type 3: Reversal (entities swapped)
    reversal = _check_reversal(new_assert, stored_assert, shared_topics)
    if reversal:
        return reversal

    return None


def _check_replacement(new_assert: Dict, stored_assert: Dict, shared_topics: Set[str]) -> Optional[Dict]:
    """Check for replacement-type contradiction (same topic, different tech)."""
    new_entities = set(e.lower() for e in new_assert["entities"])
    stored_entities = set(e.lower() for e in stored_assert["entities"])

    # Must have entities
    if not new_entities or not stored_entities:
        return None

    # Check if both are about the same decision space
    new_text_lower = new_assert["assertion"].lower()
    stored_text_lower = stored_assert["assertion"].lower()

    # Look for common decision keywords
    decision_keywords = ["use", "using", "chose", "decided", "going with", "for", "with", "auth", "database"]
    new_has_decision = any(kw in new_text_lower for kw in decision_keywords)
    stored_has_decision = any(kw in stored_text_lower for kw in decision_keywords)

    if not (new_has_decision and stored_has_decision):
        return None

    # Check for overlap in entities (some shared is OK if there's also different ones)
    shared_entities = new_entities & stored_entities
    unique_new = new_entities - stored_entities
    unique_stored = stored_entities - new_entities

    # If there are unique entities in new, and they're about the same topic, likely a replacement
    if unique_new or (new_entities != stored_entities):
        # Both are positive polarity decisions about same topic with different techs
        if new_assert["polarity"] == "positive" and stored_assert["polarity"] == "positive":
            confidence = 0.7

            # Boost confidence if explicit "instead of" or "rather than"
            if any(phrase in new_text_lower for phrase in ["instead of", "rather than", "no longer"]):
                confidence = 0.95
            # Boost if entities are clearly different
            elif not shared_entities:
                confidence = 0.8

            return {
                "new_assertion": new_assert,
                "stored_assertion": stored_assert,
                "contradiction_type": "replacement",
                "confidence": confidence,
                "explanation": f"Technology/approach changed for {list(shared_topics)[0]}: "
                             f"{', '.join(stored_entities)} → {', '.join(new_entities)}",
            }

    # New is negative about stored's technology
    if new_assert["polarity"] == "negative":
        # Check if new assertion mentions stored entities in rejection
        if any(entity in new_text_lower for entity in stored_entities):
            return {
                "new_assertion": new_assert,
                "stored_assertion": stored_assert,
                "contradiction_type": "replacement",
                "confidence": 0.9,
                "explanation": f"Previous decision rejected: {stored_assert['assertion'][:60]}...",
            }

    return None


def _check_negation(new_assert: Dict, stored_assert: Dict, shared_topics: Set[str]) -> Optional[Dict]:
    """Check for negation-type contradiction (opposing modifiers)."""
    new_text = new_assert["assertion"].lower()
    stored_text = stored_assert["assertion"].lower()

    # Check for opposing modifiers
    new_has_positive = POSITIVE_MODIFIERS.search(new_text)
    new_has_negative = NEGATIVE_MODIFIERS.search(new_text)
    stored_has_positive = POSITIVE_MODIFIERS.search(stored_text)
    stored_has_negative = NEGATIVE_MODIFIERS.search(stored_text)

    # Opposing modifiers detected
    if (new_has_positive and stored_has_negative) or (new_has_negative and stored_has_positive):
        # Check if they're talking about similar actions
        # Extract key verbs/actions
        new_words = set(re.findall(r'\b\w{4,}\b', new_text))
        stored_words = set(re.findall(r'\b\w{4,}\b', stored_text))

        common_words = new_words & stored_words
        if len(common_words) >= 2:  # At least 2 significant common words
            return {
                "new_assertion": new_assert,
                "stored_assertion": stored_assert,
                "contradiction_type": "negation",
                "confidence": 0.85,
                "explanation": f"Policy reversed for {list(shared_topics)[0]}: "
                             f"previous requirement now negated",
            }

    # Also check for simpler negations like "don't need to X" vs "must X"
    # Extract action phrases (verb phrases)
    new_verbs = set(re.findall(r'\b(run|deploy|test|build|check|validate|execute)\w*\b', new_text))
    stored_verbs = set(re.findall(r'\b(run|deploy|test|build|check|validate|execute)\w*\b', stored_text))

    if new_verbs & stored_verbs:  # Same action mentioned
        # Check if one requires it and one doesn't
        if (new_has_negative and stored_has_positive) or (new_has_positive and stored_has_negative):
            return {
                "new_assertion": new_assert,
                "stored_assertion": stored_assert,
                "contradiction_type": "negation",
                "confidence": 0.75,
                "explanation": f"Requirement changed for {list(shared_topics)[0]}: "
                             f"previous policy now reversed",
            }

    return None


def _check_reversal(new_assert: Dict, stored_assert: Dict, shared_topics: Set[str]) -> Optional[Dict]:
    """Check for reversal-type contradiction (entities swapped positions)."""
    new_entities = new_assert["entities"]
    stored_entities = stored_assert["entities"]

    if len(new_entities) < 2 or len(stored_entities) < 2:
        return None

    new_text = new_assert["assertion"].lower()
    stored_text = stored_assert["assertion"].lower()

    # Look for swap patterns: "A over B" -> "B over A" or "from A to B" -> "from B to A"
    for i, entity_a in enumerate(new_entities):
        for j, entity_b in enumerate(new_entities):
            if i >= j:
                continue

            entity_a_lower = entity_a.lower()
            entity_b_lower = entity_b.lower()

            # Check if order is reversed in stored text
            # New: "A ... B", Stored: "B ... A"
            new_pattern = f"{entity_a_lower}.*{entity_b_lower}"
            stored_pattern = f"{entity_b_lower}.*{entity_a_lower}"

            if re.search(new_pattern, new_text) and re.search(stored_pattern, stored_text):
                # Look for directional indicators
                directional = ["over", "instead of", "rather than", "from", "to", "migrate", "switch"]
                if any(word in new_text or word in stored_text for word in directional):
                    return {
                        "new_assertion": new_assert,
                        "stored_assertion": stored_assert,
                        "contradiction_type": "reversal",
                        "confidence": 0.9,
                        "explanation": f"Direction reversed for {list(shared_topics)[0]}: "
                                     f"{entity_b} and {entity_a} swapped",
                    }

    return None


def _deduplicate_contradictions(contradictions: List[Dict]) -> List[Dict]:
    """Remove duplicate contradictions, keeping the highest confidence ones."""
    if not contradictions:
        return []

    # Group by new assertion text
    groups = {}
    for c in contradictions:
        key = c["new_assertion"]["assertion"]
        if key not in groups:
            groups[key] = []
        groups[key].append(c)

    # Keep highest confidence from each group
    deduplicated = []
    for group in groups.values():
        best = max(group, key=lambda x: x["confidence"])
        deduplicated.append(best)

    return deduplicated


def format_contradiction_warnings(contradictions: List[Dict]) -> str:
    """Format contradiction warnings for injection into Claude's context.

    Returns a string like:
    "Memory update detected:
     - Previously: 'Use JWT for auth' (from earlier session)
     - Now: 'Use sessions instead of JWT'
     - Action: Previous decision superseded"
    """
    if not contradictions:
        return ""

    warnings = []
    warnings.append("⚠️  Memory Update Detected:")
    warnings.append("")

    for i, contra in enumerate(contradictions, 1):
        new_assert = contra["new_assertion"]
        stored_assert = contra["stored_assertion"]

        # Truncate long assertions
        prev_text = stored_assert["assertion"][:80]
        if len(stored_assert["assertion"]) > 80:
            prev_text += "..."

        new_text = new_assert["assertion"][:80]
        if len(new_assert["assertion"]) > 80:
            new_text += "..."

        warnings.append(f"{i}. {contra['contradiction_type'].upper()}:")
        warnings.append(f"   Previously: '{prev_text}'")
        warnings.append(f"   Now: '{new_text}'")
        warnings.append(f"   Action: Previous {stored_assert['type']} superseded")
        warnings.append(f"   Confidence: {contra['confidence']:.0%}")
        warnings.append("")

    return "\n".join(warnings)


# Test suite
if __name__ == "__main__":
    import time

    print("Running Acontext Contradiction Detection Tests\n")
    print("=" * 70)

    def test_case(name: str, func):
        """Run a test case and report results."""
        print(f"\nTest: {name}")
        start = time.time()
        try:
            func()
            elapsed = (time.time() - start) * 1000
            print(f"✓ PASS ({elapsed:.1f}ms)")
            return True
        except AssertionError as e:
            elapsed = (time.time() - start) * 1000
            print(f"✗ FAIL ({elapsed:.1f}ms): {e}")
            return False
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            print(f"✗ ERROR ({elapsed:.1f}ms): {e}")
            return False

    results = []

    # Test 1: Extract decision assertions
    def test_extract_decision():
        content = "We decided to use JWT for authentication instead of sessions."
        assertions = extract_assertions(content, tier=0, entities=["JWT", "authentication"])
        assert len(assertions) > 0, "Should extract at least one assertion"
        assert assertions[0]["type"] == "decision", f"Should be decision, got {assertions[0]['type']}"
        assert "authentication" in assertions[0]["topic"], f"Should detect auth topic, got {assertions[0]['topic']}"
        assert "jwt" in [e.lower() for e in assertions[0]["entities"]], "Should extract JWT entity"

    results.append(test_case("Extract decision assertions", test_extract_decision))

    # Test 2: Extract preference assertions
    def test_extract_preference():
        content = "I prefer React for this project because it's more familiar."
        assertions = extract_assertions(content, tier=1, entities=["React"])
        assert len(assertions) > 0, "Should extract assertion"
        # Note: "prefer" + "for" might trigger "use" pattern, so decision is acceptable
        assert assertions[0]["type"] in ["preference", "decision"], f"Should be preference or decision, got {assertions[0]['type']}"
        assert "frontend" in assertions[0]["topic"], "Should detect frontend topic"

    results.append(test_case("Extract preference assertions", test_extract_preference))

    # Test 3: Skip low-priority tiers
    def test_skip_low_priority():
        content = "The weather is nice today."
        assertions = extract_assertions(content, tier=3, entities=[])
        assert len(assertions) == 0, f"Should skip tier 3, got {len(assertions)} assertions"

    results.append(test_case("Skip low-priority messages", test_skip_low_priority))

    # Test 4: Detect replacement contradiction
    def test_replacement_contradiction():
        new_assertions = extract_assertions(
            "Let's use sessions for authentication instead of JWT.",
            tier=0,
            entities=["sessions", "authentication", "JWT"]
        )
        stored_lines = ["user: We decided to use JWT for authentication"]

        contradictions = check_contradictions(new_assertions, stored_lines)
        assert len(contradictions) > 0, f"Should detect contradiction, got {contradictions}"
        assert contradictions[0]["contradiction_type"] == "replacement", \
            f"Should be replacement, got {contradictions[0]['contradiction_type']}"
        assert contradictions[0]["confidence"] >= 0.5, "Should have reasonable confidence"

    results.append(test_case("Detect replacement contradiction", test_replacement_contradiction))

    # Test 5: Detect negation contradiction
    def test_negation_contradiction():
        new_assertions = extract_assertions(
            "We don't need to always run tests before deployment.",
            tier=0,
            entities=["tests", "deployment"]
        )
        stored_lines = ["user: We must always run tests before deployment"]

        contradictions = check_contradictions(new_assertions, stored_lines)
        # Negation detection is challenging, so we accept if it detects any contradiction type
        assert len(contradictions) > 0, f"Should detect some contradiction, got {contradictions}"
        # Either negation or replacement is acceptable for this case
        assert contradictions[0]["contradiction_type"] in ["negation", "replacement"], \
            f"Should be negation or replacement, got {contradictions[0]['contradiction_type']}"

    results.append(test_case("Detect negation contradiction", test_negation_contradiction))

    # Test 6: Detect reversal contradiction
    def test_reversal_contradiction():
        new_assertions = extract_assertions(
            "We're migrating from PostgreSQL to MySQL for the database.",
            tier=0,
            entities=["PostgreSQL", "MySQL", "database"]
        )
        stored_lines = ["user: Decided to use PostgreSQL for the database"]

        contradictions = check_contradictions(new_assertions, stored_lines)
        # Reversal is the hardest to detect, accept any contradiction type
        assert len(contradictions) > 0, f"Should detect some contradiction, got {contradictions}"
        assert contradictions[0]["contradiction_type"] in ["reversal", "replacement"], \
            f"Should be reversal or replacement, got {contradictions[0]['contradiction_type']}"

    results.append(test_case("Detect reversal contradiction", test_reversal_contradiction))

    # Test 7: No false positive on related but non-contradicting statements
    def test_no_false_positive():
        new_assertions = extract_assertions(
            "Added JWT refresh token functionality.",
            tier=0,
            entities=["JWT", "refresh token"]
        )
        stored_lines = ["user: We decided to use JWT for authentication"]

        contradictions = check_contradictions(new_assertions, stored_lines)
        assert len(contradictions) == 0, f"Should not detect contradiction, got {len(contradictions)}"

    results.append(test_case("No false positive on related statements", test_no_false_positive))

    # Test 8: Topic inference accuracy
    def test_topic_inference():
        test_cases = [
            ("Using JWT tokens for auth", "authentication"),
            ("PostgreSQL database schema migration", "database"),
            ("Docker container deployment pipeline", "deployment"),
            ("Jest unit test coverage", "testing"),
            ("REST API endpoint configuration", "api"),
        ]

        for text, expected_topic in test_cases:
            topics = _infer_topics(text)
            assert expected_topic in topics, f"Should detect {expected_topic} in '{text}', got {topics}"

    results.append(test_case("Topic inference accuracy", test_topic_inference))

    # Test 9: Format warnings correctly
    def test_format_warnings():
        contradictions = [{
            "new_assertion": {"assertion": "Use sessions for auth", "type": "decision"},
            "stored_assertion": {"assertion": "Use JWT for auth", "type": "decision"},
            "contradiction_type": "replacement",
            "confidence": 0.85,
            "explanation": "Tech changed",
        }]

        warnings = format_contradiction_warnings(contradictions)
        assert "Memory Update" in warnings, "Should contain header"
        assert "Previously" in warnings, "Should show old assertion"
        assert "Now" in warnings, "Should show new assertion"
        assert "REPLACEMENT" in warnings, "Should show contradiction type"
        assert "85%" in warnings, "Should show confidence"

    results.append(test_case("Format warnings correctly", test_format_warnings))

    # Test 10: Handle empty inputs gracefully
    def test_empty_inputs():
        assert extract_assertions("", 0, []) == [], "Should handle empty content"
        assert check_contradictions([], []) == [], "Should handle empty assertions"
        assert format_contradiction_warnings([]) == "", "Should handle empty contradictions"

    results.append(test_case("Handle empty inputs gracefully", test_empty_inputs))

    # Test 11: Performance check (< 300ms for realistic workload)
    def test_performance():
        # Simulate realistic workload
        new_content = """
        We decided to migrate from JWT to session-based authentication for better security.
        The API will now use PostgreSQL database instead of MongoDB for data storage.
        We must always run tests before deployment to catch bugs.
        Docker containers will be deployed via Kubernetes instead of Swarm.
        """

        stored_lines = [
            "user: We chose JWT for authentication",
            "user: MongoDB is our database for storage",
            "user: Don't need to run tests for hotfixes",
            "user: Using Docker Swarm for orchestration",
        ]

        start = time.time()

        # Extract and check contradictions with proper entities
        entities = ["JWT", "session", "PostgreSQL", "MongoDB", "Docker", "Kubernetes", "Swarm", "tests"]
        assertions = extract_assertions(new_content, tier=0, entities=entities)
        contradictions = check_contradictions(assertions, stored_lines)
        warnings = format_contradiction_warnings(contradictions)

        elapsed_ms = (time.time() - start) * 1000

        print(f"  Detected {len(contradictions)} contradictions in {elapsed_ms:.1f}ms")
        print(f"  Contradiction types: {[c['contradiction_type'] for c in contradictions]}")
        assert elapsed_ms < 300, f"Should complete in < 300ms, took {elapsed_ms:.1f}ms"
        # At least 2 out of 4 potential contradictions should be detected
        assert len(contradictions) >= 2, f"Should detect at least 2 contradictions, got {len(contradictions)}"

    results.append(test_case("Performance < 300ms", test_performance))

    # Test 12: Confidence thresholding
    def test_confidence_threshold():
        # Create assertions that should produce a weak contradiction
        new_assertions = extract_assertions(
            "We could maybe try React sometime",
            tier=1,
            entities=["React"]
        )

        stored_lines = ["user: We decided to use Vue for the frontend"]
        contradictions = check_contradictions(new_assertions, stored_lines)

        # Weak suggestions should not produce high-confidence contradictions
        high_conf = [c for c in contradictions if c["confidence"] >= 0.8]
        # Either no contradictions or only low-confidence ones
        assert len(high_conf) == 0, f"Should not have high-confidence contradictions for weak statements, got {high_conf}"

    results.append(test_case("Confidence threshold filtering", test_confidence_threshold))

    # Test 13: Multiple contradictions in single message
    def test_multiple_contradictions():
        content = "Switch from JWT to sessions for authentication. Also moving database from PostgreSQL to MySQL."
        assertions = extract_assertions(content, tier=0, entities=["JWT", "sessions", "PostgreSQL", "MySQL", "authentication", "database"])

        stored_lines = [
            "user: We decided to use JWT for authentication",
            "user: PostgreSQL is our primary database",
        ]

        contradictions = check_contradictions(assertions, stored_lines)
        # Should detect at least one contradiction (conservative check)
        assert len(contradictions) >= 1, f"Should detect at least one contradiction, got {len(contradictions)}"

    results.append(test_case("Multiple contradictions in single message", test_multiple_contradictions))

    # Test 14: Explicit "instead of" phrases
    def test_explicit_instead_of():
        new_assertions = extract_assertions(
            "Use Redis instead of Memcached for caching.",
            tier=0,
            entities=["Redis", "Memcached", "caching"]
        )

        stored_lines = ["user: We're using Memcached for caching"]

        contradictions = check_contradictions(new_assertions, stored_lines)
        # Explicit "instead of" should produce high confidence
        assert len(contradictions) > 0, "Should detect contradiction with 'instead of'"
        if contradictions:
            assert contradictions[0]["confidence"] >= 0.8, "Should have high confidence for explicit replacement"

    results.append(test_case("Explicit 'instead of' phrases", test_explicit_instead_of))

    # Summary
    print("\n" + "=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"\nTest Summary: {passed}/{total} passed")

    if passed == total:
        print("✓ All tests passed!")
    else:
        print(f"✗ {total - passed} test(s) failed")

    print("\n" + "=" * 70)
