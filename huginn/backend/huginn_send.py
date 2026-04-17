#!/usr/bin/env python3
"""
One-shot Huginn client. Sends a single message, streams JSON responses to stdout.
Usage: huginn_send.py <type> [content]
  huginn_send.py chat "what's up"
  huginn_send.py clear
  huginn_send.py ping
"""
import sys
import json
import socket
from pathlib import Path

SOCKET_PATH = str(Path.home() / ".local/share/huginn/huginn.sock")


def main() -> None:
    msg_type = sys.argv[1] if len(sys.argv) > 1 else "ping"

    if msg_type == "voice_file":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        tts  = sys.argv[3] == "true" if len(sys.argv) > 3 else False
        payload: dict = {"type": "voice_file", "path": path, "tts": tts}
    elif msg_type == "confirm":
        confirm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        approved   = sys.argv[3] == "true" if len(sys.argv) > 3 else False
        payload: dict = {"type": "confirm", "id": confirm_id, "approved": approved}
    elif msg_type == "switch_model":
        profile = sys.argv[2] if len(sys.argv) > 2 else ""
        payload: dict = {"type": "switch_model", "profile": profile}
    elif msg_type == "chat":
        # argv: chat <tts:true|false> <content...>
        tts     = sys.argv[2] == "true" if len(sys.argv) > 2 else False
        content = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        payload: dict = {"type": "chat", "content": content, "tts": tts}
    else:
        payload: dict = {"type": msg_type}
        if len(sys.argv) > 2:
            payload["content"] = " ".join(sys.argv[2:])

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCKET_PATH)
            s.sendall((json.dumps(payload) + "\n").encode())

            buf = ""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode()
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    print(line, flush=True)
                    try:
                        obj = json.loads(line)
                        if obj.get("type") in ("done", "cleared", "pong", "error", "confirm_ack", "model_switched"):
                            return
                    except json.JSONDecodeError:
                        pass
    except (FileNotFoundError, ConnectionRefusedError):
        print(json.dumps({"type": "error", "message": "daemon offline"}), flush=True)
    except Exception as e:
        print(json.dumps({"type": "error", "message": str(e)}), flush=True)


if __name__ == "__main__":
    main()
