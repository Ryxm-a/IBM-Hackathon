# Bob Session Health Monitor

A lightweight session health monitor for [IBM Bob Shell](https://www.ibm.com/docs/en/bob). It hooks into every tool call Bob makes, runs heuristic checks at the end of each session, and — when something looks wrong — calls `bob -p` to get an AI-generated assessment and recovery prompt. A browser dashboard displays a health score across all sessions.

---

## Table of Contents

1. [What the project does](#what-the-project-does)
2. [How the hooks work](#how-the-hooks-work)
3. [Heuristics in analyze\_session.py](#heuristics-in-analyze_sessionpy)
4. [Setup and usage](#setup-and-usage)
5. [The dashboard](#the-dashboard)
6. [File structure](#file-structure)
7. [Runtime files and .gitignore](#runtime-files-and-gitignore)
8. [Important note on inner sessions](#important-note-on-inner-sessions)

---

## What the project does

Every time Bob uses a tool (reads a file, writes code, runs a command, etc.) the `PostToolUse` hook fires and appends a timestamped record to a per-session JSONL log under `sample-sessions/`. When Bob finishes responding, the `Stop` hook fires and runs `analyzer/analyze_session.py` against that session's log.

The analyzer applies three heuristics. If any flag is raised, it calls `bob -p` with a structured prompt asking for a JSON verdict: a status (`healthy` or `degraded`), a reason, a recommended action, and a ready-to-use suggested prompt for a fresh session. The verdict is written to `analyzer/last_verdict.txt`.

On the very next user prompt, the `UserPromptSubmit` hook reads `last_verdict.txt` and writes it to stdout. Bob Shell treats that stdout as additional model context — invisible to the user as a chat message but present in the model's context window — so the model is automatically aware that the previous session was flagged before it acts on the new prompt.

Every session analysis (healthy or degraded) is appended to `analyzer/health_log.jsonl`, which powers the dashboard.

---

## How the hooks work

Hook configuration lives in [`.bob/settings.json`](.bob/settings.json). Three hook events are registered:

```json
{
  "hooks": {
    "PostToolUse": [{ "hooks": [{ "type": "command", "command": "python3 hooks/log_tool_use.py" }] }],
    "Stop":        [{ "hooks": [{ "type": "command", "command": "python3 hooks/on_stop_analyze.py", "timeout": 120 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 hooks/on_prompt_check.py" }] }]
  }
}
```

### PostToolUse → [`hooks/log_tool_use.py`](hooks/log_tool_use.py)

Fires after every tool call completes. Bob passes a JSON payload on stdin with the following fields:

| Field | Description |
|---|---|
| `session_id` | Unique identifier for the current Bob session |
| `tool_name` | Name of the tool that was called (e.g. `write_file`, `read_file`) |
| `tool_input` | The arguments passed to the tool (a JSON object) |
| `tool_response` | The raw text response returned by the tool |

The script appends a timestamped record to `sample-sessions/<session_id>.jsonl`:

```json
{ "ts": "2025-01-01T12:00:00Z", "payload": { "session_id": "…", "tool_name": "…", "tool_input": {…}, "tool_response": "…" } }
```

The `sample-sessions/` directory is created automatically if it does not exist.

### Stop → [`hooks/on_stop_analyze.py`](hooks/on_stop_analyze.py)

Fires when Bob finishes generating its response. Bob passes a JSON payload on stdin with:

| Field | Description |
|---|---|
| `session_id` | The session that just ended |
| `last_assistant_message` | The final text message Bob sent to the user |

The script extracts `session_id` and spawns `python3 analyzer/analyze_session.py <session_id>` as a subprocess with a 120-second timeout configured in the hook. Stdin is closed (`DEVNULL`) so the child process does not inherit the hook's stdin pipe.

### UserPromptSubmit → [`hooks/on_prompt_check.py`](hooks/on_prompt_check.py)

Fires when the user submits a new prompt, before Bob processes it. Bob passes the prompt payload on stdin (consumed and discarded to avoid broken-pipe warnings). The script then checks whether `analyzer/last_verdict.txt` exists. If it does, its contents are written to stdout.

**Bob Shell treats the stdout of a `UserPromptSubmit` hook as additional model context.** It is injected into the model's context window ahead of the user's prompt but is not shown as a visible chat message. This means the model silently receives the session health warning and Bob's previous assessment before answering, without any action required from the user.

---

## Heuristics in analyze\_session.py

[`analyzer/analyze_session.py`](analyzer/analyze_session.py) loads the JSONL log for the given session and runs three independent checks. Each returns either a human-readable warning string or `None`.

### `check_repetition`

Scans the **last 15 tool-call entries** in the session log and counts how many times each file path appears as the `path` argument of a `write_file` call. If the same path appears **3 or more times**, it flags a possible write loop:

> `File 'src/main.py' was written 4 times in the last 15 actions — possible loop.`

### `check_runaway`

Counts all tool-call entries in the entire session. If the total reaches **25 or more**, it flags a possible runaway session:

> `27 tool calls in this session with no break — possible runaway session.`

### `check_recurring_error`

Detects when the same error reappears after Bob attempted to fix it. The algorithm scans all entries that have a non-empty `tool_response` and looks for pairs `(A, B)` where:

1. `A` and `B` have the same normalised, truncated `tool_response` signature (whitespace-collapsed, first 200 characters).
2. At least one call to a **fix tool** appears in the entries between `A` and `B`.

The fix-tool set is:

```
write_file, apply_diff, search_and_replace, insert_content, execute_command
```

When a match is found, the flag message names the tool used at `B` and shows the first 80 characters of the error:

> `Tool 'read_file' returned the same error after a fix was attempted — possible unfixed loop. Error: "No such file or directory…"`

### What happens when flags are raised

If one or more heuristics fire, `analyze_session.py` calls `ask_bob_judge`, which runs:

```bash
bob -p "<structured prompt with last 10 session entries>" --hide-intermediary-output
```

Bob is asked to reply with a single JSON object:

```json
{
  "status": "healthy or degraded",
  "reason": "short explanation",
  "recommended_action": "new_session or narrower_prompt or add_verification or rollback",
  "suggested_prompt": "a ready-to-use shorter prompt for a fresh session"
}
```

The verdict is written to `analyzer/last_verdict.txt` and the flagged entry (flags + assessment) is appended to `analyzer/health_log.jsonl`.

If no flags are raised, any existing `last_verdict.txt` is deleted (so the next session starts clean) and a `{"flags": [], "assessment": "healthy"}` entry is appended to the health log.

---

## Setup and usage

**Prerequisites**

- Python 3.7 or later (no third-party packages required)
- IBM Bob Shell installed and configured with API key authentication

**1. Accept the license**

Bob requires a one-time license acceptance before running in non-interactive (`-p`) mode:

```bash
bob --accept-license -p "hello"
```

**2. Trust the workspace**

Bob's trust model requires you to run it interactively once from the project root so it can prompt for workspace trust:

```bash
bob
```

Follow the on-screen prompt, then exit (`/exit` or Ctrl+C).

**3. Run Bob from the project root**

All hook commands and file paths are relative. Bob must always be launched from the project root directory so that paths like `sample-sessions/`, `analyzer/last_verdict.txt`, and `hooks/*.py` resolve correctly:

```bash
cd /path/to/bob-in-a-bob
bob
```

From this point on, every Bob session in this workspace is automatically monitored — no further configuration is needed.

---

## The dashboard

The dashboard reads `analyzer/health_log.jsonl` via a `fetch()` call, which means it **must be served over HTTP**. Opening `dashboard/index.html` directly as a `file://` URL will fail due to browser CORS restrictions.

Start a local HTTP server from the project root:

```bash
python3 -m http.server 8080
```

Then open:

```
http://localhost:8080/dashboard/index.html
```

**What the dashboard shows**

- **Health score** — the percentage of sessions with no flags, displayed as a large number. Color coding:
  - 🟢 Green — 80% or above
  - 🟡 Yellow — 50–79%
  - 🔴 Red — below 50%
  - The subtitle shows the raw count: e.g. `health score · 9 healthy / 11 total sessions`

- **Session log** — one card per session analysis, newest first. Healthy sessions show a ✅ badge. Degraded sessions show a ⚠️ badge, the list of triggered flags (in amber), and Bob's raw JSON assessment.

The dashboard updates on page reload; there is no live polling.

---

## File structure

```
.
├── .bob/
│   └── settings.json          # Hook configuration (PostToolUse, Stop, UserPromptSubmit)
│
├── hooks/
│   ├── log_tool_use.py        # PostToolUse: appends tool-call records to sample-sessions/
│   ├── on_stop_analyze.py     # Stop: spawns analyze_session.py for the finished session
│   └── on_prompt_check.py     # UserPromptSubmit: injects last_verdict.txt into model context
│
├── analyzer/
│   ├── analyze_session.py     # Loads session log, runs heuristics, calls bob -p if needed
│   ├── last_verdict.txt       # Written when flags fire; deleted when session is clean (runtime)
│   └── health_log.jsonl       # Append-only log of every session analysis (runtime)
│
├── sample-sessions/
│   └── <session_id>.jsonl     # One file per Bob session; each line is a tool-call record (runtime)
│
├── dashboard/
│   └── index.html             # Browser dashboard; reads health_log.jsonl via fetch()
│
└── README.md
```

---

## Runtime files and .gitignore

The following paths are generated at runtime and excluded from version control:

```gitignore
sample-sessions/
analyzer/last_verdict.txt
analyzer/health_log.jsonl
```

`sample-sessions/` can grow large over time (one JSONL file per session). To reset the health history, delete `analyzer/health_log.jsonl`; to reset the current-session warning state, delete `analyzer/last_verdict.txt`.

---

## Important note on inner sessions

When `analyze_session.py` calls `bob -p` via `ask_bob_judge`, it spawns a **new, inner Bob session**. That inner session runs inside the same workspace, so its tool calls are also captured by the `PostToolUse` hook and written to their own session file under `sample-sessions/`. When that inner session ends, the `Stop` hook fires for it too, and `analyze_session.py` runs again on the inner session's log.

This is expected behavior and harmless. The inner session is short (a single structured prompt expecting a JSON response) and will almost always produce a clean bill of health. Occasional recursion is bounded: the inner `bob -p` call does not itself trigger further `bob -p` calls because the inner session's log will not accumulate enough entries to trip any heuristic.
