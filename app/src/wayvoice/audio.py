from __future__ import annotations
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
        proc, path = self._proc, self._path
        self._proc = None
        self._path = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
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
        return path
