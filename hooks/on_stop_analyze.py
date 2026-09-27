"""
hooks/on_stop_analyze.py
--------------------------
Stop hook (original IBM Bob design). Spawns analyzer/analyze_session.py for
the session that just finished, as a detached subprocess (stdin closed so
it doesn't inherit the hook's own stdin pipe).

This file already didn't care which assistant called it — it only ever read
session_id — so Bob, Claude Code and Codex CLI all use it completely
unmodified, just wired up from their own settings file (.bob/settings.json,
.claude/settings.json, .codex/hooks.json). The BOB_MONITOR_ASSISTANT
environment variable set by those configs is inherited automatically by the
subprocess.run() call below and by analyzer/analyze_session.py in turn.

dispatch() is exported so hooks/gemini_adapter.py can call it directly once
it has confirmed the incoming event really is a turn-end event (Gemini CLI
uses a different event name than "Stop").
"""

import subprocess
import sys
import json


def dispatch(payload):
    """Spawn analyzer/analyze_session.py for the session_id in `payload`."""
    session_id = payload.get("session_id", "")
    args = ["python3", "analyzer/analyze_session.py"]
    if session_id:
        args.append(session_id)
    subprocess.run(args, stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}
    dispatch(payload)
