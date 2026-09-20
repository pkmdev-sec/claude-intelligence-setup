#!/usr/bin/env python3
"""Contract tests for the Claude Code PreToolUse security gate."""

import json
import subprocess
import unittest
from pathlib import Path


HOOK = Path(__file__).with_name("security_gate.py")


def run_gate(payload):
    result = subprocess.run(
        ["python3", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    output = json.loads(result.stdout) if result.stdout.strip() else None
    return result.returncode, output


class SecurityGateContractTest(unittest.TestCase):
    def test_denies_dangerous_standard_hook_payload(self):
        returncode, output = run_gate(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            }
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

    def test_asks_before_standard_sensitive_file_access(self):
        _, output = run_gate(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/.env"},
            }
        )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "ask",
        )

    def test_safe_standard_payload_has_no_decision(self):
        returncode, output = run_gate(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/README.md"},
            }
        )

        self.assertEqual(returncode, 0)
        self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
