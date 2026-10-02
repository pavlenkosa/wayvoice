from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .audio import AudioRecorder
from .config import load_config
from .engine import (
    TranscriptionCancelled,
    TranscriptionTimeout,
    engine_status,
    request_faster_setup,
    transcribe,
)
from .injector import InjectionError, inject
from .i18n import tr
from .notify import notify
from .protocol import socket_path
from .shortcut import label_for


class WayVoiceDaemon:
    def __init__(self) -> None:
        self.recorder = AudioRecorder()
        self.busy = False
        self.last_text = ""
        self.last_error = ""
        self.last_warning = ""
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._transcribe_cancel = threading.Event()
        self._record_timer: threading.Timer | None = None
        self._record_started = 0.0
        self._busy_started = 0.0
        cfg = load_config()
        if cfg.get("engine", "faster-whisper") == "faster-whisper":
            st = engine_status(cfg)
            if st.get("state") in {"missing", "error"}:
                request_faster_setup()

    def status(self) -> dict:
        cfg = load_config()
        now = time.monotonic()
        return {
            "version": __version__,
            "recording": self.recorder.recording,
            "busy": self.busy,
            "recording_seconds": round(max(0.0, now - self._record_started), 1) if self.recorder.recording and self._record_started else 0.0,
            "busy_seconds": round(max(0.0, now - self._busy_started), 1) if self.busy and self._busy_started else 0.0,
            "last_text": self.last_text,
            "last_error": self.last_error,
            "last_warning": self.last_warning,
            "engine": engine_status(cfg),
            "shortcut": label_for(str(cfg.get("shortcut", "F8"))),
        }

    def _cancel_record_timer(self) -> None:
        timer = self._record_timer
        self._record_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _auto_stop_recording(self) -> None:
        with self._lock:
            if not self.recorder.recording:
                return
            cfg = load_config()
            seconds = int(cfg.get("max_recording_sec", 120))
            self.last_warning = tr("daemon.recording_limit", cfg.get("ui_language"), seconds=seconds)
        notify("WayVoice", self.last_warning, enabled=cfg.get("notify", True))
        self.stop_recording()

    def start_recording(self) -> dict:
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "Recognition is still running."}
            if self.recorder.recording:
                return {"ok": True, "state": "recording"}
            cfg = load_config()
            est = engine_status(cfg)
            if est.get("state") != "ready":
                if cfg.get("engine") == "faster-whisper" and est.get("state") in {"missing", "error"}:
                    request_faster_setup()
                return {"ok": False, "error": est.get("message", "Recognition engine is not ready.")}
            try:
                self.last_error = ""
                self.last_warning = ""
                self._transcribe_cancel.clear()
                self.recorder.start()
                self._record_started = time.monotonic()
                self._cancel_record_timer()
                max_seconds = max(5, int(cfg.get("max_recording_sec", 120)))
                self._record_timer = threading.Timer(max_seconds, self._auto_stop_recording)
                self._record_timer.daemon = True
                self._record_timer.start()
                notify("WayVoice", tr("daemon.recording_started", cfg.get("ui_language")), enabled=cfg.get("notify", True))
                return {"ok": True, "state": "recording"}
            except Exception as exc:
                self.last_error = str(exc)
                notify("WayVoice", str(exc), enabled=cfg.get("notify", True))
                return {"ok": False, "error": str(exc)}

    def cancel(self) -> dict:
        with self._lock:
            cfg = load_config()
            if self.recorder.recording:
                self._cancel_record_timer()
                self.recorder.cancel()
                self._record_started = 0.0
                notify("WayVoice", tr("daemon.recording_cancelled", cfg.get("ui_language")), enabled=cfg.get("notify", True))
                return {"ok": True, "state": "idle"}
            if self.busy:
                self._transcribe_cancel.set()
                return {"ok": True, "state": "cancelling"}
            return {"ok": True, "state": "idle"}

    def stop_recording(self) -> dict:
        with self._lock:
            if not self.recorder.recording:
                return {"ok": False, "error": "Recording is not active."}
            self._cancel_record_timer()
            try:
                wav = self.recorder.stop_to_wav()
            except Exception as exc:
                self.last_error = str(exc)
                self._record_started = 0.0
                return {"ok": False, "error": str(exc)}
            self._record_started = 0.0
            self.busy = True
            self._busy_started = time.monotonic()
            self._transcribe_cancel.clear()

        threading.Thread(target=self._transcribe_worker, args=(wav,), daemon=True).start()
        return {"ok": True, "state": "transcribing"}

    def toggle(self) -> dict:
        if self.recorder.recording:
            return self.stop_recording()
        if self.busy:
            return self.cancel()
        return self.start_recording()

    def _transcribe_worker(self, wav: Path) -> None:
        cfg = load_config()
        try:
            notify("WayVoice", tr("daemon.transcribing", cfg.get("ui_language")), enabled=cfg.get("notify", True))
            text = transcribe(wav, cfg, self._transcribe_cancel)
            self.last_text = text.strip()
            self.last_error = ""
            self.last_warning = ""
            if not text.strip():
                self.last_warning = tr("daemon.no_speech", cfg.get("ui_language"))
                notify("WayVoice", self.last_warning, enabled=cfg.get("notify", True))
                return
            try:
                result = inject(text, cfg)
                if result.warning:
                    self.last_warning = result.warning
                    notify("WayVoice", result.warning, enabled=cfg.get("notify", True))
                else:
                    notify("WayVoice", tr("daemon.text_inserted", cfg.get("ui_language")), text.strip()[:160], enabled=cfg.get("notify", True))
            except InjectionError as exc:
                self.last_error = str(exc)
                notify("WayVoice", str(exc), enabled=cfg.get("notify", True))
        except TranscriptionCancelled:
            self.last_error = ""
            self.last_warning = tr("daemon.recognition_cancelled", cfg.get("ui_language"))
            notify("WayVoice", self.last_warning, enabled=cfg.get("notify", True))
        except TranscriptionTimeout:
            timeout = int(cfg.get("transcription_timeout_sec", 90))
            self.last_error = ""
            self.last_warning = tr("daemon.recognition_timeout", cfg.get("ui_language"), seconds=timeout)
            notify("WayVoice", self.last_warning, enabled=cfg.get("notify", True))
        except Exception as exc:
            traceback.print_exc()
            self.last_error = str(exc)
            notify("WayVoice", str(exc), enabled=cfg.get("notify", True))
        finally:
            wav.unlink(missing_ok=True)
            with self._lock:
                self.busy = False
                self._busy_started = 0.0
                self._transcribe_cancel.clear()

    def dispatch(self, command: str) -> dict:
        command = command.strip().lower()
        if command == "toggle":
            return self.toggle()
        if command == "start":
            return self.start_recording()
        if command == "stop":
            return self.stop_recording()
        if command == "cancel":
            return self.cancel()
        if command == "status":
            return {"ok": True, **self.status()}
        if command == "clear-status":
            self.last_error = ""
            self.last_warning = ""
            return {"ok": True}
        if command == "engine-setup":
            request_faster_setup()
            return {"ok": True}
        if command == "ping":
            return {"ok": True, "pong": True}
        if command == "quit":
            self._shutdown.set()
            return {"ok": True}
        return {"ok": False, "error": f"Unknown command: {command}"}

    def serve(self) -> None:
        path = socket_path()
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        try:
            while not self._shutdown.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    reply = self.dispatch(data.decode("utf-8", "replace").strip())
                    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            self._cancel_record_timer()
            server.close()
            path.unlink(missing_ok=True)


def main() -> None:
    WayVoiceDaemon().serve()


if __name__ == "__main__":
    main()
