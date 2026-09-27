"""
hooks/on_prompt_check.py
--------------------------
UserPromptSubmit hook (original IBM Bob design), unchanged.

Never needed a change for multi-assistant support: it doesn't read any
assistant-specific payload fields at all — it just discards stdin and, if
analyzer/last_verdict.txt exists, writes its contents to stdout. Every
assistant this project supports (Bob, Claude Code, Codex CLI, and Gemini
CLI's own UserPromptSubmit-equivalent event) treats a hook's stdout the same
way: silent additional context for the model, not a visible chat message.
So this file is wired up directly from all four settings files with no
adapter in between.
"""

import os
import sys

if __name__ == "__main__":
    sys.stdin.read()  # consume the UserPromptSubmit payload; avoids broken-pipe warnings
    if os.path.exists("analyzer/last_verdict.txt"):
        with open("analyzer/last_verdict.txt", encoding="utf-8") as f:
            sys.stdout.write(f.read())
