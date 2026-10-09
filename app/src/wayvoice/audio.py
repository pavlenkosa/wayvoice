from __future__ import annotations
import atexit
import os
import shutil
import signal
import subprocess
import tempfile
import wave
from pathlib import Path

from .deps import describe_missing


class AudioRecorder:
    """PipeWire recorder with no Python audio dependencies."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None
        # A recorder that outlives the process that started it keeps the microphone
        # open and keeps writing to /tmp, and nothing else knows it is there. The
        # daemon stops it on the way out; this hook covers the paths that bypass
        # that, an interpreter shutdown after a crash in another thread say.
        atexit.register(self.cancel)

    @property
    def recording(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @staticmethod
    def _stderr_message(proc) -> str:
        stream = proc.stderr
        if stream is None:
            return ""
        try:
            os.set_blocking(stream.fileno(), False)
        except (AttributeError, TypeError):
            pass  # In-memory streams used by callers/tests have no OS descriptor.
        except (OSError, ValueError):
            return ""
        try:
            return (stream.read(8192) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _close_stderr(proc) -> None:
        if proc is not None and proc.stderr is not None:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def take_failure(self) -> str | None:
        """Consume an unexpected exit once, without waiting for a live recorder."""
        proc = self._proc
        if proc is None:
            return None
        code = proc.poll()
        if code is None:
            return None
        message = self._stderr_message(proc) or f"PipeWire recording stopped unexpectedly (exit code {code})."
        try:
            self.cancel()
        except OSError as exc:
            message += f" Could not remove recording: {exc}"
        return message

    def start(self) -> None:
        if self.recording:
            return
        failure = self.take_failure()
        if failure:
            raise RuntimeError(failure)
        # A stopped take remains ours until unlink succeeds. Do not overwrite
        # its path with a new recording after a filesystem cleanup failure.
        if self._path is not None:
            self.cancel()
        if not shutil.which("pw-record"):
            raise RuntimeError(describe_missing("pipewire"))

        fd, name = tempfile.mkstemp(prefix="wayvoice-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        # Keep the private inode: libsndfile truncates it without replacing its
        # 0600 permissions. Unlinking here recreated it using the caller umask.

        cmd = [
            "pw-record",
            "--rate=16000",
            "--channels=1",
            "--channel-map=mono",
            str(path),
        ]
        self._path = path
        try:
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
        except Exception as exc:
            try:
                self.cancel()
            except Exception as cleanup_exc:
                raise RuntimeError(f"{exc} Could not remove recording: {cleanup_exc}") from exc
            raise

        # Detect immediate PipeWire failures instead of pretending to record.
        try:
            self._proc.wait(timeout=0.08)
        except subprocess.TimeoutExpired:
            return

        error = self.take_failure()
        raise RuntimeError(error or "Не удалось открыть микрофон через PipeWire.")

    def cancel(self) -> None:
        """Stop the recorder and remove the recording nobody ever took.

        A file that ``stop_to_wav()`` has already handed out is *not* removed here.
        It belongs to the transcription thread, and this runs first on the way out -
        so deleting it turned every quit or SIGTERM during a dictation into a
        ``FileNotFoundError`` inside the recognizer. If that thread never runs, the
        daemon owns cleanup of recordings handed to transcription.
        """
        proc, path = self._proc, self._path
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    # Reap it: an unreaped child stays in the process table as a zombie
                    # for as long as this daemon lives, and a daemon that is cancelled
                    # often would accumulate one per dictation.
                    proc.wait(timeout=0.5)
        finally:
            # A failed signal/wait does not transfer ownership. Keep the live
            # process, private take and stderr available for a later cancel.
            if proc is None or proc.poll() is not None:
                self._proc = None
                try:
                    if path:
                        path.unlink(missing_ok=True)
                    self._path = None
                finally:
                    self._close_stderr(proc)

    def stop_to_wav(self) -> Path:
        failure = self.take_failure()
        if failure:
            raise RuntimeError(failure)
        proc, path = self._proc, self._path
        if proc is None or path is None:
            raise RuntimeError("Запись не активна.")
        interrupted = False
        forced = False
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                interrupted = True
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    forced = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.7)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=0.5)
            code = proc.poll()
            if code not in (0, -signal.SIGINT):
                error = self._stderr_message(proc)
                # pw-cat 1.4.2 returns 1 after a clean recording SIGINT: only
                # playback reaching drained sets EXIT_SUCCESS. Accept that case
                # only after our graceful stop and a readable, complete PCM WAV.
                valid = False
                if code == 1 and interrupted and not forced and not error:
                    try:
                        with wave.open(str(path), "rb") as wav:
                            frames = wav.getnframes()
                            valid = (frames > 0 and wav.getnchannels() == 1
                                     and wav.getframerate() == 16000)
                            remaining = frames
                            while valid and remaining:
                                count = min(remaining, 16384)
                                valid = len(wav.readframes(count)) == count * wav.getsampwidth()
                                remaining -= count
                    except (OSError, EOFError, wave.Error):
                        pass
                if not valid:
                    raise RuntimeError(error or f"PipeWire could not finish the recording (exit code {code}).")
            if not path.exists() or path.stat().st_size < 128:
                raise RuntimeError(self._stderr_message(proc) or "Запись микрофона получилась пустой.")
        except Exception as exc:
            try:
                self.cancel()
            except Exception as cleanup_exc:
                raise RuntimeError(f"{exc} Could not stop recording: {cleanup_exc}") from exc
            raise
        finally:
            if proc.poll() is not None:
                self._close_stderr(proc)
        self._proc = None
        self._path = None
        return path
