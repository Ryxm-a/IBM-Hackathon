"""
analyzer/analyze_session.py
----------------------------
Core of the (originally IBM Bob-only, now multi-assistant) Session Health
Monitor. Everything in this file — the three detection heuristics, the
health-log format, the dashboard activity feed — is IBM Bob's original
design. Two things were added on top of that foundation for this build:

1. Assistant awareness. Every logged entry now carries an "assistant" tag
   ("bob", "claude-code", "codex-cli", "gemini-cli") set by whichever hook
   script wrote it, via the BOB_MONITOR_ASSISTANT environment variable. This
   file reads that tag off the session's entries and threads it through to
   last_verdict.txt / health_log.jsonl / dashboard/activity.json, so a mixed
   history (e.g. you use Bob for one repo and Claude Code for another) stays
   distinguishable.

2. A hardcoded rules engine (analyzer/rules.py) replaces the default call to
   `bob -p` for turning flags into a verdict. See rules.py's module
   docstring for the full reasoning. The old behavior — call out to Bob
   in a recursive inner session for an LLM-generated assessment — still
   exists below (ask_bob_judge / _bob_executable) and can be switched back
   on by setting BOB_MONITOR_AI_JUDGE=1 in the environment before this
   script runs. It is off by default.
"""

import json
import glob
import subprocess
import os
import sys
import shutil
import re
from collections import Counter
from datetime import datetime, timezone

import rules

TRANSCRIPT_DIR = "sample-sessions"
VERDICT_FILE = "analyzer/last_verdict.txt"
HEALTH_LOG = "analyzer/health_log.jsonl"
ACTIVITY_FILE = "dashboard/activity.json"  # written for dashboard; not used by hooks

# Off by default. Set BOB_MONITOR_AI_JUDGE=1 to restore the original
# behavior of asking `bob -p` for a verdict instead of using rules.py.
USE_AI_JUDGE = os.environ.get("BOB_MONITOR_AI_JUDGE", "").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------

def load_transcript(session_id=None):
    def _parse(path):
        entries = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # skip corrupt lines
        return entries

    if session_id:
        path = f"{TRANSCRIPT_DIR}/{session_id}.jsonl"
        if not os.path.exists(path):
            return []
        return _parse(path)

    # Fallback: use the most recently modified file
    files = sorted(glob.glob(f"{TRANSCRIPT_DIR}/*.jsonl"), key=os.path.getmtime)
    if not files:
        return []
    return _parse(files[-1])


def session_assistant(entries):
    """Which coding assistant produced this session's log. Every entry is
    tagged at write time by hooks/log_tool_use.py (or hooks/gemini_adapter.py
    for Gemini CLI); older logs without the tag default to "bob"."""
    for e in entries:
        tag = e.get("assistant")
        if tag:
            return tag
    return "bob"


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

def check_repetition(entries):
    """Flags if the same file gets written 3+ times recently."""
    paths = [
        e["payload"].get("tool_input", {}).get("path")
        for e in entries[-15:]
        if e["payload"].get("tool_name") == "write_file"
    ]
    counts = Counter([p for p in paths if p])
    for path, count in counts.items():
        if count >= 3:
            return {
                "type": "repetition",
                "path": path,
                "count": count,
                "message": f"File '{path}' was written {count} times in the last 15 actions — possible loop.",
            }
    return None


def check_runaway(entries):
    """Flags if there's a huge number of tool calls with no stop in between."""
    if len(entries) >= 25:
        return {
            "type": "runaway",
            "total": len(entries),
            "message": f"{len(entries)} tool calls in this session with no break — possible runaway session.",
        }
    return None


# Tools whose presence between two identical error responses counts as a "fix attempt"
_FIX_TOOLS = {"write_file", "apply_diff", "search_and_replace", "insert_content", "execute_command"}

# Words/phrases that suggest a tool_response is actually reporting a failure,
# rather than a normal success message that merely happens to repeat.
_ERROR_MARKERS = (
    "error", "exception", "traceback", "failed", "failure",
    "not found", "no such file", "denied", "invalid",
    "cannot", "can't", "unable to", "syntax error", "undefined",
)


def _error_signature(tool_response):
    """Normalised, truncated fingerprint of a tool response string."""
    return re.sub(r"\s+", " ", tool_response.strip())[:200]


def _looks_like_error(signature):
    """Heuristic: does this response text actually read like a failure/error?"""
    sig = signature.lower()
    return any(marker in sig for marker in _ERROR_MARKERS)


