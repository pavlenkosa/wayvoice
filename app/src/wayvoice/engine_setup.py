from __future__ import annotations
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .engine import faster_runtime, faster_stamp, setup_status_path

#: Longest a single pip invocation may take.  Without it a network that stops
#: delivering leaves the status at "installing" forever: the window spins with
#: no cancel button, and every dictation attempt spawns another setup process
#: that then waits on the lock below.
PIP_TIMEOUT = 1800.0

#: How long a second setup process waits for the lock before deciding that one
#: is already running.  The holder does the work; the waiter has nothing to do.
LOCK_TIMEOUT = 2.0


def _write(state: str, message: str, log: str = "") -> None:
    path = setup_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"state": state, "message": message, "log": log}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def _install_once(runtime: Path, log) -> None:
    if not (runtime / "bin/python").exists():
        if runtime.exists():
            shutil.rmtree(runtime)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "venv", str(runtime)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=300.0,
        )

    python = runtime / "bin/python"
    # faster-whisper <=1.2.x still calls av.open(..., metadata_errors=...).
    # PyAV 19 removed that argument, so pin PyAV to the compatible series.
    subprocess.run(
        [
            str(python), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--upgrade",
            "av>=11,<19",
            "faster-whisper>=1.1.1,<2",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=PIP_TIMEOUT,
    )

    # Fail setup here rather than on the user's first dictation.
    probe = subprocess.run(
        [
            str(python), "-c",
            "import av, faster_whisper; "
            "m=int(av.__version__.split('.')[0]); "
            "assert m < 19, av.__version__; "
            "print('PyAV', av.__version__, 'faster-whisper', faster_whisper.__version__)",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120.0,
    )
    if probe.returncode != 0:
        raise RuntimeError("проверка совместимости Faster-Whisper/PyAV не пройдена")


def _take_lock(lock) -> bool:
    """Take the setup lock, waiting briefly for a holder that is finishing.

    ``flock`` in blocking mode has no way out: a crashed holder releases the
    lock, but a holder that is stuck (pip waiting on a network) keeps every
    later process waiting forever.  Polling with a deadline gives the common
    case - the previous attempt is about to finish - and gives up otherwise.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def main() -> int:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    log_dir = state_home / "wayvoice"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "engine-setup.log"
    lock_path = data_home / "wayvoice" / "engine-setup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w") as lock:
        if not _take_lock(lock):
            # Somebody else is already preparing the engine. Say so and leave:
            # waiting here would pile up one blocked process per dictation
            # attempt, and would keep reporting "installing" after the real
            # attempt has already failed.
            print("wayvoice-engine-setup: another setup is running", file=sys.stderr)
            return 0
        runtime = faster_runtime()
        stamp = faster_stamp()
        if stamp.exists() and (runtime / "bin/python").exists():
            _write("ready", "Готов", str(log_path))
            return 0

        _write("installing", "Подготавливаю движок…", str(log_path))
        last_exc: Exception | None = None
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== WayVoice engine setup {__version__} ===\n")
            for attempt in (1, 2):
                try:
                    _install_once(runtime, log)
                    for old in runtime.glob(".engine-*-ready"):
                        old.unlink(missing_ok=True)
                    stamp.touch()
                    _write("ready", "Готов", str(log_path))
                    return 0
                except Exception as exc:
                    last_exc = exc
                    log.write(f"\nAttempt {attempt} failed: {exc}\n")
                    log.flush()
                    if attempt == 1:
                        # If an old/partial environment is broken, rebuild it cleanly.
                        shutil.rmtree(runtime, ignore_errors=True)
                        _write("installing", "Повторяю подготовку…", str(log_path))

        tail = ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = " · ".join(x.strip() for x in lines[-3:] if x.strip())
        except Exception:
            pass
        message = f"Не удалось подготовить движок: {last_exc}"
        if tail:
            message += f". {tail[-300:]}"
        _write("error", message, str(log_path))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
