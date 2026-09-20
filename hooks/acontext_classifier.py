"""
Acontext Smart Write - Layer 1: Message Importance Classifier

Classifies messages into importance tiers (0-3) based on content patterns.
Designed for fast execution (<100ms) with no external dependencies.

Tier 0 (Permanent): Architectural decisions, user preferences, constraints, negative knowledge
Tier 1 (High): Requirements, design rationale, technology choices, implementation patterns
Tier 2 (Medium): File references, debugging insights, code review comments
Tier 3 (Ephemeral): Acknowledgments, tool outputs, short messages, hook markers
"""

import re
from typing import Dict, List, Tuple

try:
    from acontext_config import HAIKU_MODEL, TIER_CLASSIFIER_LLM_CONFIDENCE
    from acontext_utils import parse_llm_json, get_haiku_client
except ImportError:
    HAIKU_MODEL = "claude-haiku-4-5"
    TIER_CLASSIFIER_LLM_CONFIDENCE = 0.85
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


# Pattern definitions for tier classification
TIER_0_PATTERNS = {
    # Decision markers
    "decision": [
        r"\b(decided?|choosing|chose|will use|going with|opted for|switched to|adopting)\b",
        r"\b(final decision|ultimate choice|settling on)\b",
    ],
    # Preference markers
    "preference": [
        r"\b(prefer|always use|never use|I like|avoid|don't use|won't use)\b",
        r"\b(my preference|preferred way|standard approach)\b",
    ],
    # Constraint markers
    "constraint": [
        r"\b(must|cannot|can't|required|mandatory|limited to|constraint|restriction)\b",
        r"\b(have to|need to comply|compliance requirement)\b",
    ],
    # Negative knowledge
    "negative_knowledge": [
        r"\btried .+ but",
        r"\b(doesn't work because|won't work|abandoned|gave up on)\b",
        r"\b(blocker|blocked by|incompatible with)\b",
    ],
}

TIER_1_PATTERNS = {
    # Specification
    "requirement": [
        r"\b(requirement|feature should|needs to support|specification|spec)\b",
        r"\b(user story|acceptance criteria|must have|should have)\b",
    ],
    # Design
    "design": [
        r"\b(architecture|design pattern|structure|approach|strategy)\b",
        r"\b(framework|system design|component|module design)\b",
    ],
    # Technology choice
    "technology": [
        r"\b(using \w+ for|migrating from .+ to|chose .+ over)\b",
        r"\b(technology stack|tech choice|library selection)\b",
    ],
    # Configuration
    "configuration": [
        r"\b(configured|configuration|setting|environment variable|env var)\b",
        r"\b(setup|initialization|bootstrapped)\b",
    ],
}

TIER_2_PATTERNS = {
    # Bug analysis
    "bug": [
        r"\b(bug|issue|root cause|regression|error|exception)\b",
        r"\b(crash|failure|breaking|broken)\b",
    ],
    # Code review
    "review": [
        r"\b(code review|review comment|feedback|suggestion)\b",
        r"\b(refactor|improvement|optimization)\b",
    ],
}

# Tier 3 auto-classify patterns
ACKNOWLEDGMENT_PATTERN = re.compile(
    r"^(ok|okay|thanks?|thank you|got it|sure|yes|no|yep|nope|k)\.?$",
    re.IGNORECASE
)

HOOK_MARKER_PATTERN = re.compile(r"^\[HOOK\]", re.IGNORECASE)

NAVIGATION_PATTERN = re.compile(
    r"^(let me|checking|looking at|reading|analyzing|running)\b",
    re.IGNORECASE
)

# Entity extraction patterns
FILE_PATH_PATTERN = re.compile(
    r"(?:(?:[a-zA-Z]:)?[/\\~.][\w/\\\-\.]+\.\w+)|(?:src/[\w/\-\.]+)|(?:\.{1,2}/[\w/\-\.]+)"
)

TECHNOLOGY_PATTERN = re.compile(
    r"\b(JWT|OAuth|PostgreSQL|MySQL|SQLite|Redis|MongoDB|"
    r"React|Vue|Angular|Svelte|Next\.js|Nuxt|"
    r"Python|JavaScript|TypeScript|Go|Rust|Java|C\+\+|Ruby|PHP|"
    r"Docker|Kubernetes|AWS|GCP|Azure|"
    r"Git|GitHub|GitLab|Bitbucket|"
    r"Node\.js|Express|FastAPI|Django|Flask|Rails|"
    r"GraphQL|REST|gRPC|WebSocket|"
    r"Webpack|Vite|Rollup|esbuild|"
    r"Jest|Pytest|Mocha|Cypress|Playwright)\b"
)