def check_recurring_error(entries):
    """Flags when the same *error-looking* tool_response reappears after Bob attempted a fix.

    Only responses that look like an actual error are considered — a benign
    message that happens to repeat (e.g. two identical "ok" confirmations)
    must not be flagged as a recurring error.
    """
    responses = [
        (i, _error_signature(e["payload"].get("tool_response", "")),
         e["payload"].get("tool_name", ""))
        for i, e in enumerate(entries)
        if e["payload"].get("tool_response", "").strip()
        and _looks_like_error(e["payload"].get("tool_response", ""))
    ]

    for a in range(len(responses) - 1):
        idx_a, sig_a, _ = responses[a]
        for b in range(a + 1, len(responses)):
            idx_b, sig_b, tool_b = responses[b]
            if sig_a != sig_b:
                continue
            intervening = [
                e["payload"].get("tool_name", "")
                for e in entries[idx_a + 1:idx_b]
            ]
            if any(t in _FIX_TOOLS for t in intervening):
                sig_display = f"{sig_a[:80]}{'...' if len(sig_a) > 80 else ''}"
                return {
                    "type": "recurring_error",
                    "tool": tool_b,
                    "error": sig_display,
                    "message": (
                        f"Tool '{tool_b}' returned the same error after a fix was attempted — "
                        f"possible unfixed loop. Error: \"{sig_display}\""
                    ),
                }
    return None


# ---------------------------------------------------------------------------
# Health score (transparent model — not an official IBM metric)
# ---------------------------------------------------------------------------

_SCORE_WEIGHTS = {"runaway": 30, "recurring_error": 25, "repetition": 20}


def compute_score(flags_struct):
    """
    Score 0-100 based on detected signals. Not an official IBM metric.
    Starts at 100 and deducts per flag:
      - Runaway session: -30
      - Recurring error: -25
      - Repeated file writes: -20
    """
    score = 100
    for flag in flags_struct:
        score -= _SCORE_WEIGHTS.get(flag["type"], 15)
    return max(0, score)


# ---------------------------------------------------------------------------
# Optional AI judge (off by default — see USE_AI_JUDGE above)
# ---------------------------------------------------------------------------

def _bob_executable():
    """Return the bob executable name, preferring bob.cmd on Windows."""
    for name in ("bob.cmd", "bob.ps1", "bob"):
        if shutil.which(name):
            return name
    return "bob"


