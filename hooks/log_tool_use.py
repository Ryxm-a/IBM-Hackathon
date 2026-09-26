import sys, json, os
from datetime import datetime, timezone

def main():
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    session_id = payload.get("session_id", "unknown")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs("sample-sessions", exist_ok=True)
    with open(f"sample-sessions/{session_id}.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "payload": payload}) + "\n")

if __name__ == "__main__":
    main()
