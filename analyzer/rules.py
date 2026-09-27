"""
analyzer/rules.py
------------------
Deterministic, hardcoded verdicts for the Bob Session Health Monitor.

This module replaces the original ask_bob_judge() call in analyze_session.py
(which spawned a recursive `bob -p` sub-session to get an LLM's opinion on a
flagged session). That path still exists for anyone who wants it back — see
BOB_MONITOR_AI_JUDGE in analyze_session.py — but it is OFF by default, because:

  1. It made the monitor depend on the `bob` binary being installed, licensed,
     and reachable on whatever machine the hook fired on. That broke down the
     moment this project was extended to also watch Claude Code, Codex CLI
     and Gemini CLI sessions: none of those ship a `bob` executable, and
     shelling out to a *different* vendor's CLI mid-hook (auth, rate limits,
     first-run prompts, differing non-interactive flags) is a lot of new
     failure surface for very little payoff.
  2. It created a self-referential "inner session" every single time it
     fired — logged, re-analyzed, and capable of firing this same file
     again. Bounded, but one more thing to explain to a new contributor.
  3. Every flag this tool raises is already a narrow, well-understood
     pattern: a file loop, a runaway session, or a repeated error. None of
     that needs an LLM to interpret it. It needs a clear, consistent
     explanation and a next step — which is a lookup table, not an
     inference problem.

Each scenario below is written as: what fired -> what it usually means ->
what to do about it. If a flag type shows up that isn't in the table, it
falls back to a generic "review manually" verdict rather than guessing.
"""

from __future__ import annotations


def _scenario_repetition(flag):
    path = flag.get("path") or "the file"
    count = flag.get("count", "multiple")
    return {
        "reason": (
            f"'{path}' was rewritten {count} times in a short window. "
            "That pattern usually means one of two things: the fix being "
            "applied isn't actually landing (wrong file, stale copy, or the "
            "real bug is somewhere else), or earlier context about *why* "
            "the file was already changed got lost, so the same edit keeps "
            "getting re-derived from scratch instead of building on the "
            "last attempt."
        ),
        "recommended_action": "add_verification",
        "suggested_prompt": (
            f"Stop editing {path} for a moment. Read its full current "
            "contents, state in one sentence what the last edit was "
            "supposed to fix and why it apparently didn't, then make "
            "exactly one targeted change and stop so it can be checked "
            "before touching the file again."
        ),
    }


def _scenario_runaway(flag):
    total = flag.get("total", "a large number of")
    return {
        "reason": (
            f"{total} tool calls happened back-to-back with no pause to "
            "check in. That usually means the original request was broader "
            "than one clean unit of work, so the session kept turning up "
            "more sub-tasks instead of stopping to report progress."
        ),
        "recommended_action": "new_session",
        "suggested_prompt": (
            "Start a new session. Restate only the single smallest next "
            "step toward the original goal, ask for a short plan first, "
            "and have it stop after that one step so it can be reviewed "
            "before continuing."
        ),
    }


def _scenario_recurring_error(flag):
    tool = flag.get("tool") or "a tool call"
    error = flag.get("error") or "the same error"
    return {
        "reason": (
            f"{tool} hit \"{error}\" again after an edit was already made "
            "to fix it. An identical error reappearing right after a fix "
            "attempt means that fix didn't address the actual cause — "
            "reapplying the same kind of change will most likely reproduce "
            "the same failure a third time."
        ),
        "recommended_action": "rollback",
        "suggested_prompt": (
            f"Start a new session. Before changing any more code, "
            f"reproduce \"{error}\" in isolation and print out what's "
            "actually happening (the real file contents or command output, "
            "not an assumption about them). Only propose a fix once the "
            "root cause is confirmed — don't reapply the previous one."
        ),
    }


def _scenario_combined(top_flags):
    kinds = ", ".join(sorted({f["type"] for f in top_flags}))
    return {
        "reason": (
            f"Multiple problems fired together ({kinds}). Any one of these "
            "alone is usually recoverable in-session; together they "
            "typically mean the session is stuck in a loop it can't "
            "self-correct out of, and continuing risks compounding the "
            "problem rather than fixing it."
        ),
        "recommended_action": "new_session",
        "suggested_prompt": (
            "Start a completely fresh session. Summarize only the original "
            "goal and what's confirmed working so far — leave out the "
            "failed attempts and their details — and ask for a short plan "
            "before making any further code changes."
        ),
    }


_HANDLERS = {
    "repetition": _scenario_repetition,
    "runaway": _scenario_runaway,
    "recurring_error": _scenario_recurring_error,
}


def assess(flags_struct, entries=None):  # entries kept for API parity / future use
    """
    flags_struct: list of dicts, each with at least a "type" key matching a
    key in _HANDLERS, plus whatever context that scenario needs (path/count,
    total, tool/error).

    Returns {"status", "reason", "recommended_action", "suggested_prompt"} —
    the same shape analyze_session.py previously got back from
    parse_assessment(ask_bob_judge(...)), so nothing downstream (the verdict
    file, the health log, the dashboard) has to change.
    """
    if not flags_struct:
        return {
            "status": "healthy",
            "reason": "",
            "recommended_action": "",
            "suggested_prompt": "",
        }

    if len(flags_struct) > 1:
        scenario = _scenario_combined(flags_struct)
    else:
        flag = flags_struct[0]
        handler = _HANDLERS.get(flag["type"])
        if handler:
            scenario = handler(flag)
        else:
            scenario = {
                "reason": flag.get("message", "An unrecognized flag was raised."),
                "recommended_action": "add_verification",
                "suggested_prompt": "Review the flagged session manually before continuing.",
            }

    return {"status": "degraded", **scenario}
