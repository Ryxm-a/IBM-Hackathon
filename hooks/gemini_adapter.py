"""
hooks/gemini_adapter.py
-------------------------
Gemini CLI is the one assistant of the four this project supports whose hook
lifecycle doesn't line up closely enough to reuse hooks/log_tool_use.py and
hooks/on_stop_analyze.py directly:

  - Its tool-call event is named "AfterTool" (Bob/Claude Code/Codex CLI all
    call theirs "PostToolUse").
  - Its turn-end event uses Gemini's own naming rather than "Stop".
  - Field *contents* (tool_name, tool_input, session_id) line up closely
    enough with the other three assistants that no field-by-field mapping is
    needed there — only the event name has to be identified before deciding
    what to do with the payload.

This one file does that one job: read hook_event_name, decide whether this
is a completed tool call or an ended turn, and forward the payload to the
same log_entry() / dispatch() functions Bob/Claude/Codex already use. No
detection logic is duplicated.

Gemini CLI's hook system is newer than the other three and event names can
change between versions. If the string checks below don't match what your
installed CLI actually sends, run `gemini hooks list` (or check whatever
your version's docs call it) and update _looks_like_tool_event /
_looks_like_turn_end — that is the only place any Gemini-specific naming
lives in this project.
"""

import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BOB_MONITOR_ASSISTANT", "gemini-cli")

import log_tool_use       # noqa: E402  (path insert must happen first)
import on_stop_analyze     # noqa: E402


def _looks_like_tool_event(name):
    name = (name or "").lower()
    return "tool" in name and "after" in name


def _looks_like_turn_end(name):
    name = (name or "").lower()
    return any(marker in name for marker in ("afteragent", "stop", "turnend", "sessionend"))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    event = payload.get("hook_event_name", "")

    if _looks_like_tool_event(event):
        log_tool_use.log_entry(payload)
    elif _looks_like_turn_end(event):
        on_stop_analyze.dispatch(payload)
    # Anything else (BeforeTool, SessionStart, etc.) is intentionally ignored
    # — this monitor only cares about a completed tool call and a finished turn.


if __name__ == "__main__":
    main()
