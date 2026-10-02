from __future__ import annotations
import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .engine import faster_runtime, faster_stamp, setup_status_path


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
    )
    if probe.returncode != 0:
        raise RuntimeError("проверка совместимости Faster-Whisper/PyAV не пройдена")


def main() -> int:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    log_dir = state_home / "wayvoice"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "engine-setup.log"
    lock_path = data_home / "wayvoice" / "engine-setup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        runtime = faster_runtime()
        stamp = faster_stamp()
        if stamp.exists() and (runtime / "bin/python").exists():
            _write("ready", "Готов", str(log_path))
            return 0

        _write("installing", "Подготавливаю движок…", str(log_path))
        last_exc: Exception | None = None
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n=== WayVoice engine setup 0.5.0 ===\n")
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
