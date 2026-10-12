from __future__ import annotations
import json
import math
import sys
import os
import tempfile
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "engine": "faster-whisper",
    "model": "small",
    # "auto" rather than a pinned default: a wrong fixed language does not fail
    # loudly, it transcribes foreign speech with the wrong grammar and spelling.
    "language": "auto",
    "device": "auto",
    "microphone": "",
    "audio_feedback": False,
    "beam_size": 5,
    "vad_filter": True,
    "auto_punctuation": True,
    "spoken_punctuation": True,
    "ensure_terminal_punctuation": True,
    "paste_mode": "standard",
    "append_space": True,
    "vocabulary_enabled": False,
    "vocabulary_rules": "",
    "notify": True,
    "notify_transcript": False,
    "auto_check_updates": False,
    "shortcut": "F8",
    "shortcut_mode": "toggle",
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


# Legacy keys never affected recognition. Accept old files without carrying the
# unsupported knobs into the active config or the next explicit save.
_RETIRED_KEYS = frozenset({"compute_type_cpu", "compute_type_cuda"})

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
    """Why the last :func:`load_config` replaced unusable settings, or ``""``."""
    return _LAST_ERROR


def _valid_setting(key: str, value: Any) -> bool:
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    valid = isinstance(value, type(default))
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            valid = not isinstance(value, bool) and math.isfinite(float(value))
            int(value)
        except (TypeError, ValueError, OverflowError):
            valid = False
    if key == "vocabulary_rules" and valid:
        from .vocabulary import parse_rules
        try:
            parse_rules(value)
        except ValueError:
            valid = False
    if key == "shortcut_mode" and (not isinstance(value, str) or value not in {"toggle", "hold"}):
        valid = False
    return valid


def load_config(*, strict: bool = False) -> dict[str, Any]:
    """Load safe fallbacks; mutation callers use strict mode to preserve damage."""
    global _LAST_ERROR

    data = dict(DEFAULTS)
    path = config_path()
    error = ""
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                invalid = []
                for key, value in loaded.items():
                    if key in _RETIRED_KEYS:
                        continue
                    if key in DEFAULTS:
                        valid = _valid_setting(key, value)
                        if not valid:
                            invalid.append(key)
                            continue
                    data[key] = value
                if invalid:
                    error = (
                        f"{path}: invalid setting values: {', '.join(invalid)}; "
                        "using defaults for these keys"
                    )
            else:
                error = f"{path} does not contain an object"
        except Exception as exc:
            error = f"{path}: {exc}"
    _LAST_ERROR = error
    if strict and error:
        raise ValueError(error)
    return data


class ConfigConflictError(ValueError):
    def __init__(self, keys):
        self.keys = tuple(sorted(keys))
        super().__init__("Settings changed in another window: " + ", ".join(self.keys)
                         + ". Reopen Settings before saving again.")


@contextmanager
def _config_lock():
    path = config_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Settings are being saved by another process")
                time.sleep(0.025)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def update_config(changes: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch against current disk state, rejecting same-field conflicts.

    Missing optional keys compare as None. The caller supplies expected values for
    every changed key; unchanged fields always come from the latest disk snapshot.
    """
    if changes.keys() - expected.keys():
        raise ValueError("Expected values are required for every changed setting")
    with _config_lock():
        current = load_config(strict=True)
        conflicts = [key for key, value in changes.items()
                     if current.get(key) != expected[key] and current.get(key) != value]
        if conflicts:
            raise ConfigConflictError(conflicts)
        invalid = [key for key, value in changes.items() if not _valid_setting(key, value)]
        if invalid:
            raise ValueError("Invalid setting values: " + ", ".join(invalid))
        merged = dict(current, **changes)
        _write_config(merged)
        return merged


def save_config(data: dict[str, Any]) -> None:
    """Compatibility full replacement; production edits use update_config."""
    with _config_lock():
        load_config(strict=True)
        _write_config(data)


def _write_config(data: dict[str, Any]) -> None:
    merged = dict(DEFAULTS)
    merged.update({key: value for key, value in data.items() if key not in _RETIRED_KEYS})
    path = config_path()
    # Written to a temporary file and renamed, never in place: the daemon reads this
    # several times a minute, and a reader that catches a half-written config falls
    # back to DEFAULTS - a different engine, model, language or recording limit,
    # with nothing said. engine_setup does the same for its status file.
    # Unique private temporary files avoid collisions between independent writers.
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json.tmp", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def number(cfg: dict, key: str, default):
    """Read a numeric setting, falling back to ``default`` when it is not one.

    ``config.json`` is a file a person can edit and ``load_config`` copies it over
    the defaults, and callers can also supply partial dictionaries, so conversion
    needs a floor. Without one, a single ``"beam_size": null`` raises somewhere deep
    in the engine - or, worse, in the settings window, where an exception during
    construction means no window at all, and an exception inside a GLib timeout means
    the timer is gone for the rest of the session.

    The type of ``default`` decides the type of the answer, so an integer setting
    comes back as an integer.
    """
    value = cfg.get(key, default)
    try:
        if isinstance(default, bool):
            return bool(value)
        result = int(value) if isinstance(default, int) else float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite number")
        return result
    except (TypeError, ValueError, OverflowError):
        print(f"WayVoice: {key}={value!r} is not a number, using {default!r}",
              file=sys.stderr)
        return default
