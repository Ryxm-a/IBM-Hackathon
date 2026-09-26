import subprocess
import sys
import json

if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.read())
        session_id = payload.get("session_id", "")
    except (json.JSONDecodeError, ValueError):
        session_id = ""

    args = ["python3", "analyzer/analyze_session.py"]
    if session_id:
        args.append(session_id)
    subprocess.run(args, stdin=subprocess.DEVNULL)
