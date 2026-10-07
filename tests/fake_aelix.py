"""A deterministic subprocess implementing Aelix's documented JSONL wire."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--session-dir", required=True)
parser.add_argument("--session")
parser.add_argument("--tools")
parser.add_argument("--missing-policy", action="store_true")
parser.add_argument("--wrong-session", action="store_true")
args, _ = parser.parse_known_args()
directory = Path(args.session_dir)
directory.mkdir(parents=True, exist_ok=True)
filename = Path(args.session) if args.session else directory / (uuid.uuid4().hex + ".jsonl")
if args.wrong_session:
    filename = directory.parent / "escaped.jsonl"
count = len(filename.read_text().splitlines()) if filename.exists() else 0
answer = "old answer that must not leak into a failed turn"
if args.tools and not args.missing_policy:
    print("aelix-mattermost-policy-ready:" + os.environ["AELIX_MATTERMOST_POLICY_NONCE"],
          file=sys.stderr, flush=True)


def emit(packet):
    print(json.dumps(packet), flush=True)


for line in sys.stdin:
    packet = json.loads(line)
    command = packet["type"]
    result = {}
    if command == "get_state":
        result = {"sessionFile": str(filename), "messageCount": count * 2,
                  "isStreaming": False, "tokenVisible": "MATTERMOST_TOKEN" in os.environ}
    elif command == "prompt":
        emit({"type": "response", "id": packet["id"], "command": command, "success": True})
        text = packet["message"]
        if text == "__exit__":
            sys.exit(7)
        if text == "__malformed__":
            print("not JSON", flush=True)
            continue
        if text == "__hang__":
            time.sleep(60)
            continue
        if text.startswith("__slow__"):
            time.sleep(0.08)
        count += 1
        with filename.open("a") as handle:
            handle.write(json.dumps({"prompt": text}) + "\n")
        if text == "__error__":
            message = {"role": "assistant", "content": [], "stopReason": "error",
                       "errorMessage": "provider-secret-must-not-be-posted"}
        else:
            answer = f"turn {count}: {text}"
            message = {"role": "assistant", "content": [{"type": "text", "text": answer}],
                       "stopReason": "stop"}
        emit({"type": "message_end", "message": message})
        emit({"type": "agent_end", "messages": [message]})
        continue
    elif command == "get_last_assistant_text":
        result = {"text": answer}
    elif command == "abort":
        emit({"type": "agent_end", "messages": []})
    else:
        emit({"type": "response", "id": packet["id"], "command": command,
              "success": False, "error": "unsupported command"})
        continue
    emit({"type": "response", "id": packet["id"], "command": command, "success": True, "data": result})
