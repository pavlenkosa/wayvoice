"""First-run choices and explicit, cancellable preparation, independent of GTK."""
from __future__ import annotations

import fcntl
import os
import sys
import time
from threading import Event
from typing import Callable

from . import config, engine
from .runtime import model_preparation, processes
from .i18n import SUPPORTED_UI_LANGUAGES
from .paths import app_src_dir

MODELS = ("tiny", "small", "medium")
# Approximate download totals for FETCH_PATTERNS, not RAM requirements.
# Source: https://huggingface.co/api/models/Systran/faster-whisper-{id}?blobs=true
# Checked 2026-10-10; upstream may change. Never fetch metadata while choosing.
MODEL_DOWNLOAD_BYTES = {"tiny": 78_203_619, "small": 486_212_372, "medium": 1_530_571_735}
SETUP_TIMEOUT = 4500.0  # Two pip attempts, venv creation and import probes.


def needs_onboarding() -> bool:
    if not config.config_path().exists():
        return True
    cfg = config.load_config()
    # Existing users and damaged settings use the ordinary settings/recovery UI.
    return not config.config_error() and cfg.get("onboarding_completed") is False


def finish_config(language: str, model: str, deferred: bool = False) -> dict:
    """Build choices without saving, touching services or downloading anything."""
    if language not in SUPPORTED_UI_LANGUAGES or model not in MODELS:
        raise ValueError("Unsupported first-run language or model")
    cfg = config.load_config(strict=True)
    cfg.update(ui_language=language, engine="faster-whisper", model=model,
               device="cpu", language="auto", onboarding_deferred=bool(deferred))
    return cfg


def selection_changes(language: str, model: str, completed: bool, deferred: bool,
                      expected: dict) -> dict:
    if language not in SUPPORTED_UI_LANGUAGES or model not in MODELS:
        raise ValueError("Unsupported first-run language or model")
    desired = dict(ui_language=language, engine="faster-whisper", model=model,
                   device="cpu", language="auto", onboarding_deferred=bool(deferred),
                   onboarding_completed=bool(completed))
    return {key: value for key, value in desired.items() if value != expected.get(key)}


def save_selection(language: str, model: str, completed: bool, deferred: bool = False,
                   *, expected: dict | None = None) -> dict:
    baseline = config.load_config(strict=True) if expected is None else dict(expected)
    changes = selection_changes(language, model, completed, deferred, baseline)
    return config.update_config(changes, {key: baseline.get(key) for key in changes})


def _check_cancel(event: Event) -> None:
    if event.is_set():
        raise engine.TranscriptionCancelled("Preparation cancelled")


def _setup_running() -> bool:
    # A status file can outlive a cancelled/crashed setup. Observe its lock rather
    # than treating that stale marker as a process we must wait for.
    # engine_setup keeps the lock in the standard data home even with an override.
    path = engine._data_home() / "wayvoice" / "engine-setup.lock"
    try:
        with path.open("r") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def _wait_runtime(cfg: dict, event: Event) -> dict:
    """Observe somebody else's setup; cancellation never stops that process."""
    deadline = time.monotonic() + SETUP_TIMEOUT
    while True:
        _check_cancel(event)
        state = engine.engine_status(cfg)
        if state.get("state") != "installing":
            return state
        if not _setup_running():
            raise RuntimeError("Engine preparation stopped before becoming ready")
        if time.monotonic() >= deadline:
            raise engine.TranscriptionTimeout("Engine preparation timed out")
        event.wait(0.25)


def prepare_selection(
    cfg: dict,
    cancel_event: Event,
    on_progress: Callable[[int, int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> dict:
    """Prepare only after an explicit user action; called off the GTK thread.

    Own setup subprocesses use the engine's process-group cleanup. Existing setup
    belongs to its original caller. Model preparation avoids a warm worker, whose
    lifetime belongs to the dictation daemon rather than this dialog.
    """
    target = dict(cfg)
    if target.get("engine") != "faster-whisper" or target.get("model") not in MODELS:
        raise ValueError("Unsupported first-run engine or model")
    _check_cancel(cancel_event)
    state = engine.engine_status(target)
    if state.get("state") != "ready":
        if on_phase:
            on_phase("runtime")
        if state.get("state") != "installing" or not _setup_running():
            env = dict(os.environ)
            env["PYTHONPATH"] = str(app_src_dir()) + os.pathsep + env.get("PYTHONPATH", "")
            result = processes._run_cancelable(
                [sys.executable, "-m", "wayvoice.engine_setup"],
                timeout=SETUP_TIMEOUT, cancel_event=cancel_event, env=env,
            )
            if result.returncode:
                state = engine.engine_status(target)
                raise RuntimeError(state.get("message") or result.stderr or "Engine preparation failed")
        state = _wait_runtime(target, cancel_event)
        if state.get("state") != "ready":
            raise RuntimeError(state.get("message") or "Engine preparation failed")
    _check_cancel(cancel_event)
    if on_phase:
        on_phase("download")
    target["engine_worker"] = False
    result = model_preparation.prepare_model(
        engine.engine_from_config(target), target, on_progress=on_progress,
        cancel_event=cancel_event, download=True,
    )
    _check_cancel(cancel_event)
    if result.get("state") == "cancelled":
        raise engine.TranscriptionCancelled("Preparation cancelled")
    if result.get("state") != "ready":
        raise RuntimeError(result.get("error") or "Model preparation failed")
    return result
