"""Idle-only selection of prepared local recognition configurations."""
import time

from . import languages
from .config import load_config, update_config
from .engine import engine_status
from . import model_store
from .models import MODEL_PRESETS, display_name, forced_language

FIELDS = ('engine', 'model', 'language')


def prepared_choices(cfg):
    """Local probes only; never prepare a runtime or download a model."""
    choices = []
    faster = dict(cfg, engine='faster-whisper')
    if engine_status(faster).get('state') == 'ready':
        models = [str(item['id']) for item in MODEL_PRESETS if item['id'] != '__custom__']
        current = str(cfg.get('model', 'small'))
        if current not in models:
            models.append(current)
        for model in models:
            try:
                model_store.inference_dir(model)
            except (OSError, RuntimeError, ValueError):
                continue
            else:
                choices.append({'engine': 'faster-whisper', 'model': model,
                                'label': f'Faster-Whisper · {display_name(model)}',
                                'forced_language': forced_language(model)})
    cpp = dict(cfg, engine='whisper-cpp')
    if engine_status(cpp).get('state') == 'ready':
        choices.append({'engine': 'whisper-cpp', 'model': str(cfg.get('model', 'small')),
                        'label': 'whisper.cpp', 'forced_language': None})
    parakeet = dict(cfg, engine="parakeet")
    if engine_status(parakeet).get("state") == "ready":
        choices.append({"engine": "parakeet", "model": str(cfg.get("model", "small")),
                        "label": "Parakeet v3 · experimental CPU", "forced_language": "auto"})
    return choices


def select_prepared(daemon, payload):
    """The lifecycle lock serializes selection with capture/update readiness."""
    from .updater import updating
    try:
        if not isinstance(payload, dict) or set(payload) != {'changes', 'expected', 'expires'}:
            raise ValueError('Invalid recognition selection')
        changes, expected = payload['changes'], payload['expected']
        if not isinstance(changes, dict) or not isinstance(expected, dict):
            raise ValueError('Invalid recognition selection')
        if set(changes) - set(FIELDS) or set(expected) != set(FIELDS):
            raise ValueError('Only engine, model and language may be selected')
        expires = float(payload['expires'])
        with daemon._lock:
            if not time.monotonic() < expires <= time.monotonic() + 30:
                raise ValueError('Selection expired. Try again.')
            if (daemon._shutdown.is_set() or updating() or daemon.recorder.recording
                    or daemon.busy or daemon.preparation.running or daemon._model_maintenance):
                return {'ok': False, 'busy': True, 'error': 'Finish the current operation before selecting recognition settings.'}
            cfg = load_config(strict=True)
            candidate = dict(cfg, **changes)
            if not languages.is_valid(candidate.get('language', 'auto')):
                raise ValueError('Unknown recognition language')
            selected = next((item for item in prepared_choices(candidate)
                             if item['engine'] == candidate['engine'] and item['model'] == candidate['model']), None)
            if selected is None:
                raise ValueError('Prepare this model explicitly in Settings first.')
            forced = selected.get('forced_language')
            if forced and candidate.get('language') != forced:
                raise ValueError(f'This model requires language: {forced}')
            if time.monotonic() >= expires:
                raise ValueError('Selection expired. Try again.')
            dependencies = ('whisper_cpp_binary', 'whisper_cpp_model', 'parakeet_binary', 'parakeet_model', 'engine_worker', 'device')
            guards = {key: cfg.get(key) for key in dependencies}
            guards.update({key: expected[key] for key in FIELDS if key not in changes})
            cfg = update_config(changes, {key: expected[key] for key in changes}, guards=guards, deadline=expires)
            phase = daemon.preparation.start(cfg, download=False)
            report = daemon.preparation.snapshot()
            if (not phase and cfg.get('engine') == 'faster-whisper' and cfg.get('engine_worker', True)
                    and report.get('state') == 'error'):
                return {'ok': False, 'saved': True, 'cfg': cfg, 'error': report.get('error'), 'state': 'error'}
            return {'ok': True, 'cfg': cfg, 'state': phase or 'ready'}
    except (ValueError, OSError, TimeoutError, TypeError, OverflowError) as exc:
        return {'ok': False, 'error': str(exc)}