MODULE_PATTERN = re.compile(r"\b[a-z_]+\.[a-z_]+(?:\.[a-z_]+)*\b")

# Justification markers for tier 0
JUSTIFICATION_PATTERN = re.compile(
    r"\b(because|since|to enable|in order to|so that|for .+ purposes?)\b",
    re.IGNORECASE
)


def classify_importance(content: str, role: str = "user") -> Dict:
    """
    Classify message importance into tiers 0-3.

    Args:
        content: The message content to classify
        role: The message role ("user", "assistant", "system")

    Returns:
        Dictionary with:
            - tier: int (0-3, where 0 is most important)
            - confidence: float (0.0-1.0)
            - category: str (e.g., "decision", "preference", "acknowledgment")
            - entities: list[str] (extracted tech names, file paths, modules)

    Examples:
        >>> classify_importance("I prefer using PostgreSQL for this project")
        {'tier': 0, 'confidence': 0.95, 'category': 'preference', 'entities': ['PostgreSQL']}

        >>> classify_importance("ok")
        {'tier': 3, 'confidence': 1.0, 'category': 'acknowledgment', 'entities': []}
    """
    content_lower = content.lower()
    content_len = len(content.strip())

    # Extract entities first (used across tiers)
    entities = _extract_entities(content)

    # Tier 3: Auto-classify ephemeral content
    tier_3_result = _check_tier_3(content, content_lower, content_len)
    if tier_3_result:
        return {**tier_3_result, "entities": entities}

    # Check tiers 0-2 with pattern matching
    tier_0_result = _check_tier_0(content, content_lower, entities)
    if tier_0_result:
        return {**tier_0_result, "entities": entities}

    tier_1_result = _check_tier_1(content_lower)
    if tier_1_result:
        return {**tier_1_result, "entities": entities}

    tier_2_result = _check_tier_2(content, content_lower, entities)
    if tier_2_result:
        return {**tier_2_result, "entities": entities}

    # Regex uncertain — try Claude Haiku for accurate classification
    llm_result = _classify_with_llm(content, entities)
    if llm_result:
        return llm_result

    # Final fallback: Tier 2 with low confidence
    return {
        "tier": 2,
        "confidence": 0.5,
        "category": "general",
        "entities": entities,
    }


def _check_tier_3(content: str, content_lower: str, content_len: int) -> Dict | None:
    """Check if content matches Tier 3 (ephemeral) patterns."""

    # Very short messages
    if content_len < 15:
        return {
            "tier": 3,
            "confidence": 0.95,
            "category": "short_message",
        }

    # Pure acknowledgments
    if ACKNOWLEDGMENT_PATTERN.match(content.strip()):
        return {
            "tier": 3,
            "confidence": 1.0,
            "category": "acknowledgment",
        }

    # Hook markers
    if HOOK_MARKER_PATTERN.match(content):
        return {
            "tier": 3,
            "confidence": 1.0,
            "category": "hook_marker",
        }

    # Large code blocks
    code_blocks = re.findall(r"```[\s\S]*?```", content)
    if code_blocks and sum(len(block) for block in code_blocks) > 300:
        return {
            "tier": 3,
            "confidence": 0.9,
            "category": "tool_output",
        }

    # Navigation phrases
    if NAVIGATION_PATTERN.match(content.strip()):
        return {
            "tier": 3,
            "confidence": 0.85,
            "category": "navigation",
        }

    return None


def _check_tier_0(content: str, content_lower: str, entities: List[str]) -> Dict | None:
    """Check if content matches Tier 0 (permanent) patterns."""

    matches: List[Tuple[str, float]] = []

    # Check all Tier 0 pattern categories
    for category, patterns in TIER_0_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content_lower):
                matches.append((category, 0.9))
                break  # One match per category is enough

    if not matches:
        return None

    # Boost confidence if content has justification + entities
    confidence = matches[0][1]
    has_justification = bool(JUSTIFICATION_PATTERN.search(content_lower))
    has_entities = len(entities) > 0

    if has_justification and has_entities:
        confidence = min(0.98, confidence + 0.08)
    elif has_justification or has_entities:
        confidence = min(0.95, confidence + 0.05)

    return {
        "tier": 0,
        "confidence": confidence,
        "category": matches[0][0],
    }


