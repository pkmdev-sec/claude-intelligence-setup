# Acontext Pro Bridge for Claude Code

This hook bridge syncs Claude Code lifecycle events to Acontext via the public API.

## Required env vars

- `ACONTEXT_API_KEY` (required)
- `ACONTEXT_BASE_URL` (optional, default: `https://api.acontext.app/api/v1`)
- `ACONTEXT_USER` (optional, sets user on session create)

## Files

- `~/.claude/hooks/acontext_bridge.py`: main hook handler
- `~/.claude/hooks/acontext_bridge.sh`: shell wrapper
- `~/.claude/hooks/.acontext_state/session_map.json`: Claude session -> Acontext session mapping
- `~/.claude/hooks/.acontext_state/bridge.log`: hook runtime log

## Events wired

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `PostToolUseFailure`
- `PreCompact`
- `Stop`
- `SessionEnd`
