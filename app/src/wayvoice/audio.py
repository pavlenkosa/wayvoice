from __future__ import annotations
import atexit
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from .deps import describe_missing


class AudioRecorder:
    """PipeWire recorder with no Python audio dependencies."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None
        #: Recording handed to the caller and not yet deleted by it.
        self._finished: Path | None = None
        # A recorder that outlives the process that started it keeps the microphone
        # open and keeps writing to /tmp, and nothing else knows it is there. The
        # daemon stops it on the way out; this hook covers the paths that bypass
        # that, an interpreter shutdown after a crash in another thread say.
        atexit.register(self.cancel)

    @property
    def recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.recording:
            return
        if not shutil.which("pw-record"):
            raise RuntimeError(describe_missing("pipewire"))

        fd, name = tempfile.mkstemp(prefix="wayvoice-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        path.unlink(missing_ok=True)

        cmd = [
            "pw-record",
            "--rate=16000",
            "--channels=1",
            "--channel-map=mono",
            str(path),
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            # Its own session: pw-record holds the microphone, and a daemon that is
            # killed must not leave a recorder behind still holding it. Being in its
            # own session it also ignores the daemon's terminal signals, so it is
            # stopped deliberately - through stop_to_wav()/cancel().
            start_new_session=True,
        )
        self._path = path

        # Detect immediate PipeWire failures instead of pretending to record.
        try:
            self._proc.wait(timeout=0.08)
        except subprocess.TimeoutExpired:
            return

        err = ""
        if self._proc.stderr:
            err = self._proc.stderr.read().strip()
        self._proc = None
        self._path = None
        path.unlink(missing_ok=True)
        raise RuntimeError(err or "Не удалось открыть микрофон через PipeWire.")

    def cancel(self) -> None:
        """Stop the recorder and remove the recording nobody ever took.

        A file that ``stop_to_wav()`` has already handed out is *not* removed here.
        It belongs to the transcription thread, and this runs first on the way out -
        so deleting it turned every quit or SIGTERM during a dictation into a
        ``FileNotFoundError`` inside the recognizer. If that thread never runs, the
        recording is swept at the next start.
        """
        proc, path = self._proc, self._path
        self._proc = None
        self._path = None
        self._finished = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                # Reap it: an unreaped child stays in the process table as a zombie
                # for as long as this daemon lives, and a daemon that is cancelled
                # often would accumulate one per dictation.
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        if path:
            path.unlink(missing_ok=True)

    def stop_to_wav(self) -> Path:
        proc, path = self._proc, self._path
        if proc is None or path is None:
            raise RuntimeError("Запись не активна.")

        self._proc = None
        self._path = None
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=0.7)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.5)

        if not path.exists() or path.stat().st_size < 128:
            err = ""
            if proc.stderr:
                try:
                    err = proc.stderr.read().strip()
                except Exception:
                    pass
            path.unlink(missing_ok=True)
            raise RuntimeError(err or "Запись микрофона получилась пустой.")
        # Remember it: the caller owns it from here on and normally deletes it
        # itself, but if this process dies first, cancel() is what removes it.
        self._finished = path
        return path