def _check_tier_1(content_lower: str) -> Dict | None:
    """Check if content matches Tier 1 (high importance) patterns."""

    for category, patterns in TIER_1_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content_lower):
                return {
                    "tier": 1,
                    "confidence": 0.85,
                    "category": category,
                }

    return None


def _check_tier_2(content: str, content_lower: str, entities: List[str]) -> Dict | None:
    """Check if content matches Tier 2 (medium importance) patterns."""

    # Check bug/review patterns
    for category, patterns in TIER_2_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content_lower):
                return {
                    "tier": 2,
                    "confidence": 0.8,
                    "category": category,
                }

    # File references (if entities contain file paths)
    if any("/" in e or "\\" in e or "." in e for e in entities):
        return {
            "tier": 2,
            "confidence": 0.75,
            "category": "file_reference",
        }

    return None


def _extract_entities(content: str) -> List[str]:
    """
    Extract entities from content: tech names, file paths, module names.

    Returns:
        List of unique entity strings found in the content
    """
    entities = set()

    # Extract file paths
    file_paths = FILE_PATH_PATTERN.findall(content)
    entities.update(file_paths)

    # Extract technology names
    tech_names = TECHNOLOGY_PATTERN.findall(content)
    entities.update(tech_names)

    # Extract module names (but filter out common words)
    module_names = MODULE_PATTERN.findall(content)
    # Only keep if they look like actual modules (contain at least one dot and reasonable length)
    entities.update(m for m in module_names if 5 <= len(m) <= 50)

    return sorted(entities)


# Test cases
if __name__ == "__main__":
    test_cases = [
        # Tier 0 examples
        ("I decided to use PostgreSQL for this project because it has better JSON support", 0),
        ("Never use eval() in production code", 0),
        ("We must comply with GDPR data retention policies", 0),
        ("Tried using Redis for caching but it kept timing out", 0),
        ("My preference is to always use TypeScript over JavaScript", 0),

        # Tier 1 examples
        ("The feature should support real-time updates via WebSocket", 1),
        ("Using FastAPI for the backend architecture", 1),
        ("Requirement: user authentication needs to support OAuth", 1),
        ("Configured environment variables for AWS credentials", 1),

        # Tier 2 examples
        ("Found a bug in src/auth/middleware.ts at line 42", 2),
        ("Code review comment: this function could be optimized", 2),
        ("The issue is caused by a race condition in the event handler", 2),

        # Tier 3 examples
        ("ok", 3),
        ("thanks", 3),
        ("Let me check the logs", 3),
        ("[HOOK] Pre-message processing", 3),
        ("hi", 3),
    ]

    print("Running Acontext Classifier Tests\n" + "=" * 60)

    passed = 0
    failed = 0

    for content, expected_tier in test_cases:
        result = classify_importance(content)
        actual_tier = result["tier"]
        status = "PASS" if actual_tier == expected_tier else "FAIL"

        if status == "PASS":
            passed += 1
        else:
            failed += 1

        print(f"\n[{status}] Expected Tier {expected_tier}, Got Tier {actual_tier}")
        print(f"Content: {content[:80]}")
        print(f"Result: {result}")

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(test_cases)} tests")

def _classify_with_llm(content: str, entities: list) -> Dict | None:
    """Use Claude Haiku to classify messages regex couldn't handle confidently.

    Only called when regex falls through to default Tier 2 (uncertain).
    This catches the ~50% of messages that contain real decisions
    without explicit keywords like 'decided' or 'prefer'.
    """
    try:
        client = get_haiku_client()
        if not client:
            return None

        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f'Classify this software project message. Return ONLY JSON: '
                    f'{{"tier":N,"cat":"category"}}\n'
                    f'Tier 0=permanent decision/rule, 1=high (requirement/location), '
                    f'2=medium (context/finding), 3=ephemeral (ack/short)\n'
                    f'Message: "{content[:200]}"'
                ),
            }],
        )

        raw = resp.content[0].text.strip()
        parsed = parse_llm_json(raw)

        tier = int(parsed.get("tier", 2))
        category = str(parsed.get("cat", "general"))

        return {
            "tier": max(0, min(3, tier)),
            "confidence": TIER_CLASSIFIER_LLM_CONFIDENCE,
            "category": category,
            "entities": entities,
        }
    except Exception:
        return None
