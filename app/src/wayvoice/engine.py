from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any

from .models import forced_language
from .paths import script_path
from .postprocess import normalize

ENGINE_LABELS = {
    "faster-whisper": "Faster-Whisper",
    "whisper-cpp": "whisper.cpp",
    "custom": "External command",
}

RUNTIME_STAMP = ".engine-v1-ready"


class TranscriptionCancelled(RuntimeError):
    pass


class TranscriptionTimeout(RuntimeError):
    pass


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def faster_runtime() -> Path:
    return _data_home() / "wayvoice" / "runtime"


def faster_stamp() -> Path:
    return faster_runtime() / RUNTIME_STAMP


def setup_status_path() -> Path:
    return _state_home() / "wayvoice" / "engine-status.json"


def _read_setup_status() -> dict[str, Any]:
    try:
        raw = json.loads(setup_status_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def request_faster_setup() -> None:
    try:
        subprocess.Popen(
            ["systemctl", "--user", "--no-block", "start", "wayvoice-engine-setup.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _find_whisper_cpp(cfg: dict[str, Any]) -> str | None:
    explicit = str(cfg.get("whisper_cpp_binary") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None
    for name in ("whisper-cli", "whisper.cpp", "main"):
        found = shutil.which(name)
        if found:
            return found
    return None


def engine_status(cfg: dict[str, Any]) -> dict[str, Any]:
    engine = str(cfg.get("engine", "faster-whisper"))
    label = ENGINE_LABELS.get(engine, engine)

    if engine == "faster-whisper":
        runtime = faster_runtime()
        # Accept previous WayVoice-ready stamps and migrate lazily. This avoids
        # a needless runtime rebuild after every application update.
        ready_stamps = list(runtime.glob(".engine-*-ready")) if runtime.exists() else []
        if (faster_stamp().exists() or ready_stamps) and (runtime / "bin/python").exists():
            if ready_stamps and not faster_stamp().exists():
                try:
                    faster_stamp().touch()
                except Exception:
                    pass
            return {"id": engine, "label": label, "state": "ready", "message": "Ready"}
        status = _read_setup_status()
        if status.get("state") == "installing":
            return {"id": engine, "label": label, "state": "installing", "message": str(status.get("message") or "Preparing…")}
        if status.get("state") == "error":
            return {
                "id": engine,
                "label": label,
                "state": "error",
                "message": str(status.get("message") or "Faster-Whisper setup failed"),
                "log": str(status.get("log") or ""),
            }
        return {"id": engine, "label": label, "state": "missing", "message": "Faster-Whisper is not prepared"}

    if engine == "whisper-cpp":
        binary = _find_whisper_cpp(cfg)
        model = Path(str(cfg.get("whisper_cpp_model") or "")).expanduser()
        if not binary:
            return {"id": engine, "label": label, "state": "missing", "message": "whisper-cli was not found"}
        if not model.is_file():
            return {"id": engine, "label": label, "state": "missing", "message": "Select a whisper.cpp GGML/GGUF model"}
        return {"id": engine, "label": label, "state": "ready", "message": f"Ready · {Path(binary).name}"}

    if engine == "custom":
        command = str(cfg.get("custom_command") or "").strip()
        if not command:
            return {"id": engine, "label": label, "state": "missing", "message": "Configure a command that prints text to stdout"}
        return {"id": engine, "label": label, "state": "ready", "message": "Ready"}

    return {"id": engine, "label": label, "state": "error", "message": "Unknown recognition engine"}


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=1.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_cancelable(
    args: list[str],
    *,
    timeout: float,
    cancel_event: Event | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    started = time.monotonic()
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process(proc)
            proc.communicate()
            raise TranscriptionCancelled("Transcription cancelled")
        if timeout > 0 and time.monotonic() - started >= timeout:
            _terminate_process(proc)
            proc.communicate()
            raise TranscriptionTimeout(f"Transcription exceeded {int(timeout)} seconds")
        time.sleep(0.12)
    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _postprocess(text: str, cfg: dict[str, Any]) -> str:
    text = text.strip()
    if cfg.get("auto_punctuation", True):
        text = normalize(
            text,
            spoken_punctuation=bool(cfg.get("spoken_punctuation", True)),
            ensure_terminal_punctuation=bool(cfg.get("ensure_terminal_punctuation", True)),
        )
    if text and cfg.get("append_space", True):
        text += " "
    return text


def _transcribe_faster(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    runtime_python = faster_runtime() / "bin/python"
    if not runtime_python.exists():
        raise RuntimeError("Faster-Whisper is not ready")
    runner = str(script_path("fw_runner.py"))
    args = [
        str(runtime_python), runner,
        "--audio", str(audio),
        "--model", str(cfg.get("model", "small")),
        "--language", str(forced_language(str(cfg.get("model", "small"))) or cfg.get("language", "ru") or "auto"),
        "--device", str(cfg.get("device", "auto")),
        "--beam-size", str(int(cfg.get("beam_size", 5))),
    ]
    if cfg.get("vad_filter", True):
        args.append("--vad")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    cp = _run_cancelable(
        args,
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
        cancel_event=cancel_event,
        env=env,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Faster-Whisper failed")
    return (cp.stdout or "").strip()


def _transcribe_whisper_cpp(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    binary = _find_whisper_cpp(cfg)
    model = Path(str(cfg.get("whisper_cpp_model") or "")).expanduser()
    if not binary:
        raise RuntimeError("whisper-cli was not found")
    if not model.is_file():
        raise RuntimeError("whisper.cpp model was not found")
    language = str(cfg.get("language", "ru") or "auto")
    args = [binary, "-m", str(model), "-f", str(audio), "-l", language, "-nt", "-np"]
    if not bool(cfg.get("whisper_cpp_gpu", True)):
        args.append("-ng")
    cp = _run_cancelable(
        args,
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
        cancel_event=cancel_event,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "whisper.cpp failed")
    return (cp.stdout or "").strip()


def _transcribe_custom(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    template = str(cfg.get("custom_command") or "").strip()
    if not template:
        raise RuntimeError("External command is not configured")
    rendered = template.replace("{audio}", shlex.quote(str(audio)))
    cp = _run_cancelable(
        ["/bin/sh", "-lc", rendered],
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
        cancel_event=cancel_event,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "External recognition command failed")
    return (cp.stdout or "").strip()


def transcribe(audio: Path, cfg: dict[str, Any], cancel_event: Event | None = None) -> str:
    engine = str(cfg.get("engine", "faster-whisper"))
    if engine == "faster-whisper":
        raw = _transcribe_faster(audio, cfg, cancel_event)
    elif engine == "whisper-cpp":
        raw = _transcribe_whisper_cpp(audio, cfg, cancel_event)
    elif engine == "custom":
        raw = _transcribe_custom(audio, cfg, cancel_event)
    else:
        raise RuntimeError(f"Unknown engine: {engine}")
    return _postprocess(raw, cfg)
