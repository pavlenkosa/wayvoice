from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "engine": "faster-whisper",
    "model": "small",
    # "auto" rather than a pinned default: a wrong fixed language does not fail
    # loudly, it transcribes foreign speech with the wrong grammar and spelling.
    "language": "auto",
    "device": "auto",
    "compute_type_cpu": "int8",
    "compute_type_cuda": "float16",
    "beam_size": 5,
    "vad_filter": True,
    "auto_punctuation": True,
    "spoken_punctuation": True,
    "ensure_terminal_punctuation": True,
    "paste_mode": "standard",
    "append_space": True,
    "notify": True,
    "shortcut": "F8",
    "whisper_cpp_binary": "",
    "whisper_cpp_model": "",
    "whisper_cpp_gpu": True,
    "custom_command": "",
    "custom_model": "",
    "ui_language": "auto",
    "transcription_timeout_sec": 90,
    "max_recording_sec": 120,
    # Warm worker: keeps the model in memory between dictations.  Turning it
    # off restores the one-shot runner for every dictation.
    "engine_worker": True,
    "engine_worker_idle_sec": 900,
}


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "wayvoice"


def config_path() -> Path:
    return config_dir() / "config.json"


#: Set when the last :func:`load_config` found a file it could not use. The daemon
#: keeps working on the defaults, but a broken config is reported rather than
#: silently replaced by defaults nobody chose.
_LAST_ERROR = ""


def config_error() -> str:
    """Why the last :func:`load_config` fell back to defaults, or ``""``."""
    return _LAST_ERROR


def load_config() -> dict[str, Any]:
    global _LAST_ERROR

    data = dict(DEFAULTS)
    path = config_path()
    _LAST_ERROR = ""
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
            else:
                _LAST_ERROR = f"{path} does not contain an object"
        except Exception as exc:
            _LAST_ERROR = f"{path}: {exc}"
    return data


def save_config(data: dict[str, Any]) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update(data)
    path = config_path()
    # Written to a temporary file and renamed, never in place: the daemon reads this
    # several times a minute, and a reader that catches a half-written config falls
    # back to DEFAULTS - a different engine, model, language or recording limit,
    # with nothing said. engine_setup does the same for its status file.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
