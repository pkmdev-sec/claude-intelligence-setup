#!/usr/bin/env python3
"""
Security Gate Hook for Claude Code
Detects dangerous command patterns and sensitive file operations.
"""

import sys
import json
import re
from typing import Dict, Any, Optional

# Dangerous patterns that should be automatically denied
DANGEROUS_BASH_PATTERNS = [
    r"\brm\s+-rf\s+/",  # rm -rf /
    r"\bmkfs\b",  # format disk
    r"\bdd\s+if=",  # raw disk write
    r":\(\)\s*\{\s*:.*\|.*\|.*\}\s*;?",  # fork bomb
]

# High-risk patterns that require user confirmation
HIGH_RISK_BASH_PATTERNS = [
    r"\brm\s+-rf\b",  # rm -rf (any path)
    r"\bsudo\b",  # sudo commands
    r"\bgit\s+push\s+--force\b",  # force push
    r"\bcurl\b.*\|\s*(?:bash|sh)\b",  # curl pipe to shell
    r"\bchmod\s+-R\s+777\b",  # open permissions
]

# Sensitive file paths that require confirmation for Read/Write/Edit
SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.env(\.|$)",  # .env files
    r"(^|/)secrets?(/|$)",  # secrets directory
    r"(^|/)credentials?(/|$)",  # credentials directory
    r"(^|/)id_rsa$",  # SSH private key
    r"\.pem$",  # certificates
    r"\.p12$",  # PKCS12
    r"\.key$",  # private keys
]


def check_dangerous_pattern(command: str) -> Optional[str]:
    """Check if command contains dangerous patterns."""
    for pattern in DANGEROUS_BASH_PATTERNS:
        if re.search(pattern, command):
            return f"Dangerous command pattern detected: {pattern}"
    return None


def check_high_risk_pattern(command: str) -> Optional[str]:
    """Check if command contains high-risk patterns."""
    for pattern in HIGH_RISK_BASH_PATTERNS:
        if re.search(pattern, command):
            return f"High-risk command pattern detected: {pattern}"
    return None


def check_sensitive_path(file_path: str) -> Optional[str]:
    """Check if file path matches sensitive patterns."""
    for pattern in SENSITIVE_PATH_PATTERNS:
        if re.search(pattern, file_path, re.IGNORECASE):
            return f"Accessing sensitive file: {file_path}"
    return None


def create_response(decision: str, reason: str) -> Dict[str, Any]:
    """Create hook response for deny/ask decisions."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
        "suppressOutput": True,
    }


def main():
    """Main security gate logic."""
    try:
        # Read event from stdin
        event_data = sys.stdin.read().strip()
        if not event_data:
            sys.exit(0)  # Empty input, allow

        event = json.loads(event_data)
        legacy_tool = event.get("tool", {})
        if not isinstance(legacy_tool, dict):
            legacy_tool = {}

        tool_name = event.get("tool_name") or legacy_tool.get("name", "")
        tool_input = event.get("tool_input") or legacy_tool.get("input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}

        # Check Bash commands
        if tool_name == "Bash":
            command = tool_input.get("command", "")

            # Check for dangerous patterns (auto-deny)
            dangerous_reason = check_dangerous_pattern(command)
            if dangerous_reason:
                response = create_response("deny", dangerous_reason)
                print(json.dumps(response))
                sys.exit(0)

            # Check for high-risk patterns (ask user)
            high_risk_reason = check_high_risk_pattern(command)
            if high_risk_reason:
                response = create_response("ask", high_risk_reason)
                print(json.dumps(response))
                sys.exit(0)

        # Check Read/Write/Edit operations on sensitive paths
        elif tool_name in ["Read", "Write", "Edit"]:
            file_path = tool_input.get("file_path", "")

            sensitive_reason = check_sensitive_path(file_path)
            if sensitive_reason:
                response = create_response("ask", sensitive_reason)
                print(json.dumps(response))
                sys.exit(0)

        # If no patterns matched, allow (return empty)
        sys.exit(0)

    except json.JSONDecodeError:
        # Invalid JSON, allow
        sys.exit(0)
    except Exception:
        # Any other error, allow (fail open)
        sys.exit(0)


if __name__ == "__main__":
    main()
