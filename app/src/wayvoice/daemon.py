from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .audio import AudioRecorder
from .config import config_error, load_config
from .engine import (
    DEFAULT_ENGINE,
    TranscriptionCancelled,
    TranscriptionTimeout,
    engine_from_config,
    engine_status,
    request_engine_setup,
    transcribe,
)
from .injector import InjectionError, inject
from .i18n import tr
from .notify import notify
from .protocol import owner_lock_path, socket_path
from .shortcut import label_for

#: How long one client may take to send its request line.  Generous for a
#: command of a few bytes over a unix socket, and short enough that a client
#: which connects and then says nothing cannot hold the daemon: the accept loop
#: serves one connection at a time, so a client that never finishes its line
#: takes the hotkey down with it.
CLIENT_TIMEOUT = 5.0

#: Longest request the daemon reads.  Real commands are tens of bytes; anything
#: bigger is a client that is broken or hostile, and reading it into memory
#: would be its decision, not ours.
MAX_REQUEST_BYTES = 64 * 1024


def _sweep_stale_recordings(max_age: float = 3600.0) -> int:
    """Remove recordings left behind by a daemon that was killed outright.

    ``cancel()`` and the shutdown hook cover every orderly exit, but a SIGKILL
    or a power loss leaves the file behind, and at 16 kHz mono that is 32 kB for
    every second of speech nobody is ever going to transcribe again.  Only files
    older than ``max_age`` are touched: a recording in progress cannot be older
    than the longest limit the user can configure, so this cannot delete one.
    """
    removed = 0
    cutoff = time.time() - max_age
    try:
        candidates = list(Path(tempfile.gettempdir()).glob("wayvoice-*.wav"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed


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
        _sweep_stale_recordings()
        cfg = load_config()
        engine = engine_from_config(cfg)
        if engine is not None and engine.needs_setup:
            self._prepare_engine(engine, engine_status(cfg))

    @staticmethod
    def _prepare_engine(engine, status: dict) -> None:
        """Start the selected engine's setup, if it can be prepared at all.

        An engine that needs no preparation (whisper.cpp, an external command)
        is skipped: there is nothing to install, and asking for it anyway would
        prepare a runtime nothing will ever use.
        """
        if status.get("state") in {"missing", "error"}:
            request_engine_setup(engine)

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
            # The daemon is running on the defaults right now; say so instead of
            # letting the window report a configuration the user never chose.
            "config_error": config_error(),
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
                engine = engine_from_config(cfg)
                if engine is not None and engine.needs_setup:
                    self._prepare_engine(engine, est)
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
                # The notification itself cannot raise (see notify.notify), and
                # it must not be the last thing in the handler either: an
                # exception here would escape start_recording entirely.
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

        thread = threading.Thread(target=self._transcribe_worker, args=(wav,), daemon=True)
        try:
            thread.start()
        except Exception as exc:
            # Without this the daemon would keep ``busy`` set forever: recording
            # is refused, toggle turns into cancel, and only a restart of the
            # daemon clears it. The temporary recording would leak as well.
            self.busy = False
            self._busy_started = 0.0
            self._transcribe_cancel.clear()
            wav.unlink(missing_ok=True)
            self.last_error = str(exc)
            return {"ok": False, "error": str(exc)}
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
                # Cancel means cancel. Recognition checks the flag while it
                # decodes, but nothing between here and the injection looked at
                # it: a cancel that arrived in the last milliseconds used to be
                # ignored and the text was typed into whatever window the user
                # had switched to in the meantime.
                if self._transcribe_cancel.is_set():
                    self.last_text = ""
                    self.last_warning = tr(
                        "daemon.recognition_cancelled", cfg.get("ui_language")
                    )
                    notify(
                        "WayVoice",
                        self.last_warning,
                        enabled=cfg.get("notify", True),
                    )
                    return
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
            cfg = load_config()
            engine = engine_from_config(cfg)
            if engine is None:
                # A broken config, not an engine without setup: saying the
                # latter would hide the actual problem.
                engine_id = str(cfg.get("engine", DEFAULT_ENGINE))
                return {"ok": False, "error": f"Unknown recognition engine: {engine_id}"}
            if request_engine_setup(engine):
                return {"ok": True}
            # Nothing was started, so say why instead of replying "ok" to a
            # request that did not happen.
            return {"ok": False, "error": f"{engine.label} needs no preparation."}
        if command == "ping":
            return {"ok": True, "pong": True}
        if command == "quit":
            self._shutdown.set()
            return {"ok": True}
        return {"ok": False, "error": f"Unknown command: {command}"}

    def _live_daemon(self, path: Path) -> bool:
        """Return whether another daemon already owns ``path``.

        Without this check a second daemon would unlink the running daemon's
        socket, bind one at the same path and the first one would be orphaned:
        unreachable through the filesystem, yet still holding the microphone
        and the Wayland clipboard.  Under systemd this could not happen because
        the unit owned the lifetime, but :mod:`wayvoice.service` may now spawn
        the daemon directly (Flatpak has no systemctl), so a double start has
        to be refused here instead.

        A socket file that nobody answers on is a leftover from a crash and is
        safe to replace, which is what the unlink below is for.  The probe is a
        single real ``ping``: opening a connection and dropping it would only
        teach the running daemon nothing while risking an EPIPE on its side.
        """
        if not path.exists():
            return False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        try:
            client.connect(str(path))
            client.sendall(b"ping\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = client.recv(256)
                if not chunk:
                    break
                data += chunk
        except OSError:
            return False
        finally:
            client.close()
        try:
            return bool(json.loads(data.decode("utf-8", "replace")).get("ok"))
        except (ValueError, AttributeError):
            # Something answers on the socket but not in our protocol; treat it
            # as occupied rather than stealing a path we do not understand.
            return True

    def serve(self) -> None:
        path = socket_path()
        # The lock comes first and is authoritative: a second daemon must be
        # turned away even when the first one is alive but not answering,
        # because a daemon that is not answering is exactly the case where the
        # probe below would hand the socket over and orphan a process that is
        # still holding the microphone.
        with contextlib.closing(open(owner_lock_path(), "w")) as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print(
                    "WayVoice: another daemon already owns this session; exiting.",
                    file=sys.stderr,
                )
                return
            self._serve_locked(path)

    def _serve_locked(self, path: Path) -> None:
        if self._live_daemon(path):
            print(
                "WayVoice: another daemon already owns the socket; exiting.",
                file=sys.stderr,
            )
            return
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
                    try:
                        # The accepted socket is blocking again (CPython undoes
                        # the listener's timeout for it), so the read has to get
                        # its own deadline. Without it a client that connects
                        # and stays quiet blocks this loop - and with it the hot
                        # key - until the daemon is restarted.
                        conn.settimeout(CLIENT_TIMEOUT)
                        data = b""
                        while not data.endswith(b"\n"):
                            if len(data) > MAX_REQUEST_BYTES:
                                data = b""
                                break
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        if not data:
                            # A client that connected and left again (a probe,
                            # or a window that was closed) must not cost us the
                            # daemon: answering it would only raise EPIPE here
                            # and take the whole accept loop down with it. The
                            # same path swallows a client that sent nothing at
                            # all within the deadline, and one that tried to
                            # make us buffer its whole output.
                            continue
                        reply = self.dispatch(data.decode("utf-8", "replace").strip())
                        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                    except OSError:
                        # Same for a client that hung up mid-reply, and for one
                        # whose request took longer than CLIENT_TIMEOUT.
                        continue
        finally:
            self._cancel_record_timer()
            # Leaving a recording behind is not a cleanup detail: pw-record keeps
            # the microphone open and keeps writing to /tmp, so a daemon that
            # exits mid-dictation would hold the device until something kills
            # that process by hand.
            try:
                self.recorder.cancel()
            except Exception as exc:  # never let this stop the shutdown
                print(f"WayVoice: could not stop the recorder: {exc}", file=sys.stderr)
            self._transcribe_cancel.set()
            server.close()
            path.unlink(missing_ok=True)


def main() -> None:
    WayVoiceDaemon().serve()


if __name__ == "__main__":
    main()
