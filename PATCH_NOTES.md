# How to apply this to Ryxm-a/IBM-Hackathon

This folder mirrors your repo's structure. Nothing here touches
`dashboard/index.html`, `check.py`, `test_check.py`, `test_monitor.py`, or
`debug_payload.json` — I didn't want to guess at your dashboard's exact
markup and rewrite it blind, so the new `assistant` field is additive-only
and the existing dashboard keeps working untouched.

## Files that are brand new — just add them

- `.claude/settings.json`
- `.codex/hooks.json`
- `.gemini/settings.json`
- `hooks/gemini_adapter.py`
- `analyzer/rules.py`
- `PATCH_NOTES.md` (this file — optional to keep, delete once merged if you don't want it in the repo)

## Files that replace existing ones — same path, new content

- `README.md` — full rewrite. Bob credit moved up top, adds the multi-assistant
  and rules-engine sections. Your original content (heuristics, setup, file
  structure, runtime files) is preserved, just reorganized around the new material.
- `analyzer/analyze_session.py` — same three heuristics, byte-for-byte same
  matching logic, just:
  - each `check_*` function now returns a small dict instead of a bare string
    (adds a `"type"` + context, keeps a `"message"` key with the exact same
    text as before, so `health_log.jsonl`/`last_verdict.txt`/dashboard are
    unaffected)
  - `compute_score` reads `flag["type"]` instead of string-sniffing the message
  - `ask_bob_judge` / `_bob_executable` / `parse_assessment` are unchanged but
    now gated behind `BOB_MONITOR_AI_JUDGE=1` (unset = uses `rules.py` instead)
  - every session gets tagged with an `assistant` field, everywhere
    (`sample-sessions` entries via the hook scripts, `last_verdict.txt`,
    `health_log.jsonl` rows, `dashboard/activity.json`)
- `hooks/log_tool_use.py` — same file-append logic, wrapped in a `log_entry()`
  function (so `gemini_adapter.py` can reuse it), plus the `assistant` tag.
- `hooks/on_stop_analyze.py` — same subprocess-spawn logic, wrapped in a
  `dispatch()` function (same reuse reason), otherwise identical.
- `hooks/on_prompt_check.py` — literally unchanged, included here only so the
  folder is a complete drop-in copy of `hooks/`.
- `.bob/settings.json` — only change is the `Stop` hook's `timeout`, dropped
  from 120 to 30 seconds, since the default path no longer waits on a `bob -p`
  call. If you turn `BOB_MONITOR_AI_JUDGE=1` back on, bump this back up.

## What I deliberately left alone

- `dashboard/index.html` — no changes. The new `assistant` field is available
  in `health_log.jsonl` and `activity.json` rows whenever you want to surface
  it (e.g. a small badge per session card), but I didn't want to rewrite a
  file I hadn't actually seen.
- `check.py`, `test_check.py`, `test_monitor.py`, `debug_payload.json` — these
  looked like scratch/dogfooding artifacts unrelated to the monitor itself
  (per my earlier review). Not part of this request, so left untouched.

## One thing worth double-checking yourself

Gemini CLI's hook system is the newest of the four and I couldn't verify the
exact literal event names your installed version emits for "tool call
finished" and "turn ended" — `.gemini/settings.json` and
`hooks/gemini_adapter.py` use my best reading of their current docs
(`AfterTool`, `SessionEnd`), with a runtime fallback that pattern-matches on
`hook_event_name` rather than requiring an exact string. If your version logs
something else, run `gemini hooks list` and adjust the two `_looks_like_*`
functions in `gemini_adapter.py` and the two keys in `.gemini/settings.json`.
