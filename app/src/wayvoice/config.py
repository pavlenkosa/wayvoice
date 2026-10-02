from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "engine": "faster-whisper",
    "model": "small",
    "language": "ru",
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
}


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "wayvoice"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    data = dict(DEFAULTS)
    path = config_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    return data


def save_config(data: dict[str, Any]) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update(data)
    config_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
