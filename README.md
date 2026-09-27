# Bob Session Health Monitor

A lightweight session health monitor originally built for [IBM Bob Shell](https://www.ibm.com/docs/en/bob). **Everything about how this project works — the hook lifecycle, the JSONL session log format, the three detection heuristics, the health-score model, the dashboard — is IBM Bob's design.** Bob's `PostToolUse` / `Stop` / `UserPromptSubmit` hook model is the entire foundation this project is built on, and is what made a monitor like this possible in the first place.

This build extends that foundation in two ways, both described in detail below:

1. **It's now assistant-aware.** The same detection engine now also watches sessions from Claude Code, Codex CLI (the ChatGPT / OpenAI coding agent), and Gemini CLI, using thin per-assistant hook configs that feed the exact same Bob-designed pipeline. Every logged entry, verdict, and dashboard row is tagged with which assistant produced it.
2. **The verdict step no longer calls an LLM by default.** Flags are now turned into a diagnosis and a suggested next prompt by a small hardcoded rules table instead of a recursive `bob -p` call. Why, and what that trades off, is in [Why the AI judge was replaced](#why-the-ai-judge-was-replaced).

Everything else — including the whole original Bob-only setup — still works exactly as before.

---

## Table of Contents

1. [What the project does](#what-the-project-does)
2. [How the hooks work](#how-the-hooks-work)
3. [Heuristics in analyze_session.py](#heuristics-in-analyze_sessionpy)
4. [Why the AI judge was replaced](#why-the-ai-judge-was-replaced)
5. [Multi-assistant support](#multi-assistant-support)
6. [Setup and usage](#setup-and-usage)
7. [The dashboard](#the-dashboard)
8. [File structure](#file-structure)
9. [Runtime files and .gitignore](#runtime-files-and-gitignore)
10. [Credit](#credit)

---

## What the project does

Every time a coding assistant uses a tool (reads a file, writes code, runs a command, etc.) a `PostToolUse`-style hook fires and appends a timestamped record to a per-session JSONL log under `sample-sessions/`. When the assistant finishes responding, a `Stop`-style hook fires and runs `analyzer/analyze_session.py` against that session's log.

The analyzer applies three heuristics — all three are Bob's original design, untouched:

- **`check_repetition`** — the same file written 3+ times in the last 15 tool calls.
- **`check_runaway`** — 25+ tool calls in one session with no break.
- **`check_recurring_error`** — the same error-looking `tool_response` reappearing after a fix was attempted in between.

If any heuristic fires, the flagged details are run through `analyzer/rules.py`, a hardcoded scenario table (not an LLM call — see below), which produces a status, a plain-English reason, a recommended action (`new_session` / `narrower_prompt` / `add_verification` / `rollback`), and a ready-to-use suggested prompt for a fresh session. The verdict is written to `analyzer/last_verdict.txt`.

On the very next user prompt, a `UserPromptSubmit`-style hook reads `last_verdict.txt` and writes it to stdout. The host assistant treats that stdout as additional model context — invisible to the user as a chat message but present in the model's context window — so the model is automatically aware the previous session was flagged before it acts on the new prompt. This mechanism is exactly Bob's original design; it carries over unmodified to every assistant this project now supports, because all of them treat a `UserPromptSubmit` hook's stdout the same way.

Every session analysis (healthy or degraded) is appended to `analyzer/health_log.jsonl`, which powers the dashboard.

---

## How the hooks work

Each supported assistant gets its own settings file, all pointing at the same underlying Python scripts in `hooks/`:

| Assistant | Config file | Notes |
|---|---|---|
| **IBM Bob** | `.bob/settings.json` | Original, unmodified |
| **Claude Code** | `.claude/settings.json` | Same hook event names and payload shape as Bob — reuses `hooks/*.py` directly |
| **Codex CLI** (ChatGPT) | `.codex/hooks.json` | Same hook event names and payload shape as Bob — reuses `hooks/*.py` directly |
| **Gemini CLI** | `.gemini/settings.json` | Different event names (`AfterTool` instead of `PostToolUse`, etc.) — routed through `hooks/gemini_adapter.py` first |

### PostToolUse → `hooks/log_tool_use.py`

Fires after every tool call completes. The assistant passes a JSON payload on stdin:

| Field | Description |
|---|---|
| `session_id` | Unique identifier for the current session |
| `tool_name` | Name of the tool that was called (e.g. `write_file`, `read_file`) |
| `tool_input` | The arguments passed to the tool (a JSON object) |
| `tool_response` | The raw text response returned by the tool |

The script appends a timestamped, **assistant-tagged** record to `sample-sessions/<session_id>.jsonl`:

```json
{ "ts": "2025-01-01T12:00:00Z", "assistant": "bob", "payload": { "session_id": "…", "tool_name": "…", "tool_input": {…}, "tool_response": "…" } }
```

The `assistant` tag comes from the `BOB_MONITOR_ASSISTANT` environment variable, set inline in each config's hook command (see the config files). It defaults to `"bob"` if unset, so the original Bob-only setup needs zero changes.

The `sample-sessions/` directory is created automatically if it does not exist.

### Stop → `hooks/on_stop_analyze.py`

Fires when the assistant finishes generating its response. Extracts `session_id` from the payload and spawns `python3 analyzer/analyze_session.py <session_id>` as a subprocess with a **30-second** timeout (down from the original 120 seconds — see [Why the AI judge was replaced](#why-the-ai-judge-was-replaced) for why that's now safe). Stdin is closed (`DEVNULL`) so the child process doesn't inherit the hook's stdin pipe.

### UserPromptSubmit → `hooks/on_prompt_check.py`

Fires when the user submits a new prompt, before the assistant processes it. Checks whether `analyzer/last_verdict.txt` exists, and if so writes its contents to stdout, which the host assistant folds into the model's context ahead of the user's prompt. Completely unchanged from the original — it never touches an assistant-specific field to begin with.

### Gemini CLI's extra step

Gemini CLI's tool-call event is named `AfterTool`, not `PostToolUse`, and its turn-end event doesn't use Bob/Claude/Codex's `Stop` name either. `hooks/gemini_adapter.py` is a small translation layer: it reads `hook_event_name` out of the payload, decides whether it's looking at a completed tool call or an ended turn, and forwards to the exact same `log_entry()` / `dispatch()` functions the other three assistants call directly. No detection logic is duplicated — this file's only job is naming.

Gemini CLI's hook system is newer and versions may use different literal event names than the ones matched in `_looks_like_tool_event` / `_looks_like_turn_end`. If your installed version doesn't match, check `gemini hooks list` and adjust those two functions — that's the only place any Gemini-specific naming lives.

---

## Heuristics in analyze_session.py

Unchanged from Bob's original design — see the code for the exact matching rules. Summary:

### `check_repetition`
Scans the last 15 tool-call entries and counts how many times each file path appears as the `path` argument of a `write_file` call. 3+ occurrences flags a possible write loop.

### `check_runaway`
Counts all tool-call entries in the session. 25+ flags a possible runaway session.

### `check_recurring_error`
Looks for pairs of tool responses that (a) look like an actual error — matched against a keyword list (`error`, `traceback`, `not found`, `denied`, etc.), so two identical "ok" confirmations never false-positive — and (b) have a "fix tool" call (`write_file`, `apply_diff`, `search_and_replace`, `insert_content`, `execute_command`) in between them. That pattern means a fix was attempted and the same failure came back anyway.

---

## Why the AI judge was replaced

The original design, when any heuristic fired, called `bob -p` in a fresh sub-session and asked it for a JSON verdict (reason, recommended action, suggested prompt). That still works and is still in the code (`ask_bob_judge` / `_bob_executable` in `analyze_session.py`) — set `BOB_MONITOR_AI_JUDGE=1` in the environment to turn it back on.

It's off by default now, for three concrete reasons:

1. **It only worked for Bob.** `bob -p` needs the `bob` binary, license-accepted and configured, on whatever machine the hook fires on. Claude Code, Codex CLI and Gemini CLI sessions don't have that binary, and shelling out to a *different* vendor's CLI from inside one assistant's hook (auth, first-run prompts, differing non-interactive flags, rate limits) is a lot of new failure surface for what turns out to be a small payoff.
2. **It was self-referential.** Every time it fired, it spawned a new inner Bob session, which got logged and re-analyzed by the exact same hooks — bounded, and explained in the original README, but one more thing to reason about.
3. **None of the flags actually need an LLM's judgment.** A file rewritten 3+ times, 25+ tool calls with no break, and a repeated error after a fix attempt are three narrow, well-understood patterns. Each one has a specific, known cause and a specific, known fix — that's a lookup table, not an inference problem.

`analyzer/rules.py` is that lookup table. Each scenario is written as *this flag means this is probably happening → do this about it*:

| Flag | What it usually means | Recommended action | Suggested next step |
|---|---|---|---|
| **Repetition** (same file written 3+ times) | The fix isn't landing (wrong file, stale copy, real bug elsewhere), or context about *why* it was already changed got lost, so the same edit keeps getting re-derived | `add_verification` | Re-read the file, state in one sentence why the last edit didn't work, make exactly one targeted change, stop |
| **Runaway** (25+ tool calls, no break) | The original request was broader than one clean unit of work | `narrower_prompt` | Start a new session, restate only the smallest next step, ask for a plan first |
| **Recurring error** (same error after a fix attempt) | The fix didn't address the root cause | `rollback` | Reproduce the error in isolation, confirm the actual root cause before proposing another fix |
| **More than one of the above at once** | The session is stuck in a loop it can't self-correct out of | `new_session` | Fresh session, summarize only the goal and what's confirmed working, ask for a plan before any more changes |

This is fully deterministic, has zero external dependencies, runs in milliseconds, and is the reason the `Stop` hook's timeout could safely drop from 120 seconds to 30.

---

## Multi-assistant support

| Assistant | Hook event names | Payload shape | Works via |
|---|---|---|---|
| IBM Bob | `PostToolUse` / `Stop` / `UserPromptSubmit` | `session_id`, `tool_name`, `tool_input`, `tool_response` | `hooks/*.py` directly |
| Claude Code | Same names as Bob | Same fields as Bob | `hooks/*.py` directly |
| Codex CLI | Same names as Bob | Same fields as Bob | `hooks/*.py` directly |
| Gemini CLI | `AfterTool` / a turn-end event / its own prompt-submit event | Same tool_name/tool_input fields, different event names | `hooks/gemini_adapter.py`, then the same `hooks/*.py` functions |

Because the detection engine, the rules table, and the log/dashboard formats are all shared, a session logged from any of the four assistants is analyzed identically and shows up in the same dashboard, tagged with which assistant it came from.

---

## Setup and usage

**Prerequisites**

- Python 3.7 or later (no third-party packages required)
- At least one of: IBM Bob Shell, Claude Code, Codex CLI, or Gemini CLI, installed and configured

**1. Copy the config for your assistant(s)**

Copy whichever of `.bob/`, `.claude/`, `.codex/`, `.gemini/` matches what you use into your project root, alongside `hooks/` and `analyzer/`. You can enable more than one at once — sessions from each are tagged and logged separately.

**2. Trust the workspace / accept any one-time license prompts**

Follow whatever first-run step your assistant requires (Bob's license acceptance, Claude Code / Codex CLI / Gemini CLI workspace trust prompts, etc.) — this project doesn't change any of that.

**3. Run your assistant from the project root**

All hook commands and file paths are relative. The assistant must always be launched from the project root so that `sample-sessions/`, `analyzer/last_verdict.txt`, and `hooks/*.py` resolve correctly.

From this point on, every session in this workspace — from any configured assistant — is automatically monitored, with no further configuration needed.

**Want the old LLM-generated verdicts back?** Set `BOB_MONITOR_AI_JUDGE=1` in the environment before launching, and bump the `Stop` hook's `timeout` back up to something like `120` in your assistant's config, since the AI path can take a while and only works for Bob sessions.

---

## The dashboard

Unchanged from the original. Reads `analyzer/health_log.jsonl` via `fetch()`, so it must be served over HTTP:

```
python3 -m http.server 8080
```

Then open `http://localhost:8080/dashboard/index.html`.

**What the dashboard shows**

- **Health score** — percentage of sessions with no flags. 🟢 80%+, 🟡 50–79%, 🔴 below 50%.
- **Session log** — one card per session, newest first. ✅ healthy, ⚠️ degraded (with flags and the assessment).

Health-log rows and `dashboard/activity.json` now also carry an `assistant` field (`"bob"`, `"claude-code"`, `"codex-cli"`, or `"gemini-cli"`). This is additive — the dashboard markup described above doesn't need to change to keep working, since it was already just reading known fields off each row; a version-name badge per session is an easy follow-up if you want one, using that new field.

---

## File structure

```
.
├── .bob/settings.json          # IBM Bob hook config (original)
├── .claude/settings.json       # Claude Code hook config
├── .codex/hooks.json           # Codex CLI hook config
├── .gemini/settings.json       # Gemini CLI hook config
│
├── hooks/
│   ├── log_tool_use.py         # PostToolUse: appends tagged records to sample-sessions/
│   ├── on_stop_analyze.py      # Stop: spawns analyze_session.py for the finished session
│   ├── on_prompt_check.py      # UserPromptSubmit: injects last_verdict.txt into model context
│   └── gemini_adapter.py       # Gemini-only: translates AfterTool/turn-end into the above
│
├── analyzer/
│   ├── analyze_session.py      # Loads session log, runs heuristics, produces a verdict
│   ├── rules.py                # Hardcoded scenario → verdict table (default path)
│   ├── last_verdict.txt        # Written when flags fire; deleted when session is clean (runtime)
│   └── health_log.jsonl        # Append-only log of every session analysis (runtime)
│
├── sample-sessions/
│   └── <session_id>.jsonl      # One file per session; each line is a tagged tool-call record (runtime)
│
├── dashboard/
│   └── index.html              # Browser dashboard; reads health_log.jsonl via fetch()
│
└── README.md
```

---

## Runtime files and .gitignore

Unchanged:

```
sample-sessions/
analyzer/last_verdict.txt
analyzer/health_log.jsonl
```

To reset the health history, delete `analyzer/health_log.jsonl`; to reset the current-session warning state, delete `analyzer/last_verdict.txt`.

---

## Credit

This project exists because of **IBM Bob Shell's hook model and Bob's own design of the health-monitoring pipeline** — the JSONL session log format, all three detection heuristics, the health-score model, and the original dashboard were all built for and around Bob. Bob is what made this idea possible, and remains the reference implementation everything else in this repo has to stay compatible with. The Claude Code / Codex CLI / Gemini CLI support and the hardcoded rules engine described above are extensions layered on top of that foundation for this hackathon build, not a replacement for it.
