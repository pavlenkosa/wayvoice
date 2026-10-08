from __future__ import annotations

import contextlib
import fcntl
import json
import os
import queue
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .audio import AudioRecorder
from .config import DEFAULTS as _CONFIG_DEFAULTS, config_error, load_config, number
from .engine import (
    DEFAULT_ENGINE,
    TranscriptionCancelled,
    TranscriptionTimeout,
    engine_from_config,
    get_engine,
    stop_jobs,
    cancel_jobs,
    engine_status,
    model_state,
    prepare_model,
    request_engine_setup,
    transcribe,
)
from .injector import InjectionError, inject
from .i18n import tr
from .notify import notify, reset_notification_id
from .protocol import owner_lock_path, socket_path
from .shortcut import label_for

#: How long one client may take to send its request line. Generous for a command of
#: a few bytes over a unix socket, and short enough that a client which connects and
#: then says nothing cannot hold the daemon: the accept loop serves one connection at a
#: time, so such a client takes the hotkey down with it.
CLIENT_TIMEOUT = 5.0

#: Commands answered on the accept loop itself, because they are questions and must
#: not queue behind work that takes time. ``quit`` belongs here too: it only sets a
#: flag, and answering it first is what lets the window close cleanly.
INLINE_COMMANDS = frozenset({"ping", "status", "clear-status", "quit"})

#: How many commands may wait for the worker at once. A queue that grew without
#: bound would turn a stuck command into a daemon that accepts requests and answers
#: none of them.
COMMAND_QUEUE = 32

#: How long the shutdown waits for the command worker to finish the command it is
#: holding: long enough for a recorder to flush its WAV header, short enough that
#: ``quit`` does not feel like a hang.
COMMAND_DRAIN_TIMEOUT = 5.0
JOB_DRAIN_TIMEOUT = 5.0

#: Longest request the daemon reads. Real commands are tens of bytes; anything
#: bigger is a client that is broken or hostile, and reading it into memory would be
#: its decision, not ours.
MAX_REQUEST_BYTES = 64 * 1024


def _send(conn: socket.socket, reply: dict) -> None:
    """Write one reply and nothing else; the caller closes the connection."""
    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))


def _close(conn: socket.socket) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _recording_limit(cfg: dict[str, Any]) -> int:
    """How many seconds one recording may last, or ``ValueError``.

    The value comes out of a JSON file a person can edit, and the two places that
    read it are the line that opens the microphone and a timer thread. A
    ``ValueError`` from ``int()`` in the second one kills the timer, so the limit
    never fires; in the first it arrives with the recorder already running. Both
    want the same thing - a number, or a refusal that names the setting.
    """
    raw = cfg.get("max_recording_sec", _CONFIG_DEFAULTS["max_recording_sec"])
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"max_recording_sec must be a number of seconds, not {raw!r}"
        ) from None
    # The floor is here rather than at the two call sites, so the warning the user
    # is shown and the timer that is armed cannot disagree about the limit.
    return max(5, seconds)


