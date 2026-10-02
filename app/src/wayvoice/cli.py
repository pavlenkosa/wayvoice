from __future__ import annotations
import json
import os
import socket
import sys

from .config import load_config
from .engine import engine_status, request_faster_setup
from .protocol import socket_path
from .shortcut import apply_shortcut


def request(command: str, timeout: float = 1.5) -> dict:
    path = socket_path()
    if not path.exists():
        return {"ok": False, "error": "Фоновый сервис WayVoice ещё не запущен."}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall((command + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Сервис WayVoice не ответил: {exc}"}
    finally:
        sock.close()


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "settings"

    if command in {"settings", "ui", "config"}:
        os.execvp("wayvoice-settings", ["wayvoice-settings"])
    if command == "apply-shortcut":
        cfg = load_config()
        ok, msg = apply_shortcut(str(cfg.get("shortcut", "F8")))
        if not ok:
            print(msg, file=sys.stderr)
            raise SystemExit(1)
        return
    if command == "engine-setup":
        request_faster_setup()
        print("Запуск подготовки Faster-Whisper запрошен.")
        return
    if command == "engine-status":
        print(json.dumps(engine_status(load_config()), ensure_ascii=False, indent=2))
        return
    if command not in {"toggle", "start", "stop", "cancel", "status", "ping", "quit"}:
        print("Использование: wayvoice {toggle|start|stop|cancel|status|settings|engine-setup|engine-status}", file=sys.stderr)
        raise SystemExit(2)

    reply = request(command)
    if command == "status" or not reply.get("ok"):
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    if not reply.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