def ask_bob_judge(flags, entries):
    """Call `bob -p` (well, `bob run`) to get an LLM-generated assessment of
    the flagged session. Only used when BOB_MONITOR_AI_JUDGE=1 is set; by
    default rules.assess() handles this instead. See rules.py's docstring
    for why. Kept here, unmodified from the original implementation, so
    nobody who liked the old behavior loses it."""
    summary_lines = []
    for e in entries[-10:]:
        p = e["payload"]
        summary_lines.append(f"  {e['ts']} {p.get('tool_name', '?')}")
    summary = "\n".join(summary_lines)

    flags_text = "\n".join(f"- {f}" for f in flags)

    prompt = (
        "An automated session health monitor flagged this coding-assistant session. "
        "Flags raised:\n" + flags_text
        + "\n\nLast 10 tool calls (timestamp + tool name):\n" + summary
        + "\n\nReply with ONLY a JSON object:\n"
        '{"status":"degraded","reason":"<short explanation>","recommended_action":'
        '"new_session or narrower_prompt or add_verification or rollback",'
        '"suggested_prompt":"<ready-to-use prompt for a fresh session>"}'
    )

    bob = _bob_executable()
    try:
        import time
        proc = subprocess.Popen(
            [bob, "run", "--format", "json", "--log-level", "silent", "--max-turns", "2", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        verdict = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                text = obj.get("last_message") or obj.get("result") or obj.get("content") or obj.get("message") or ""
                if text and "maximum" not in text.lower() and "turn" not in text.lower():
                    candidates = [t.strip() for t in text.splitlines() if t.strip().startswith("{")]
                    verdict = candidates[-1] if candidates else text.strip()
                    break
            except (json.JSONDecodeError, AttributeError):
                continue
        proc.kill()
        proc.wait()
        if verdict:
            return verdict
        return json.dumps({"status": "unknown", "reason": "empty response", "recommended_action": "none", "suggested_prompt": ""})
    except Exception as e:
        return json.dumps({"status": "unknown", "reason": str(e), "recommended_action": "none", "suggested_prompt": ""})


def parse_assessment(verdict_raw):
    """
    Only used on the AI-judge path (BOB_MONITOR_AI_JUDGE=1), to make sense of
    whatever text `bob -p` returned. The default rules.py path already
    returns structured fields directly and never touches this.
    """
    if not verdict_raw or verdict_raw == "healthy":
        return "healthy", "", "", ""

    try:
        obj = json.loads(verdict_raw)
        if isinstance(obj, dict):
            return (
                obj.get("status", ""),
                obj.get("reason", ""),
                obj.get("recommended_action", ""),
                obj.get("suggested_prompt", ""),
            )
    except (json.JSONDecodeError, ValueError):
        pass

    match = re.search(r'\{[^{}]+\}', verdict_raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            if isinstance(obj, dict):
                return (
                    obj.get("status", ""),
                    obj.get("reason", ""),
                    obj.get("recommended_action", ""),
                    obj.get("suggested_prompt", ""),
                )
        except (json.JSONDecodeError, ValueError):
            pass

    return "degraded", verdict_raw, "", ""


# ---------------------------------------------------------------------------
# Dashboard activity summary
# ---------------------------------------------------------------------------

def write_activity(session_id, entries, assistant):
    """
    Write a compact activity summary to dashboard/activity.json so the
    dashboard can display recent tool calls without guessing filenames.
    Format is intentionally simple and does NOT include tool_response
    (which can be large) unless it is short.
    """
    activity = []
    for e in entries[-50:]:  # last 50 tool calls
        p = e["payload"]
        tool_input = p.get("tool_input", {})
        entry = {
            "ts": e.get("ts", ""),
            "tool": p.get("tool_name", ""),
            "path": tool_input.get("path", "") or tool_input.get("command", ""),
        }
        activity.append(entry)

    summary = {
        "session_id": session_id or "",
        "assistant": assistant,
        "total_calls": len(entries),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "activity": list(reversed(activity)),  # newest first
    }

    os.makedirs("dashboard", exist_ok=True)
    with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    entries = load_transcript(session_id)
    if not entries:
        return

    assistant = session_assistant(entries)

    flags_struct = []
    for check in [check_repetition, check_runaway, check_recurring_error]:
        result = check(entries)
        if result:
            flags_struct.append(result)
    flags = [f["message"] for f in flags_struct]  # plain strings, for the dashboard/verdict file

    os.makedirs("analyzer", exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tool_calls = len(entries)

    ts_list = [e.get("ts", "") for e in entries if e.get("ts")]
    session_start = ts_list[0] if ts_list else ""
    session_end = ts_list[-1] if ts_list else ""

    if flags_struct:
        if USE_AI_JUDGE:
            verdict_raw = ask_bob_judge(flags, entries)
            status, reason, recommended_action, suggested_prompt = parse_assessment(verdict_raw)
        else:
            verdict = rules.assess(flags_struct, entries)
            status = verdict["status"]
            reason = verdict["reason"]
            recommended_action = verdict["recommended_action"]
            suggested_prompt = verdict["suggested_prompt"]
            verdict_raw = json.dumps(verdict)

        score = compute_score(flags_struct)

        with open(VERDICT_FILE, "w", encoding="utf-8") as f:
            f.write(
                f"WARNING SESSION HEALTH ({assistant}):\n" + "\n".join(flags) +
                "\n\nAssessment:\n" + verdict_raw
            )

        log_entry = {
            "timestamp": timestamp,
            "session_id": session_id or "",
            "assistant": assistant,
            "session_start": session_start,
            "session_end": session_end,
            "status": "degraded",
            "score": score,
            "tool_calls": tool_calls,
            "flags": flags,
            "assessment": verdict_raw,
            "reason": reason,
            "recommended_action": recommended_action,
            "suggested_prompt": suggested_prompt,
        }
    else:
        if os.path.exists(VERDICT_FILE):
            os.remove(VERDICT_FILE)
        log_entry = {
            "timestamp": timestamp,
            "session_id": session_id or "",
            "assistant": assistant,
            "session_start": session_start,
            "session_end": session_end,
            "status": "healthy",
            "score": 100,
            "tool_calls": tool_calls,
            "flags": [],
            "assessment": "healthy",
            "reason": "",
            "recommended_action": "",
            "suggested_prompt": "",
        }

    with open(HEALTH_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    write_activity(session_id, entries, assistant)


if __name__ == "__main__":
    main()
