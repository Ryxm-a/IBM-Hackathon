"""
hooks/log_tool_use.py
-----------------------
PostToolUse hook (original IBM Bob design). Appends one record per tool call
to sample-sessions/<session_id>.jsonl.

Unchanged behavior from the original, plus one addition: every entry is now
tagged with an "assistant" field, read from the BOB_MONITOR_ASSISTANT
environment variable (defaults to "bob" if unset, so existing Bob-only setups
need zero changes). Each assistant's hook config sets this variable to its
own name before invoking this script — see .bob/settings.json,
.claude/settings.json and .codex/hooks.json in this repo. Gemini CLI reuses
the log_entry() function below through hooks/gemini_adapter.py instead of
calling this file directly, because its tool-call event has a different name
(AfterTool, not PostToolUse) and needs a small translation step first.

log_entry() is exported so it can be reused by gemini_adapter.py without
duplicating the file-writing logic.
"""

import sys, json, os
from datetime import datetime, timezone

ASSISTANT = os.environ.get("BOB_MONITOR_ASSISTANT", "bob")


def log_entry(payload):
    """Append one normalized tool-call record for `payload`. Bob, Claude Code
    and Codex CLI all send a PostToolUse payload shaped the same way
    (session_id, tool_name, tool_input, tool_response), so this same function
    handles all three untouched."""
    session_id = payload.get("session_id", "unknown")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("sample-sessions", exist_ok=True)
    with open(f"sample-sessions/{session_id}.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "assistant": ASSISTANT, "payload": payload}) + "\n")


def main():
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return
    log_entry(payload)


if __name__ == "__main__":
    main()
