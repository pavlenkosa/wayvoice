"""Opt-in manual acceptance harness. Never imported by the installed application.

Path and operation guards are installed before daemon/UI imports. This is an
accidental-side-effect boundary for trusted checkout code, not a security sandbox.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'app/src'))
MARKER = 'session.json'
_CONFIGURED_ROOT = None
COMMANDS = ('ping', 'status', 'start', 'stop', 'cancel', 'ptt-start', 'ptt-stop',
            'clear-text', 'clear-error')
SCENARIOS = {
    'short': 'Text available; next recording starts',
    'long': 'Complete recognition; next recording starts',
    'silence': 'No invented text; returns to idle',
    'cancel-recording': 'Microphone released; next recording starts',
    'cancel-recognition': 'Cancelled work not inserted; next recording starts',
    'repeat': 'Repeated start/stop does not strand state',
    'ptt': 'Addressed press/release owns one recording',
    'delivery': 'Text inserted or clipboard fallback explained',
    'microphone': 'Selected input used without silent fallback',
    'hot-unplug': 'Missing selected input reported; state recovered',
    'config-merge': 'Different fields preserved across two windows',
    'config-conflict': 'Same-field conflict retains draft and disk',
    'config-damaged': 'Raw damaged config preserved; Save refused',
    'series': 'Twenty completed takes; no stranded state or retained WAV',
    'shutdown': 'Only session processes stopped; no retained WAV',
}


class AcceptanceBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise AcceptanceBlocked('Unavailable in the manual acceptance session')


def _write(path: Path, value) -> None:
    fd, name = tempfile.mkstemp(prefix='.acceptance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_session(root: Path) -> dict:
    root = Path(root).absolute()
    info = root.lstat()
    if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AcceptanceBlocked('Session directory must be private and owned by you')
    data = json.loads((root / MARKER).read_text())
    if data.get('schema') != 1 or data.get('root') != str(root) or not isinstance(data.get('id'), str):
        raise AcceptanceBlocked('Not an acceptance session')
    if data.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise AcceptanceBlocked('Session belongs to another boot')
    return data


def create_session(root: Path) -> dict:
    from wayvoice import config, model_store
    from wayvoice.runtime import locations
    cfg = config.load_config(strict=True)
    runtime, cache = locations.faster_runtime().absolute(), model_store.hub_root().absolute()
    root = Path(root).absolute()
    root.mkdir(mode=0o700)  # Refuse any existing directory; never adopt user data.
    for name in ('config/wayvoice', 'data', 'state', 'cache', 'tmp', 'run', 'logs', 'owners'):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        commit = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(REPO), 'status', '--porcelain'],
                                   capture_output=True, text=True, check=True).stdout)
    except (OSError, subprocess.SubprocessError):
        commit, dirty = 'unknown', True
    # Copy only; never save through the user's config API during initialization.
    cfg.update(auto_check_updates=False, notify=False, notify_transcript=False,
               shortcut_backend='manual', onboarding_completed=True,
               onboarding_deferred=False, paste_mode='copy')
    _write(root / 'config/wayvoice/config.json', cfg)
    data = {'schema': 1, 'id': uuid.uuid4().hex, 'root': str(root),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'commit': commit, 'dirty': dirty, 'runtime': str(runtime), 'hub': str(cache),
            'desktop': os.environ.get('XDG_CURRENT_DESKTOP', 'unknown'),
            'session_type': os.environ.get('XDG_SESSION_TYPE', 'unknown')}
    _write(root / MARKER, data)
    _write(root / 'phase.json', {'phase': 'functional'})
    return data


def configure(root: Path) -> dict:
    """Install explicit guards in this process before importing UI/daemon."""
    global _CONFIGURED_ROOT
    data = load_session(root)
    root = Path(data['root'])
    if _CONFIGURED_ROOT is not None:
        if _CONFIGURED_ROOT != root:
            raise AcceptanceBlocked('Use a new process for each acceptance session')
        return data
    os.umask(0o077)
    for variable, directory in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                                ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache'),
                                ('TMPDIR', 'tmp')):
        os.environ[variable] = str(root / directory)
    # Preserve actual Wayland/PipeWire runtime directory and session bus.
    tempfile.tempdir = str(root / 'tmp')
    os.environ.update(WAYVOICE_RUNTIME=data['runtime'], HF_HUB_CACHE=data['hub'],
                      HUGGINGFACE_HUB_CACHE=data['hub'], HF_HUB_OFFLINE='1',
                      TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    from wayvoice import protocol, fw_worker, service, shortcut, pkgsys, updater, model_store, injector
    from wayvoice.runtime import locations, model_preparation
    protocol.runtime_dir = lambda: root / 'run'
    fw_worker.default_socket_path = lambda: root / 'run/fw.sock'
    locations.worker_log_path = lambda: Path(os.devnull)
    locations.faster_stamp = lambda: root / 'state/runtime-ready'
    # No service._spawn: a new interpreter without these guards is unsafe.
    service.systemd_available = lambda: False
    service._systemctl = _blocked
    service._spawn = _blocked
    service.start_daemon = lambda wait=5.0: service.daemon_socket_alive()
    service.restart_daemon = _blocked
    service.start_user_unit = lambda unit: False
    service.request_engine_setup = _blocked
    service.apply_shortcut_now = lambda: (False, 'Manual acceptance: desktop bindings unchanged')
    shortcut.apply_shortcut = lambda *a, **k: (False, 'Manual acceptance: desktop bindings unchanged')
    shortcut.manual_shortcut_required = lambda: True
    shortcut.portal_shortcut_desktop = lambda: False
    pkgsys.install_packages = _blocked
    pkgsys._run_manager = _blocked
    updater.check = _blocked
    updater._read = _blocked
    updater.install = _blocked
    updater.gui_install = _blocked
    updater.restart = _blocked
    model_store.delete = _blocked
    model_preparation.download_model = _blocked
    model_preparation.download_configured_model = _blocked
    from wayvoice.runtime import worker_client
    stop_worker = worker_client.stop_worker
    def owned_stop_worker(timeout=2.0, expected_identity=None, cleanup=True):
        if expected_identity is None:
            try:
                pid = int(locations.worker_pid_path().read_text().split()[0])
            except (OSError, ValueError, IndexError):
                return False
        else:
            pid = expected_identity[0]
        owner = next((owner for owner in live_owners(root)
                      if owner['role'] == 'worker' and owner['pid'] == pid), None)
        if owner is None:
            return False
        identity = worker_client._worker_identity(pid)
        if identity is None or identity[1] != owner['start']:
            return False
        if expected_identity is not None and expected_identity != identity:
            return False
        return stop_worker(timeout=timeout, expected_identity=identity, cleanup=cleanup)
    worker_client.stop_worker = owned_stop_worker
    from wayvoice.shortcut_portal import ShortcutPortal
    ShortcutPortal.start = _blocked
    ShortcutPortal.configure = _blocked
    # Existing helper may be used deliberately; never start one or send a key
    # when it is unavailable. Copy mode is the initial, explicit default.
    injector.ensure_helper_running = lambda wait=2.0: injector.helper_answering()
    paste = injector.paste_with_ydotool
    def guarded_paste(*args, **kwargs):
        if not injector.helper_answering():
            return False, 'Acceptance: automatic paste unavailable; text is in the clipboard.'
        return paste(*args, **kwargs)
    injector.paste_with_ydotool = guarded_paste
    _CONFIGURED_ROOT = root
    return data


def preflight(root: Path, cfg: dict | None = None) -> dict:
    data = load_session(root)
    from wayvoice import config, model_store
    cfg = config.load_config(strict=True) if cfg is None else cfg
    blocked = []
    runtime = Path(data['runtime'])
    if cfg.get('engine') != 'faster-whisper':
        blocked.append('ONLY_PREPARED_FASTER_WHISPER_SUPPORTED')
    if not (runtime / 'bin/python').is_file() or not list(runtime.glob('.engine-*-ready')):
        blocked.append('RUNTIME_NOT_PREPARED')
    try:
        model_store.inference_dir(str(cfg.get('model', '')))
    except (OSError, RuntimeError, ValueError):
        blocked.append('MODEL_NOT_PREPARED')
    for command in ('pw-record', 'wl-copy'):
        if not shutil.which(command):
            blocked.append('MISSING_' + command.upper().replace('-', '_'))
    if data['session_type'] != 'wayland':
        blocked.append('NOT_WAYLAND')
    return {'ok': not blocked, 'blocked': blocked, 'commit': data['commit'], 'dirty': data['dirty']}


def public_status(reply: dict) -> dict:
    """Whitelist metadata; status.last_text and arbitrary error messages stay private."""
    result = {key: reply[key] for key in ('ok', 'recording', 'busy') if isinstance(reply.get(key), bool)}
    states = {'idle', 'recording', 'transcribing', 'cancelling', 'queued', 'warming',
              'downloading', 'ready', 'error', 'cancelled', 'cold', 'nothing', 'busy', 'accepted'}
    if isinstance(reply.get('state'), str) and reply['state'] in states:
        result['state'] = reply['state']
    result['last_error_present'] = bool(reply.get('last_error') or reply.get('error'))
    result['last_warning_present'] = bool(reply.get('last_warning'))
    result['config_error_present'] = bool(reply.get('config_error'))
    model = reply.get('model')
    if isinstance(model, dict):
        download = model.get('download', {})
        if not isinstance(download, dict):
            download = {}
        result['preparation'] = {key: download.get(key) for key in ('state', 'warming')
                                 if (key == 'state' and isinstance(download.get(key), str) and download[key] in states)
                                 or (key == 'warming' and isinstance(download.get(key), bool))}
    return result


def _identity(pid: int) -> str | None:
    try:
        raw = Path(f'/proc/{pid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return fields[19]  # starttime, field 22
    except (OSError, IndexError):
        return None


def register_owner(root: Path, pid: int, role: str) -> None:
    identity = _identity(pid)
    if identity is None:
        return
    data = load_session(root)
    _write(root / 'owners' / f'{pid}.json',
           {'pid': pid, 'start': identity, 'role': role, 'session': data['id']})


def live_owners(root: Path) -> list[dict]:
    data = load_session(root)
    owners = []
    for path in (root / 'owners').glob('*.json'):
        owner = json.loads(path.read_text())
        if owner.get('session') == data['id'] and _identity(owner['pid']) == owner.get('start'):
            owners.append(owner)
    return owners


def memory_sample(root: Path) -> dict:
    result = {}
    for owner in live_owners(root):
        try:
            values = Path(f"/proc/{owner['pid']}/status").read_text().splitlines()
            rss = next(int(line.split()[1]) for line in values if line.startswith('VmRSS:'))
            result[f"{owner['role']}_{owner['pid']}"] = rss
        except (OSError, ValueError, StopIteration):
            pass
    return result


def append_row(root: Path, filename: str, row: dict) -> None:
    # A single write per bounded numeric row; no speech, audio, errors or free text.
    raw = (json.dumps(row, allow_nan=False) + '\n').encode()
    if len(raw) > 4096:
        raise AcceptanceBlocked('Result row is too large')
    fd = os.open(root / filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


def read_metrics(root: Path) -> list:
    path = root / 'metrics.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _summary(rows):
    values = sorted(float(row['elapsed_ms']) for row in rows
                    if row.get('ok') is True and isinstance(row.get('elapsed_ms'), (int, float))
                    and not isinstance(row['elapsed_ms'], bool)
                    and math.isfinite(row['elapsed_ms']) and row['elapsed_ms'] >= 0)
    outcomes = {}
    for row in rows:
        outcome = row.get('outcome') or ('completed' if row.get('ok') is True else 'error')
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {'attempted': len(rows), 'ok': sum(row.get('ok') is True for row in rows),
            'outcomes': outcomes, 'n': len(values),
            'median_ms': statistics.median(values) if values else None,
            'p95_ms': values[math.ceil(.95 * len(values)) - 1] if values else None,
            'max_ms': max(values) if values else None}


def summarize(rows: list) -> dict:
    """Never pool latencies across different model/settings conditions."""
    result = {}
    fields = ('settings_sha256', 'engine', 'model', 'device', 'language')
    for phase in ('cold', 'warm', 'functional'):
        selected = [row for row in rows if row.get('phase') == phase]
        grouped = {}
        for row in selected:
            key = tuple(row.get(field) for field in fields)
            grouped.setdefault(key, []).append(row)
        summary = _summary(selected)
        summary['groups'] = [dict(zip(fields, key), **_summary(group)) for key, group in grouped.items()]
        summary['mixed_configs'] = len(grouped) > 1
        if summary['mixed_configs']:
            # Counts remain useful; only each group's timing distribution is valid.
            for field in ('median_ms', 'p95_ms', 'max_ms'):
                summary[field] = None
        result[phase] = summary
    return result


def result_matrix(manual: list) -> list:
    matrix = []
    for scenario, expected in SCENARIOS.items():
        applications = ('editor', 'browser', 'terminal') if scenario == 'delivery' else ('none',)
        for application in applications:
            found = [(index, row) for index, row in enumerate(manual, 1)
                     if row.get('scenario') == scenario and row.get('application') == application]
            index, row = found[-1] if found else (None, {})
            matrix.append({'scenario': scenario, 'application': application, 'expected': expected,
                           'actual': row.get('result', 'NOT_TESTED'), 'paste': row.get('paste', 'na'),
                           'duration_ms': row.get('duration_ms'),
                           'diagnostics': f'manual.jsonl#{index}' if index else None,
                           'metric_take_id': row.get('metric_take_id')})
    return matrix


class Observer:
    """Harness-only timing around real pipeline functions; never retain their text."""
    def __init__(self, root: Path):
        self.root = root
        self.local = threading.local()
        self.pending = None
        self.counter = 0
        self.active = None

    def stop(self, daemon, original, *args, **kwargs):
        started = time.monotonic_ns()
        with daemon._lock:
            if daemon.recorder.recording and (kwargs.get('expected_generation') is None or
                    kwargs['expected_generation'] == daemon._record_generation):
                self.counter += 1
                try:
                    from wayvoice.config import load_config
                    # Telemetry must never prevent a real stop after damaged config.
                    try:
                        cfg = load_config(strict=True)
                    except (OSError, ValueError):
                        cfg = {}
                    fingerprint = settings_fingerprint(cfg)
                    phase = json.loads((self.root / 'phase.json').read_text())
                    label = phase['phase']
                    if label == 'cold':
                        _write(self.root / 'phase.json', {'phase': 'functional'})
                    if phase.get('settings_sha256') != fingerprint:
                        label = 'functional'
                    self.pending = {'take': self.counter, 'take_id': uuid.uuid4().hex,
                                    'started': started, 'phase': label, 'record_generation': daemon._record_generation,
                                    'engine': cfg.get('engine'), 'model': cfg.get('model'),
                                    'device': cfg.get('device'), 'language': cfg.get('language'),
                                    'settings_sha256': fingerprint, 'rss_before_kib': memory_sample(self.root)}
                except Exception:
                    self.pending = {'take': self.counter, 'take_id': uuid.uuid4().hex,
                                    'started': started, 'phase': 'functional',
                                    'record_generation': daemon._record_generation,
                                    'observer_error': True}
            # This is an RLock: preserve stop admission and pending-row identity
            # across concurrent manual/timer calls without changing the pipeline.
            return original(*args, **kwargs)

    def cancel(self, daemon, original):
        with daemon._lock:
            if daemon.busy:
                row = next((candidate for candidate in (self.active, self.pending)
                            if candidate is not None and candidate.get('record_generation') == daemon._record_generation), None)
                if row is not None:
                    row['cancelled'] = True
            return original()

    def recognize(self, original, *args, **kwargs):
        row = getattr(self.local, 'row', None)
        cfg = args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs.get('cfg')
        if row is not None and isinstance(cfg, dict):
            fingerprint = settings_fingerprint(cfg)
            if row.get('settings_sha256') != fingerprint:
                row['phase'] = 'functional'
            row.update(settings_sha256=fingerprint, engine=cfg.get('engine'),
                       model=cfg.get('model'), device=cfg.get('device'), language=cfg.get('language'))
        try:
            text = original(*args, **kwargs)
            row = getattr(self.local, 'row', None)
            if row is not None:
                row['text_ready_ms'] = (time.monotonic_ns() - row['started']) / 1e6
                row['has_text'] = bool(text.strip())
            return text
        except Exception as exc:
            row = getattr(self.local, 'row', None)
            if row is not None:
                row['cancelled'] = row.get('cancelled', False) or type(exc).__name__ in {'TranscriptionCancelled', 'InjectionCancelled'}
            raise

    def deliver(self, original, *args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            row = getattr(self.local, 'row', None)
            if row is not None and type(exc).__name__ in {'InjectionCancelled', 'TranscriptionCancelled'}:
                row['cancelled'] = True
            raise
        row = getattr(self.local, 'row', None)
        if row is not None:
            row['delivery_ready_ms'] = (time.monotonic_ns() - row['started']) / 1e6
            row['pasted'] = bool(result.pasted)
            row['warning'] = bool(result.warning)
        return result

    def worker(self, daemon, original, *args, **kwargs):
        with daemon._lock:
            row = dict(self.pending or {'take': 0, 'started': time.monotonic_ns(), 'phase': 'functional'})
            self.local.row = row
            self.active = row
        try:
            return original(*args, **kwargs)
        finally:
            delivered = row.get('delivery_ready_ms')
            with daemon._lock:
                same_take = row.get('record_generation') == daemon._record_generation
                cancelled = row.get('cancelled', False) or (same_take and daemon._transcribe_cancel.is_set())
            shutdown = getattr(daemon, '_shutdown', None)
            if delivered is None and shutdown is not None and shutdown.is_set():
                cancelled = True
            outcome = ('cancelled' if cancelled else 'completed' if delivered is not None
                       else 'no_speech' if row.get('has_text') is False else 'error')
            row.update(ok=outcome == 'completed', outcome=outcome, elapsed_ms=delivered,
                       text_ready_ms=row.get('text_ready_ms'), delivery_ready_ms=delivered,
                       pasted=row.get('pasted'), rss_after_kib=memory_sample(self.root))
            row.pop('started', None)
            row.pop('has_text', None)
            self.local.row = None
            with daemon._lock:
                if self.active is row:
                    self.active = None
            append_row(self.root, 'metrics.jsonl', row)


def settings_fingerprint(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def cold_worker(root: Path) -> bool:
    from wayvoice.runtime import worker_client, locations
    worker_client.stop_worker()
    if any(owner['role'] in {'worker', 'job'} for owner in live_owners(root)):
        return False
    try:
        pid = int(locations.worker_pid_path().read_text().strip())
    except (OSError, ValueError):
        pid = 0
    # An absent worker is already cold; a verified survivor must block the run.
    if pid > 0 and worker_client._worker_identity(pid) is not None:
        return False
    return worker_client._worker_ping() is None


def run_daemon(root: Path) -> None:
    from wayvoice import daemon as module, config, injector
    from wayvoice.runtime import worker_client, processes
    observer = Observer(root)
    recognize, inject = module.transcribe, module.inject
    def transcribe(audio, cfg, event):
        check = preflight(root, cfg)
        if not check['ok']:
            raise AcceptanceBlocked('Resources not prepared')
        return observer.recognize(recognize, audio, cfg, event)
    module.transcribe = transcribe
    module.inject = lambda *a, **k: observer.deliver(inject, *a, **k)
    module.notify = lambda *a, **k: None
    # Prevent startup preparation/downloads and portal registration, not the
    # actual recording/transcription/delivery implementation being accepted.
    daemon = module.WayVoiceDaemon()
    daemon.prepare_on_start = lambda: None
    daemon._restore_portal_shortcut = lambda: None
    stop, worker, dispatch = daemon.stop_recording, daemon._transcribe_worker, daemon.dispatch
    cancel = daemon.cancel
    daemon.cancel = lambda: observer.cancel(daemon, cancel)
    start = daemon.start_recording
    def guarded_start(*args, **kwargs):
        try:
            ready = preflight(root)['ok']
        except (OSError, ValueError):
            ready = False
        if not ready or daemon.preparation.running:
            return {'ok': False, 'error': 'Acceptance resources not ready'}
        phase = json.loads((root / 'phase.json').read_text())
        if phase['phase'] == 'warm':
            reply = worker_client._worker_ping()
            cfg = config.load_config(strict=True)
            if not reply or not reply.get('warm') or not worker_client._worker_settings_match(reply, cfg):
                _write(root / 'phase.json', {'phase': 'functional'})
        return start(*args, **kwargs)
    daemon.start_recording = guarded_start
    daemon.stop_recording = lambda *a, **k: observer.stop(daemon, stop, *a, **k)
    daemon._transcribe_worker = lambda *a, **k: observer.worker(daemon, worker, *a, **k)
    def guarded_dispatch(command):
        if command in {'start', 'ptt-start', 'toggle'} and not preflight(root)['ok']:
            return {'ok': False, 'error': 'Acceptance resources not prepared'}
        if command == 'acceptance-cold':
            with daemon._lock:
                if daemon.busy or daemon.recorder.recording or daemon.preparation.running:
                    return {'ok': False, 'error': 'busy'}
                if not cold_worker(root):
                    return {'ok': False, 'error': 'worker still running'}
                _write(root / 'phase.json', {'phase': 'cold',
                       'settings_sha256': settings_fingerprint(config.load_config(strict=True))})
                return {'ok': True, 'state': 'cold'}
        if command == 'acceptance-warm':
            if not preflight(root)['ok']:
                return {'ok': False, 'error': 'Acceptance resources not prepared'}
            with daemon._lock:
                if daemon.busy or daemon.recorder.recording or daemon.preparation.running:
                    return {'ok': False, 'error': 'busy'}
                cfg = config.load_config(strict=True)
                if not cfg.get('engine_worker', True):
                    return {'ok': False, 'error': 'worker disabled'}
                phase = daemon.preparation.start(cfg, download=False)
                if phase:
                    _write(root / 'phase.json', {'phase': 'warm',
                           'settings_sha256': settings_fingerprint(cfg)})
                return {'ok': bool(phase), 'state': phase}
        name = command.strip().split(' ', 1)[0].lower()
        if name in {'prepare-model', 'delete-model', 'configure-shortcut', 'engine-setup'} or name.startswith('update-'):
            return {'ok': False, 'error': 'Unavailable in acceptance'}
        return dispatch(command)
    daemon.dispatch = guarded_dispatch
    # Track real owned children. Their output is never retained by this harness.
    start_worker = worker_client._start_worker
    def tracked_worker(cfg):
        result = start_worker(cfg)
        for proc in worker_client._worker_procs:
            register_owner(root, proc.pid, 'worker')
        if observer.active is not None and observer.active['phase'] == 'warm':
            observer.active['phase'] = 'functional'
        return result
    worker_client._start_worker = tracked_worker
    spawn = processes._spawn_job
    def tracked_job(*args, **kwargs):
        proc = spawn(*args, **kwargs)
        register_owner(root, proc.pid, 'job')
        return proc
    processes._spawn_job = tracked_job
    recorder_start = daemon.recorder.start
    def tracked_recording(*args, **kwargs):
        result = recorder_start(*args, **kwargs)
        if daemon.recorder._proc is not None:
            register_owner(root, daemon.recorder._proc.pid, 'recorder')
        return result
    daemon.recorder.start = tracked_recording
    register_owner(root, os.getpid(), 'daemon')
    module._install_signal_handlers(daemon)
    try:
        daemon.serve()
    finally:
        worker_client.stop_worker()


def run_ui(root: Path, window: str) -> None:
    from wayvoice.ui import application
    from wayvoice import cli
    # Block CLI exec-to-installed-UI and restart-to-installed-launcher paths.
    cli.main = _blocked
    application.restart_installed_ui = _blocked
    from wayvoice.ui.dialogs.update import UpdateDialog
    UpdateDialog._transaction = _blocked
    from wayvoice.ui.controllers.status import StatusController
    StatusController._journal_tail = lambda self: 'Acceptance: use the numeric harness report.'
    app = application.App()
    suffix = hashlib.sha256((str(root) + window).encode()).hexdigest()[:20]
    app.set_application_id('io.github.stepan.WayVoice.Acceptance.s' + suffix)
    register_owner(root, os.getpid(), 'ui')
    app.run(['wayvoice-acceptance'])


def stop_session(root: Path, purge: bool = False) -> dict:
    from wayvoice.cli import request
    request('quit', timeout=1)
    # Pin identities before signalling. Refuse PID reuse and unsupported pidfds.
    deadline = time.monotonic() + 6
    while live_owners(root) and time.monotonic() < deadline:
        time.sleep(.1)
        # UI doesn't receive daemon quit; close its owned process explicitly.
        if all(owner['role'] == 'ui' for owner in live_owners(root)):
            break
    for owner in live_owners(root):
        try:
            fd = os.pidfd_open(owner['pid'])
        except (OSError, AttributeError):
            continue
        try:
            if _identity(owner['pid']) != owner['start']:
                continue
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if select.select([fd], [], [], 0)[0]:
                    break
                signal.pidfd_send_signal(fd, sig)
                if select.select([fd], [], [], 2)[0]:
                    break
        finally:
            os.close(fd)
    alive = live_owners(root)
    wavs = list((root / 'tmp').rglob('*.wav'))
    if not alive:
        for path in wavs:
            path.unlink()
    result = {'ok': not alive, 'remaining_processes': len(alive), 'remaining_wavs': len(wavs),
              'audio_removed': not alive}
    result['remaining_wavs'] = len(list((root / 'tmp').rglob('*.wav')))
    result['removed_wavs'] = len(wavs) - result['remaining_wavs']
    if purge and not alive:
        shutil.rmtree(root)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init')
    init.add_argument('--session', type=Path, dest='init_session')
    for action in ('preflight', 'daemon'):
        sub.add_parser(action)
    ui = sub.add_parser('ui')
    ui.add_argument('--window', default='one', choices=('one', 'two'))
    command = sub.add_parser('command')
    command.add_argument('command', choices=COMMANDS)
    worker = sub.add_parser('worker')
    worker.add_argument('phase', choices=('cold', 'warm'))
    report = sub.add_parser('report')
    report.add_argument('--output', type=Path)
    record = sub.add_parser('record')
    record.add_argument('--scenario', required=True, choices=('short', 'long', 'silence', 'cancel-recording',
        'cancel-recognition', 'repeat', 'ptt', 'delivery', 'microphone', 'hot-unplug',
        'config-merge', 'config-conflict', 'config-damaged', 'series', 'shutdown'))
    record.add_argument('--result', required=True, choices=('PASS', 'FAIL', 'BLOCKED', 'NOT_TESTED'))
    record.add_argument('--application', default='none', choices=('editor', 'browser', 'terminal', 'none'))
    record.add_argument('--paste', default='na', choices=('yes', 'no', 'na'))
    record.add_argument('--duration-ms', type=float)
    record.add_argument('--take-id')
    stop = sub.add_parser('stop-session')
    stop.add_argument('--purge', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.action == 'init':
        root = args.init_session or args.session
        if root is None:
            root = Path(tempfile.gettempdir()) / ('wayvoice-acceptance-' + uuid.uuid4().hex[:12])
        data = create_session(root)
        print(json.dumps({'session': data['root'], 'commit': data['commit'], 'dirty': data['dirty']}))
        return 0
    if args.session is None:
        parser.error('--session is required')
    root = args.session.absolute()
    data = configure(root)
    if args.action in {'daemon', 'ui'}:
        check = preflight(root)
        if not check['ok']:
            print(json.dumps(check))
            return 2
        # Native libraries and exception messages may contain user data. Do not
        # retain stdout/stderr; diagnostics are numeric acceptance rows only.
        with open(os.devnull, 'w') as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        if args.action == 'daemon':
            run_daemon(root)
        else:
            run_ui(root, args.window)
        return 0
    if args.action == 'preflight':
        result = preflight(root)
    elif args.action in {'command', 'worker'}:
        from wayvoice.cli import request
        command = args.command if args.action == 'command' else 'acceptance-' + args.phase
        if command == 'clear-error':
            command = 'clear-status'
        result = public_status(request(command, timeout=5))
    elif args.action == 'record':
        row = {'scenario': args.scenario, 'result': args.result,
               'application': args.application, 'paste': args.paste}
        if args.duration_ms is not None:
            if not math.isfinite(args.duration_ms) or args.duration_ms < 0:
                raise AcceptanceBlocked('Duration must be finite and nonnegative')
            row['duration_ms'] = args.duration_ms
        if args.take_id is not None:
            metric = next((metric for metric in read_metrics(root) if metric.get('take_id') == args.take_id), None)
            if metric is None:
                raise AcceptanceBlocked('Unknown metric take ID')
            row.update(metric_take_id=args.take_id, duration_ms=metric.get('elapsed_ms'))
        append_row(root, 'manual.jsonl', row)
        result = {'ok': True}
    elif args.action == 'stop-session':
        result = stop_session(root, args.purge)
    else:
        rows = read_metrics(root)
        manual = root / 'manual.jsonl'
        result = {'ok': True, 'session': data, 'metrics': rows, 'summary': summarize(rows),
                  'manual': [json.loads(line) for line in manual.read_text().splitlines()] if manual.exists() else [],
                  'live_owners': len(live_owners(root)), 'remaining_wavs': len(list((root / 'tmp').rglob('*.wav')))}
        result['matrix'] = result_matrix(result['manual'])
        result['acceptance'] = 'OPEN'  # Human evidence/review required; never self-certify.
        if args.output:
            _write(args.output.absolute(), result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get('ok', True) else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AcceptanceBlocked, OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'blocked': type(exc).__name__}))
        raise SystemExit(2)
