import os
import sys

if __name__ == "__main__":
    sys.stdin.read()  # consume the UserPromptSubmit payload; avoids broken-pipe warnings
    if os.path.exists("analyzer/last_verdict.txt"):
        with open("analyzer/last_verdict.txt", encoding="utf-8") as f:
            sys.stdout.write(f.read())
