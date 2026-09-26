import json
import glob
import subprocess
import os
import sys
import shutil
import re
from collections import Counter
from datetime import datetime, timezone

TRANSCRIPT_DIR = "sample-sessions"
VERDICT_FILE   = "analyzer/last_verdict.txt"
HEALTH_LOG     = "analyzer/health_log.jsonl"
ACTIVITY_FILE  = "dashboard/activity.json"   # written for dashboard; not used by hooks

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
            return f"File '{path}' was written {count} times in the last 15 actions — possible loop."
    return None

def check_runaway(entries):
    """Flags if there's a huge number of tool calls with no stop in between."""
    if len(entries) >= 25:
        return f"{len(entries)} tool calls in this session with no break — possible runaway session."
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
                return (
                    f"Tool '{tool_b}' returned the same error after a fix was attempted — "
                    f"possible unfixed loop. Error: \"{sig_a[:80]}{'...' if len(sig_a) > 80 else ''}\""
                )
    return None

# ---------------------------------------------------------------------------
# Health score (transparent model — not an official IBM metric)
# ---------------------------------------------------------------------------

def compute_score(flags, entries):
    """
    Score 0-100 based on detected signals. Not an official IBM metric.
    Starts at 100 and deducts for each detected problem:
      - Runaway session: -30
      - Recurring error: -25
      - Repeated file writes: -20
    """
    score = 100
    for flag in flags:
        fl = flag.lower()
        if "runaway" in fl or "tool calls" in fl:
            score -= 30
        elif "recurring" in fl or "same error" in fl or "unfixed loop" in fl:
            score -= 25
        elif "written" in fl and "possible loop" in fl:
            score -= 20
        else:
            score -= 15
    return max(0, score)

# ---------------------------------------------------------------------------
# Bob judge (headless bob run)
# ---------------------------------------------------------------------------

def _bob_executable():
    """Return the bob executable name, preferring bob.cmd on Windows."""
    for name in ("bob.cmd", "bob.ps1", "bob"):
        if shutil.which(name):
            return name
    return "bob"

def ask_bob_judge(flags, entries):
    """Call bob run to get a structured assessment of the flagged session."""
    summary_lines = []
    for e in entries[-10:]:
        p = e["payload"]
        summary_lines.append(f"  {e['ts']}  {p.get('tool_name','?')}")
    summary = "\n".join(summary_lines)
    flags_text = "\n".join(f"- {f}" for f in flags)
    prompt = (
        "An automated session health monitor flagged this Bob Shell session. "
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

# ---------------------------------------------------------------------------
# Dashboard activity summary
# ---------------------------------------------------------------------------

def write_activity(session_id, entries):
    """
    Write a compact activity summary to dashboard/activity.json so the
    dashboard can display recent tool calls without guessing filenames.
    Format is intentionally simple and does NOT include tool_response
    (which can be large) unless it is short.
    """
    activity = []
    for e in entries[-50:]:   # last 50 tool calls
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
        "total_calls": len(entries),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "activity": list(reversed(activity)),   # newest first
    }
    os.makedirs("dashboard", exist_ok=True)
    with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

# ---------------------------------------------------------------------------
# Parse assessment into structured fields
# ---------------------------------------------------------------------------

def parse_assessment(verdict_raw):
    """
    Try to extract structured fields from verdict_raw.
    Returns (status, reason, recommended_action, suggested_prompt).
    Falls back gracefully to plain-text handling.
    """
    if not verdict_raw or verdict_raw == "healthy":
        return "healthy", "", "", ""
    # Try direct JSON parse
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
    # Try to find a JSON object embedded in text
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
    # Plain text fallback
    return "degraded", verdict_raw, "", ""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    entries = load_transcript(session_id)
    if not entries:
        return

    flags = []
    for check in [check_repetition, check_runaway, check_recurring_error]:
        result = check(entries)
        if result:
            flags.append(result)

    os.makedirs("analyzer", exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tool_calls = len(entries)

    # Derive session start/end from transcript timestamps
    ts_list = [e.get("ts", "") for e in entries if e.get("ts")]
    session_start = ts_list[0] if ts_list else ""
    session_end   = ts_list[-1] if ts_list else ""

    if flags:
        verdict_raw = ask_bob_judge(flags, entries)
        status, reason, recommended_action, suggested_prompt = parse_assessment(verdict_raw)
        score = compute_score(flags, entries)

        with open(VERDICT_FILE, "w", encoding="utf-8") as f:
            f.write(
                "WARNING SESSION HEALTH:\n" + "\n".join(flags) +
                "\n\nBob's assessment:\n" + verdict_raw
            )

        log_entry = {
            "timestamp": timestamp,
            "session_id": session_id or "",
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

    write_activity(session_id, entries)


if __name__ == "__main__":
    main()
