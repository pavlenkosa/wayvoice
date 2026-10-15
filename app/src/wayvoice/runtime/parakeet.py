"""Experimental CPU Parakeet through NeMo-Speech.cpp's local-file CLI.

Verified contract: NeMo-Speech.cpp v0.2.0, Parakeet TDT 0.6B v3 Q8_0 GGUF.
No model aliases, downloads, optional pipelines or shell execution on dictation.
"""
import fcntl
import json
import os
from pathlib import Path

from ..config import number
from . import processes
from ..paths import app_dir
from ..parakeet_setup import default_root, RUNTIME_NAME, MODEL_NAME


def assets(cfg):
    bundled = app_dir().parent / 'parakeet' / 'bin/nemo-speech'
    default_binary = bundled if bundled.is_file() else default_root() / RUNTIME_NAME / 'bin/nemo-speech'
    binary = Path(str(cfg.get('parakeet_binary') or default_binary)).expanduser().resolve()
    model = Path(str(cfg.get('parakeet_model') or default_root() / MODEL_NAME)).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('Select a NeMo-Speech.cpp v0.2.0 CPU executable')
    try:
        with model.open('rb') as stream:
            valid = model.stat().st_size >= 32 and stream.read(4) == b'GGUF'
    except OSError:
        valid = False
    if not valid:
        raise RuntimeError('Select a prepared Parakeet TDT 0.6B v3 GGUF model')
    return binary, model


def status(cfg):
    try:
        lock_path = default_root() / '.prepare.lock'
        if lock_path.exists():
            with lock_path.open('r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {'state': 'installing', 'message': 'Preparing Parakeet CPU runtime and model'}
        assets(cfg)
    except (OSError, RuntimeError, ValueError) as exc:
        return {'state': 'missing', 'message': str(exc)}
    return {'state': 'ready', 'message': 'Ready · experimental CPU · automatic language'}


def command(audio, cfg):
    binary, model = assets(cfg)
    # Absolute paths cannot be interpreted as a model catalogue alias, even if
    # the model disappears between validation and opening it in the child.
    return [str(binary), 'transcribe', str(Path(audio).resolve()), '--model', str(model),
            '--device', 'cpu', '--json']


def transcribe(audio, cfg, cancel_event=None):
    cp = processes._run_cancelable(command(audio, cfg),
                                   timeout=number(cfg, 'transcription_timeout_sec', 90),
                                   cancel_event=cancel_event)
    if cp.returncode:
        detail = (cp.stderr or '').strip().splitlines()
        raise RuntimeError(detail[-1] if detail else 'Parakeet failed')
    try:
        result = json.loads(cp.stdout)
        if not isinstance(result, dict) or not isinstance(result.get('text'), str) or 'error' in result:
            raise ValueError('missing text')
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Invalid Parakeet output') from exc
    return result['text'].strip()