def _sweep_stale_recordings(max_age: float = 3600.0) -> int:
    """Remove recordings left behind by a daemon that was killed outright.

    ``cancel()`` and the shutdown hook cover every orderly exit, but a SIGKILL or a power
    loss leaves the file behind, at 32 kB for every second of speech nobody will transcribe
    again. Only files older than ``max_age`` are touched: a recording in progress cannot
    be older than the longest limit the user can configure.
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
        self._transcribe_thread: threading.Thread | None = None
        self._transcribe_wav: Path | None = None
        self._record_timer: threading.Timer | None = None
        self._record_started = 0.0
        self._busy_started = 0.0
        #: Progress of the background model preparation, and how to stop it.
        self._download: dict = {
            "state": "idle", "model": "", "done_bytes": 0, "total_bytes": 0,
            "error": "", "warming": False,
        }
        self._prepare_thread: threading.Thread | None = None
        #: Set before the thread starts and cleared when it ends. A thread that has
        #: been created but not started is not alive yet, and asking about the thread
        #: alone let two callers through, which started two downloads of one file.
        self._prepare_running = False
        self._prepare_cancel = threading.Event()

    def prepare_on_start(self) -> None:
        """Get ready for the first dictation, now that this daemon owns the session.

        Called from :meth:`serve` after the ownership lock is held, and not from ``__init__``:
        a second daemon that is about to be refused must change nothing first. Preparing the
        engine writes a status file, and warming the worker stops the running daemon's worker
        before starting its own, leaving the pid file owned by a process about to exit.
        """
        _sweep_stale_recordings()
        cfg = load_config()
        engine = engine_from_config(cfg)
        if engine is not None and engine.needs_setup:
            self._prepare_engine(engine, engine_status(cfg))
        # Warm the worker with the model that is already on disk, so the first
        # dictation of the session costs the same as every one after it. Nothing is
        # fetched here: a download starts when the user asks for one in the settings.
        self._start_model_prepare(cfg, download=False)

    @staticmethod
    def _prepare_engine(engine, status: dict) -> None:
        """Start the selected engine's setup, if it can be prepared at all.

        An engine that needs no preparation (whisper.cpp, an external command) is skipped:
        asking anyway would prepare a runtime nothing will use.
        """
        if status.get("state") in {"missing", "error"}:
            request_engine_setup(engine)

    # ------------------------------------------------------------------
    # Getting the model ready before it is needed
    # ------------------------------------------------------------------
    def _start_model_prepare(self, cfg: dict | None = None, download: bool = True) -> str:
        """Get the selected model ready: fetch it if asked to, then warm it.

        Returns the phase that was *started* - ``"downloading"`` or ``"warming"`` - read
        before the thread has had a chance to finish. Reporting the state the background work
        had reached by then would be a race, and a race that answers "ready" for a load that
        has just begun.

        Only one run at a time: two downloads of the same 3 GB file would fight over the same
        cache, and the second one's progress would be the first one's failure.

        A model that is already on disk is never fetched - the daemon asks this on every start
        and the answer must not involve the network - so ``download=False`` is what startup
        uses. Asking to prepare a model that *is* on disk warms it rather than reporting
        "already done", which used to leave someone who had just picked a different model
        facing a slow first dictation with nothing having told them it was coming.
        """
        if self._shutdown.is_set():
            return ""
        settings = dict(cfg or load_config())
        engine = engine_from_config(settings)
        state = model_state(engine, settings)
        if not state["supported"]:
            return ""
        if state["present"]:
            # Nothing to fetch. Warming it is still the point of being asked, and it
            # needs a worker to warm into - the user may have turned that off.
            if not settings.get("engine_worker", True):
                return ""
            download = False
        elif not download:
            # Asked to warm up a model that is not there.
            return ""
        with self._lock:
            if self._shutdown.is_set() or self._prepare_running:
                return ""
            self._prepare_running = True
            self._prepare_cancel.clear()
            phase = "downloading" if download else "warming"
            self._download = {
                "state": phase,
                "model": state["model"],
                "done_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warming": not download,
            }
            thread = threading.Thread(
                target=self._prepare_model_worker,
                args=(settings, dict(self._download), download),
                daemon=True,
            )
            self._prepare_thread = thread
        try:
            with self._lock:
                if self._shutdown.is_set():
                    self._prepare_running = False
                    self._prepare_thread = None
                    return ""
            thread.start()
        except Exception as exc:
            with self._lock:
                self._prepare_running = False
                self._prepare_thread = None
                self._download = {"state": "error", "model": state["model"],
                                  "done_bytes": 0, "total_bytes": 0,
                                  "error": str(exc), "warming": False}
            return ""
        return phase

    def _prepare_model_worker(self, cfg: dict, reported: dict, download: bool) -> None:
        """Fetch the model when asked to, then let the warm worker hold it.

        Runs on its own thread and keeps its hands off the daemon's locks: a dictation may be
        waiting on the hot key while three gigabytes come down.
        """
        engine = engine_from_config(cfg)

        def on_progress(done: int, total: int) -> None:
            with self._lock:
                # Never resurrect a download the user cancelled, and never overwrite
                # the error of one that has already finished.
                if self._download.get("state") != "downloading":
                    return
                self._download["done_bytes"] = int(done)
                self._download["total_bytes"] = int(total)

        def on_warming() -> None:
            with self._lock:
                if self._download.get("state") == "downloading":
                    # From the user's side this is the same wait: the weights are down and
                    # the model is going into memory. Reporting it only in the final
                    # answer would leave the window at 100% for the whole load.
                    self._download["warming"] = True

        try:
            result = prepare_model(
                engine, cfg, on_progress, self._prepare_cancel, on_warming, download
            )
        except TranscriptionCancelled:
            # A cancelled preparation is not a failure: "cancelled" is a state the
            # engine's own contract lists, and the window shows it as such.
            result = {"state": "cancelled"}
        except Exception as exc:
            # prepare_model is documented to answer with a dict, but a value it does
            # not expect - a hand-edited config, a helper that died in a way it does
            # not recognise - raises instead. Without this the thread ended there,
            # and because _prepare_running was only cleared on the success path it
            # stayed set: the settings button and `wayvoice model --download` were
            # then refused for the rest of the session, and the window's spinner
            # turned over a download that was not happening.
            traceback.print_exc()
            result = {"state": "error", "error": str(exc)}
        finally:
            with self._lock:
                if not isinstance(result, dict):
                    result = {"state": "error", "error": str(result)}
                self._download = {
                    "state": str(result.get("state") or "ready"),
                    "model": reported.get("model", ""),
                    "done_bytes": int(result.get("done") or 0),
                    "total_bytes": int(result.get("total") or 0),
                    "error": str(result.get("error") or ""),
                    # Whether the warm-up succeeded, not whether one is running: the
                    # reply ends the work, and a window still saying "preparing"
                    # would never stop.
                    "warming": False,
                }
                # In the finally, so that no path out of this thread can leave the
                # daemon permanently unable to start another preparation.
                self._prepare_running = False
                self._prepare_thread = None

    def _cancel_model_prepare(self) -> str:
        """Stop a running preparation; report which part was actually stopped.

        The two halves are not alike. Cancelling a download stops the transfer and the model
        is not going to be there; what follows the weights is a read into memory, which cannot
        be half-done and takes no notice of the flag, so saying "stopped" about that would be
        a lie the window would wait on.
        """
        with self._lock:
            download = dict(self._download)
            running = self._prepare_running
            if not running:
                phase = "nothing"
            elif download.get("warming") or download.get("state") == "warming":
                phase = "warming"
            else:
                phase = "download"
                self._prepare_cancel.set()
        return phase

    def _model_report(self, cfg: dict) -> dict:
        """Model state for :meth:`status`, merged with any run in flight."""
        engine = engine_from_config(cfg)
        state = model_state(engine, cfg)
        # A read snapshot is sufficient for progress; status must not wait for
        # lifecycle I/O (for example pw-record finalizing its WAV under the lock).
        download = dict(self._download)
        return {
            "supported": state["supported"],
            "present": state["present"],
            "model": state["model"],
            "download": download,
            # Warming is not a download, but from the user's side it is the same wait:
            # the model is not in memory yet and dictation will be slow.
            "warming": bool(download.get("warming")) or download.get("state") == "warming",
        }

    def _reconcile_recorder(self, blocking=True) -> str | None:
        """Consume a lost recording under lifecycle ownership; status never waits."""
        if not self._lock.acquire(blocking=blocking):
            return None
        try:
            probe = getattr(self.recorder, "take_failure", None)
            error = probe() if callable(probe) else None
            if not isinstance(error, str) or not error:
                return None
            self._cancel_record_timer()
            self._record_started = 0.0
            self.last_error = error
            reset_notification_id()
            return error
        finally:
            self._lock.release()

    def status(self) -> dict:
        self._reconcile_recorder(blocking=False)
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
            "model": self._model_report(cfg),
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
            if self._reconcile_recorder():
                return
            if not self.recorder.recording:
                return
            cfg = load_config()
            try:
                seconds = _recording_limit(cfg)
            except ValueError:
                # The start path refuses a limit it cannot read, so by the time this
                # timer fires the value has only just become bad. Falling back to the
                # default is the point: an exception here would kill the timer thread
                # and the recording would never be closed at all.
                seconds = max(5, int(_CONFIG_DEFAULTS["max_recording_sec"]))
            self.last_warning = tr("daemon.recording_limit", cfg.get("ui_language"), seconds=seconds)
        notify(self.last_warning, enabled=cfg.get("notify", True))
        self.stop_recording()

    def start_recording(self) -> dict:
        with self._lock:
            failure = self._reconcile_recorder()
            if failure:
                return {"ok": False, "error": failure}
            if self._shutdown.is_set():
                return {"ok": False, "error": "Daemon is shutting down."}
            if self.busy:
                return {
                    "ok": False,
                    "error": tr("daemon.busy_recognizing", self._language()),
                }
            if self.recorder.recording:
                return {"ok": True, "state": "recording"}
            # Read the limit before the microphone opens AND before engine/model preflight.
            # A hand-edited config can hold anything, and this used to be parsed *after*
            # the recorder was running: the ValueError arrived with pw-record already
            # holding the device and no timer to close it, so the recording continued
            # until the next keypress - which then transcribed minutes of room noise.
            # This validation must happen before any engine/model readiness check, so
            # a malformed config is reported even when the engine is not prepared.
            cfg = load_config()
            try:
                max_seconds = _recording_limit(cfg)
            except ValueError as exc:
                self.last_error = str(exc)
                return {"ok": False, "error": str(exc)}
            est = engine_status(cfg)
            if est.get("state") != "ready":
                engine = engine_from_config(cfg)
                if engine is not None and engine.needs_setup:
                    self._prepare_engine(engine, est)
                return {"ok": False, "error": est.get("message", "Recognition engine is not ready.")}
            model = self._model_report(cfg)
            if model["supported"] and not model["present"]:
                # Recording into a dictation that cannot happen yet: the model would be
                # fetched mid-transcription, a silent wait of minutes and then a
                # timeout.
                #
                # Pressing the key is not agreeing to spend the bandwidth. The window
                # asks before a download and the user may have said no, so this says
                # what is missing and where it can be fetched.
                return {
                    "ok": False,
                    "error": tr(
                        "daemon.model_missing", cfg.get("ui_language"),
                        model=model["model"],
                    ),
                }
            try:
                self.last_error = ""
                self.last_warning = ""
                self._transcribe_cancel.clear()
                self.recorder.start()
                self._record_started = time.monotonic()
                self._cancel_record_timer()
                self._record_timer = threading.Timer(max_seconds, self._auto_stop_recording)
                self._record_timer.daemon = True
                self._record_timer.start()
                # A dictation gets its own notification, and the states inside it
                # update that one. Forgetting the previous id here is what starts
                # a new entry: without it the next recording would overwrite the
                # transcript of the last one.
                reset_notification_id()
                notify(tr("daemon.recording_started", cfg.get("ui_language")), enabled=cfg.get("notify", True), replace=False)
                return {"ok": True, "state": "recording"}
            except Exception as exc:
                # Startup owns the take until both recorder and timer are ready.
                # Roll back even if failure follows opening the microphone.
                self._cancel_record_timer()
                self._record_started = 0.0
                error = str(exc)
                try:
                    self.recorder.cancel()
                except Exception as cleanup_exc:
                    error += f" Could not clean up recording: {cleanup_exc}"
                self.last_error = error
                # The notification cannot raise (see notify.notify) and must not be the
                # last thing in the handler either: an exception here would escape
                # start_recording entirely.
                notify(error, enabled=cfg.get("notify", True))
                return {"ok": False, "error": error}

    def cancel(self) -> dict:
        with self._lock:
            self._reconcile_recorder()
            cfg = load_config()
            if self.recorder.recording:
                self._cancel_record_timer()
                try:
                    self.recorder.cancel()
                except Exception as exc:
                    self.last_error = str(exc)
                    return {"ok": False, "error": str(exc)}
                self._record_started = 0.0
                notify(tr("daemon.recording_cancelled", cfg.get("ui_language")), enabled=cfg.get("notify", True))
                reset_notification_id()
                return {"ok": True, "state": "idle"}
            if self.busy:
                self._transcribe_cancel.set()
                return {"ok": True, "state": "cancelling"}
            return {"ok": True, "state": "idle"}

    def stop_recording(self) -> dict:
        with self._lock:
            failure = self._reconcile_recorder()
            if failure:
                return {"ok": False, "error": failure}
            if self._shutdown.is_set():
                return {"ok": False, "error": "Daemon is shutting down."}
            if not self.recorder.recording:
                return {
                    "ok": False,
                    "error": tr("daemon.not_recording", self._language()),
                }
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

        with self._lock:
            if self._shutdown.is_set():
                self.busy = False
                self._busy_started = 0.0
                wav.unlink(missing_ok=True)
                return {"ok": False, "error": "Daemon is shutting down."}
            thread = threading.Thread(target=self._transcribe_worker, args=(wav,), daemon=True)
            self._transcribe_thread = thread
            self._transcribe_wav = wav
        try:
            thread.start()
        except Exception as exc:
            self._transcribe_thread = None
            self._transcribe_wav = None
            self.busy = False
            self._busy_started = 0.0
            if not self._shutdown.is_set():
                self._transcribe_cancel.clear()
            wav.unlink(missing_ok=True)
            self.last_error = str(exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "state": "transcribing"}

    @staticmethod
    def _language() -> str | None:
        """The user's language, for a reply that has no config in hand.

        Some replies are refusals raised before anything has read the settings - an unknown
        command, a request for a second dictation. They are still shown, so they are still
        translated, at the cost of one small file read on a path that has just refused.
        """
        return load_config().get("ui_language")

    def request_shutdown(self) -> None:
        """Ask the serving loop to stop and clean up.

        Public because a signal handler has no business reaching into ``_shutdown``: setting
        the flag is the whole contract, and stopping the recorder and removing the socket
        happens in the ordinary way afterwards.
        """
        self._shutdown.set()

    def toggle(self) -> dict:
        failure = self._reconcile_recorder()
        if failure:
            return {"ok": False, "error": failure}
        if self.recorder.recording:
            return self.stop_recording()
        if self.busy:
            return self.cancel()
        return self.start_recording()

    def _transcribe_worker(self, wav: Path) -> None:
        cfg = {}
        try:
            cfg = load_config()
            if self._transcribe_cancel.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            notify(tr("daemon.transcribing", cfg.get("ui_language")), enabled=cfg.get("notify", True))
            text = transcribe(wav, cfg, self._transcribe_cancel)
            self.last_text = text.strip()
            self.last_error = ""
            self.last_warning = ""
            if not text.strip():
                self.last_warning = tr("daemon.no_speech", cfg.get("ui_language"))
                notify(self.last_warning, enabled=cfg.get("notify", True))
                return
            try:
                # Cancel means cancel. Recognition checks the flag while it decodes, but
                # nothing between here and the injection looked at it, so a cancel in
                # the last milliseconds used to be ignored and the text was typed into
                # whatever window the user had switched to.
                if self._transcribe_cancel.is_set():
                    self.last_text = ""
                    self.last_warning = tr(
                        "daemon.recognition_cancelled", cfg.get("ui_language")
                    )
                    notify(
                        self.last_warning,
                        enabled=cfg.get("notify", True),
                    )
                    return
                result = inject(text, cfg)
                if result.warning:
                    self.last_warning = result.warning
                    notify(result.warning, enabled=cfg.get("notify", True))
                else:
                    notify(tr("daemon.text_inserted", cfg.get("ui_language")), text.strip()[:160], enabled=cfg.get("notify", True))
            except InjectionError as exc:
                self.last_error = str(exc)
                notify(str(exc), enabled=cfg.get("notify", True))
        except TranscriptionCancelled:
            self.last_error = ""
            self.last_warning = tr("daemon.recognition_cancelled", cfg.get("ui_language"))
            notify(self.last_warning, enabled=cfg.get("notify", True))
        except TranscriptionTimeout:
            timeout = number(cfg, "transcription_timeout_sec", 90)
            self.last_error = ""
            self.last_warning = tr("daemon.recognition_timeout", cfg.get("ui_language"), seconds=timeout)
            notify(self.last_warning, enabled=cfg.get("notify", True))
        except Exception as exc:
            traceback.print_exc()
            self.last_error = str(exc)
            notify(str(exc), enabled=cfg.get("notify", True))
        finally:
            # The state reset comes first and the unlink cannot raise into it.
            # ``missing_ok=True`` covers exactly one exception, and this used to be
            # the other way round: a PermissionError here escaped the finally, so
            # ``busy`` stayed set and the daemon refused every hot-key press until
            # somebody restarted it - after the text had already been delivered.
            with self._lock:
                self.busy = False
                self._busy_started = 0.0
                if not self._shutdown.is_set():
                    self._transcribe_cancel.clear()
            # The result is the last thing this notification says. It stays in the
            # history, and the next dictation starts a new entry instead of
            # overwriting a transcript the user may still be reading.
            reset_notification_id()
            try:
                wav.unlink(missing_ok=True)
            except OSError as exc:
                # A leftover recording is swept at the next start; it is not worth
                # turning into an error state after the text is already inserted.
                print(f"WayVoice: could not remove {wav}: {exc}", file=sys.stderr)
            finally:
                with self._lock:
                    if self._transcribe_thread is threading.current_thread():
                        self._transcribe_thread = None
                        self._transcribe_wav = None


    def dispatch(self, command: str) -> dict:
        if self._shutdown.is_set() and command.strip().lower() not in {"ping", "status", "quit"}:
            return {"ok": False, "state": "shutting_down", "error": "Daemon is shutting down."}
        raw = command.strip()
        name, _, target_json = raw.partition(" ")
        addressed = name.lower() == "prepare-model" and bool(target_json)
        command = name.lower() if addressed else raw.lower()
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
        if command == "prepare-model":
            cfg = dict(load_config())
            if addressed:
                try:
                    target = json.loads(target_json)
                    if not isinstance(target, dict) or set(target) != {"engine", "model"}:
                        raise ValueError("Expected engine and model")
                    if any(not isinstance(target[key], str) or not target[key].strip()
                           for key in ("engine", "model")):
                        raise ValueError("Engine and model must be nonempty strings")
                    if any(ord(ch) < 32 for ch in target["model"]):
                        raise ValueError("Invalid model name")
                    if get_engine(target["engine"]) is None:
                        raise ValueError("Unknown engine")
                except (ValueError, TypeError) as exc:
                    return {"ok": False, "state": "invalid_target", "error": str(exc)}
                cfg.update(target)
                # A Settings draft prepares files, never replaces the live dictation
                # worker. Save/startup (or the bare CLI) owns warming saved settings.
                cfg["engine_worker"] = False
            phase = self._start_model_prepare(cfg)
            if phase:
                # What was started, not what was hoped for: a model that was on
                # disk is warmed rather than fetched, and a caller that says
                # "downloading" for both would promise the user a progress bar
                # that never moves.
                return {"ok": True, "state": phase, "model": cfg.get("model"),
                        "engine": cfg.get("engine", DEFAULT_ENGINE)}
            with self._lock:
                if self._prepare_running:
                    return {"ok": False, "state": "busy", "model": cfg.get("model"),
                            "active_model": self._download.get("model"),
                            "error": tr("daemon.model_prepare_busy", cfg.get("ui_language"))}
            state = self._model_report(cfg)
            if not state["supported"]:
                # A local directory or an engine without hub models. There is
                # nothing to fetch and nothing is wrong, so this says which of
                # the two it is rather than reporting a failure to prepare.
                return {
                    "ok": False,
                    "error": tr("daemon.model_not_downloadable", cfg.get("ui_language")),
                    "state": "not_applicable",
                }
            if state["present"]:
                return {"ok": True, "state": "ready", "model": cfg.get("model"),
                        "engine": cfg.get("engine", DEFAULT_ENGINE)}
            # Not started: a download is already running, or one just failed.
            download = state["download"]
            return {
                "ok": False,
                "error": str(download.get("error") or tr("daemon.model_prepare_busy", cfg.get("ui_language"))),
            }
        if command == "cancel-download":
            phase = self._cancel_model_prepare()
            return {
                "ok": phase != "nothing",
                "phase": phase,
                # Kept for a window built against the older reply, which only
                # knew whether anything was running.
                "stopped": phase != "nothing",
            }
        if command == "engine-setup":
            cfg = load_config()
            engine = engine_from_config(cfg)
            if engine is None:
                # A broken config, not an engine without setup: saying the
                # latter would hide the actual problem.
                engine_id = str(cfg.get("engine", DEFAULT_ENGINE))
                return {
                    "ok": False,
                    "error": tr(
                        "daemon.unknown_engine", self._language(), engine=engine_id
                    ),
                }
            if request_engine_setup(engine):
                return {"ok": True}
            # Nothing was started, so say why instead of replying "ok" to a
            # request that did not happen.
            return {
                "ok": False,
                "error": tr(
                    "daemon.engine_needs_no_setup", self._language(), engine=engine.label
                ),
            }
        if command == "ping":
            return {"ok": True, "pong": True}
        if command == "quit":
            self._shutdown.set()
            return {"ok": True}
        return {
                "ok": False,
                "error": tr("daemon.unknown_command", self._language(), command=command),
            }

    def _command_loop(self, commands: "queue.Queue") -> None:
        """Serve the commands that may take time, one at a time.

        One worker, not a pool: the hot key, the microphone button and the window all expect a
        command to finish before the next one starts, and the daemon's own lock enforces that.
        What the worker buys is that the accept loop - which has to answer a status poll within
        0.12 s - is never the thread doing the waiting.

        A command that raises must not take the worker with it; every client behind it would
        hang until it timed out.
        """
        while True:
            item = commands.get()
            if item is None:
                return
            conn, command = item
            try:
                reply = self.dispatch(command)
            except Exception as exc:
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                _send(conn, reply)
            except OSError:
                pass
            _close(conn)

    def _live_daemon(self, path: Path) -> bool:
        """Return whether another daemon already owns ``path``.

        Without this a second daemon would unlink the running daemon's socket, bind one at the
        same path and orphan the first: unreachable through the filesystem, yet still holding
        the microphone and the clipboard. systemd's unit used to make that impossible, but
        :mod:`wayvoice.service` may spawn the daemon directly, so the refusal belongs here.

        A socket nobody answers on is a leftover from a crash and is safe to replace, which is
        what the unlink below is for. The probe is a real ``ping``: opening a connection and
        dropping it would learn nothing and risk an EPIPE on the running daemon's side.
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
            # Only now, with the session known to be ours: preparing the engine
            # or warming the worker changes state the running daemon shares, and
            # a duplicate that did it first would stop the real daemon's worker
            # and leave one of its own behind.  See prepare_on_start().
            self.prepare_on_start()
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
        commands: queue.Queue = queue.Queue(maxsize=COMMAND_QUEUE)
        worker = threading.Thread(
            target=self._command_loop, args=(commands,), daemon=True
        )
        worker.start()
        try:
            while not self._shutdown.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                try:
                    # The accepted socket is blocking again (CPython undoes the
                    # listener's timeout for it), so the read has to get its own
                    # deadline. Without it a client that connects and stays quiet
                    # blocks this loop - and with it the hot key - until the
                    # daemon is restarted.
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
                except OSError:
                    # A client that hung up mid-request, and one whose request
                    # took longer than CLIENT_TIMEOUT.
                    _close(conn)
                    continue
                if not data:
                    # A client that connected and left again (a probe, or a
                    # window that was closed) must not cost us the daemon:
                    # answering it would only raise EPIPE here and take the whole
                    # accept loop down with it.  The same path swallows a client
                    # that sent nothing at all within the deadline, and one that
                    # tried to make us buffer its whole output.
                    _close(conn)
                    continue
                command = data.decode("utf-8", "replace").strip()
                if command in INLINE_COMMANDS:
                    # The questions are answered here, on the loop, and cost a fraction of a
                    # millisecond each. Everything that can take time goes to the command
                    # worker below, because the window asks for the status every 650 ms
                    # with a 0.12 s deadline and would otherwise call a busy daemon dead.
                    try:
                        _send(conn, self.dispatch(command))
                    except OSError:
                        pass
                    _close(conn)
                    continue
                try:
                    commands.put_nowait((conn, command))
                except queue.Full:
                    # More work than the daemon can be doing at once, which means something
                    # is stuck. The client is told so rather than left waiting for a
                    # reply that would never come.
                    try:
                        _send(conn, {
                            "ok": False,
                            "error": tr("daemon.too_busy", self._language()),
                        })
                    except OSError:
                        pass
                    _close(conn)
        finally:
            try:
                self._stop_work()
            finally:
                self._drain_commands(commands, worker, server, path)

    def _stop_work(self) -> None:
        """Let go of everything the daemon is holding, on the way out.

        The recognizer is told to stop *first*. It used to be told last, after the
        recorder had been cancelled - and cancelling deletes the file that
        ``stop_to_wav()`` handed to the transcription thread, so every ``quit`` or
        SIGTERM during a dictation deleted the audio out from under the engine and
        produced a ``FileNotFoundError`` where the user should have seen a cancelled
        recognition.
        """
        self._shutdown.set()
        deadline = time.monotonic() + JOB_DRAIN_TIMEOUT
        self._shutdown_deadline = deadline
        self._cancel_record_timer()
        with self._lock:
            active_worker = cancel_jobs(self._transcribe_cancel, self._prepare_cancel)
        # A download that outlived the daemon would keep fetching a file nobody is
        # waiting for, in a session that is going away.
        # Leaving a recording behind is not a cleanup detail: pw-record keeps the
        # microphone open and keeps writing to /tmp, so a daemon that exits
        # mid-dictation holds the device until something kills that process.
        try:
            self.recorder.cancel()
        except Exception as exc:  # never let this stop the shutdown
            print(f"WayVoice: could not stop the recorder: {exc}", file=sys.stderr)
        # Force registered children first. Their own owner threads reap, close
        # pipes and remove WAVs; never delete a recording while ASR still reads it.
        stop_jobs(self._transcribe_cancel, self._prepare_cancel, deadline=deadline, active_worker=active_worker)
        with self._lock:
            threads = (self._transcribe_thread, self._prepare_thread)
            if self._transcribe_thread is not None and self._transcribe_thread.ident is None:
                # A published thread can be stuck in start(). It checks cancellation
                # before reading audio if it eventually begins, so nobody owns a read.
                if self._transcribe_wav is not None:
                    try:
                        self._transcribe_wav.unlink(missing_ok=True)
                    except OSError as exc:
                        print(f"WayVoice: could not remove {self._transcribe_wav}: {exc}", file=sys.stderr)
                self.busy = False
                self._busy_started = 0.0
        for thread in threads:
            if thread is not None and thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))


    def _drain_commands(self, commands: "queue.Queue", worker: threading.Thread,
                        server: socket.socket, path: Path) -> None:
        """Close the socket after the command worker, and never because of the queue.

        ``put_nowait(None)`` is the one line here that can fail: the enqueue path
        handles a full queue explicitly, and the shutdown path did not. A full queue
        therefore raised out of the ``finally``, leaving the socket file behind and
        the queued clients without a reply.
        """
        try:
            commands.put_nowait(None)
        except queue.Full:
            pass
        remaining = max(0.0, getattr(self, "_shutdown_deadline", time.monotonic() + COMMAND_DRAIN_TIMEOUT) - time.monotonic())
        worker.join(timeout=min(COMMAND_DRAIN_TIMEOUT, remaining))
        server.close()
        path.unlink(missing_ok=True)


def main() -> None:
    daemon = WayVoiceDaemon()
    _install_signal_handlers(daemon)
    daemon.serve()


def _install_signal_handlers(daemon: WayVoiceDaemon) -> None:
    """Ask the daemon to stop when something signals it.

    Without a handler SIGTERM ended the process outright: no ``atexit`` hook, no
    ``finally``, and the recorder - in a session of its own so that its WAV header is
    finalised deliberately - left holding the microphone with nobody to stop it. That is
    what systemd sends on stop, and what :func:`wayvoice.service._force_stop_daemon` sends
    to a daemon that ignored ``quit``.

    Setting the flag is all that is needed; the accept loop wakes within its own timeout
    and the ordinary cleanup runs. SIGHUP counts as the same event, SIGINT does not: it
    already raises and unwinds through that same ``finally``.
    """
    def request_stop(_signum, _frame):
        daemon.request_shutdown()

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, request_stop)
        except (OSError, ValueError):
            # Not the main thread, or a platform without that signal: the
            # daemon then keeps the default disposition, as it always did.
            pass


if __name__ == "__main__":
    main()
