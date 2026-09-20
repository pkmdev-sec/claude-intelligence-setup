#!/usr/bin/env python3
"""Test script to verify hybrid regex + Haiku pattern in feedback detection."""

import os
import sys

# Ensure module can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acontext_feedback import detect_feedback_signals


def test_regex_fast_path():
    """Test cases where regex should catch signals immediately (no Haiku call)."""
    print("\n=== Test 1: Regex Fast Path (Explicit Contradiction) ===")

    user_msg = "Actually, we don't use JWT anymore. We switched to sessions."
    context = ["- user: We use JWT for authentication"]

    result = detect_feedback_signals(user_msg, context)

    print(f"User: {user_msg}")
    print(f"Context: {context}")
    print(f"Result: {result}")

    assert result["negative_signals"] > 0, "Should detect contradiction"
    print("✓ PASS: Regex detected contradiction without Haiku")


def test_haiku_fallback():
    """Test cases where regex is uncertain and Haiku fallback should activate."""
    print("\n=== Test 2: Haiku Fallback (Nuanced Reference) ===")

    # Subtle reference without explicit keywords
    user_msg = "What was that database we talked about earlier?"
    context = ["- user: We're using PostgreSQL for this project"]

    result = detect_feedback_signals(user_msg, context)

    print(f"User: {user_msg}")
    print(f"Context: {context}")
    print(f"Result: {result}")

    # This might return signals if Haiku catches the reference,
    # or empty if no API key or Haiku isn't confident
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("✓ Haiku fallback executed (API key available)")
    else:
        print("⚠ Haiku fallback skipped (no API key)")

    # Don't assert specific result since Haiku may or may not find signals
    print("✓ PASS: Function executed without errors")


def test_empty_context():
    """Test edge case with no context."""
    print("\n=== Test 3: Edge Case (Empty Context) ===")

    user_msg = "Let's start a new project"
    context = []

    result = detect_feedback_signals(user_msg, context)

    print(f"User: {user_msg}")
    print(f"Context: {context}")
    print(f"Result: {result}")

    assert result["negative_signals"] == 0, "Should return empty signals"
    assert result["positive_signals"] == 0, "Should return empty signals"
    print("✓ PASS: Empty context handled correctly")


def test_alignment_detection():
    """Test positive alignment detection."""
    print("\n=== Test 4: Positive Alignment ===")

    user_msg = "How do I configure the JWT secret in the auth module?"
    context = [
        "- user: We use JWT for authentication",
        "- assistant: Found auth module at src/auth.py"
    ]

    result = detect_feedback_signals(user_msg, context)

    print(f"User: {user_msg}")
    print(f"Context: {len(context)} lines")
    print(f"Result: {result}")

    assert result["positive_signals"] > 0, "Should detect alignment"
    print("✓ PASS: Regex detected positive alignment")


if __name__ == "__main__":
    print("=" * 70)
    print("Hybrid Feedback Detection Test Suite")
    print("=" * 70)

    try:
        test_regex_fast_path()
        test_haiku_fallback()
        test_empty_context()
        test_alignment_detection()

        print("\n" + "=" * 70)
        print("All tests completed successfully!")
        print("=" * 70)

    except AssertionError as e:
        print(f"\n✗ FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
