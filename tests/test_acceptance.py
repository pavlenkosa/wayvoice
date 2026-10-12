"""Acceptance tooling stays private and never exercises real desktop services."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import isolate_environment


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'acceptance.py'
LAUNCHER = SCRIPT.with_name('wayvoice-acceptance')
spec = importlib.util.spec_from_file_location('wayvoice_acceptance_test_module', SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acceptance
spec.loader.exec_module(acceptance)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_environment(self)
        self.session = self.root / 'acceptance'
        runtime = self.root / 'prepared-runtime'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'bin' / 'python').touch()
        (runtime / '.engine-v1-ready').touch()
        patcher = mock.patch.dict(os.environ, {'WAYVOICE_RUNTIME': str(runtime)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_round_trip_is_private(self):
        created = acceptance.create_session(self.session)
        loaded = acceptance.load_session(self.session)
        self.assertIsInstance(created, dict)
        self.assertEqual(loaded, created)
        self.assertEqual(loaded['schema'], 1)
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o700)
        for file in self.session.rglob('*'):
            if file.is_file():
                self.assertEqual(file.stat().st_mode & 0o077, 0, str(file))

    def test_missing_session_is_refused_without_creation(self):
        with self.assertRaises((ValueError, OSError)):
            acceptance.load_session(self.session)
        self.assertFalse(self.session.exists())

    def test_session_validation_rejects_symlink_and_public_directory(self):
        acceptance.create_session(self.session)
        alias = self.root / 'acceptance-link'
        alias.symlink_to(self.session, target_is_directory=True)
        with self.assertRaises((ValueError, OSError, acceptance.AcceptanceBlocked)):
            acceptance.load_session(alias)
        self.session.chmod(0o755)
        with self.assertRaises((ValueError, OSError, acceptance.AcceptanceBlocked)):
            acceptance.load_session(self.session)

    def test_public_status_does_not_disclose_recognized_text_or_errors(self):
        original = {'ok': True, 'recording': False, 'busy': False,
                    'last_text': 'PRIVATE DICTATION',
                    'last_error': 'PRIVATE ERROR /home/user/secret',
                    'last_warning': 'PRIVATE WARNING',
                    'model': {'download': {'error': 'PRIVATE DOWNLOAD'}}}
        redacted = acceptance.public_status(original)
        serialized = json.dumps(redacted)
        self.assertNotIn('PRIVATE', serialized)
        self.assertTrue(redacted['ok'])
        self.assertEqual(original['last_text'], 'PRIVATE DICTATION')

    def test_malformed_status_state_is_redacted_without_crashing(self):
        result = acceptance.public_status({'ok': False, 'state': {'private': 'secret'},
                                           'last_error': {'private': 'secret'}})
        self.assertFalse(result['ok'])
        self.assertNotIn('secret', json.dumps(result))

    def test_empty_metrics_have_no_successful_timings(self):
        acceptance.create_session(self.session)
        self.assertEqual(acceptance.read_metrics(self.session), [])
        summary = acceptance.summarize([])
        self.assertIsInstance(summary, dict)
        for phase in ('cold', 'warm'):
            if phase in summary:
                self.assertEqual(summary[phase]['n'], 0)

    def test_summary_excludes_failures_and_invalid_timings(self):
        rows = [
            {'phase': 'cold', 'ok': True, 'elapsed_ms': value}
            for value in (10, 20, 40)
        ] + [
            {'phase': 'warm', 'ok': True, 'elapsed_ms': 5},
            {'phase': 'cold', 'ok': False, 'elapsed_ms': 999},
            {'phase': 'cold', 'ok': True, 'elapsed_ms': float('nan')},
            {'phase': 'cold', 'ok': True, 'elapsed_ms': float('inf')},
            {'phase': 'cold', 'ok': True, 'elapsed_ms': -1},
            {'phase': 'cold', 'ok': True, 'elapsed_ms': None},
        ]
        summary = acceptance.summarize(rows)
        self.assertEqual(summary['cold'], {'n': 3, 'median_ms': 20,
                                          'p95_ms': 40, 'max_ms': 40})
        self.assertEqual(summary['warm'], {'n': 1, 'median_ms': 5,
                                          'p95_ms': 5, 'max_ms': 5})

    def test_configuration_guards_are_process_local_and_preserve_desktop_runtime(self):
        acceptance.create_session(self.session)
        desktop_runtime = os.environ['XDG_RUNTIME_DIR']
        code = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import acceptance
acceptance.configure(Path(sys.argv[2]))
from wayvoice import protocol, service, fw_worker, pkgsys, updater, model_store
from wayvoice.runtime import model_preparation
assert service.systemd_available() is False
assert os.environ['XDG_RUNTIME_DIR'] == sys.argv[3]
assert Path(os.environ['XDG_CONFIG_HOME']).is_relative_to(Path(sys.argv[2]))
assert Path(os.environ['XDG_STATE_HOME']).is_relative_to(Path(sys.argv[2]))
assert Path(os.environ['XDG_DATA_HOME']).is_relative_to(Path(sys.argv[2]))
assert Path(os.environ['TMPDIR']).is_relative_to(Path(sys.argv[2]))
assert protocol.runtime_dir().is_relative_to(Path(sys.argv[2]))
assert fw_worker.default_socket_path().is_relative_to(Path(sys.argv[2]))
for call in (lambda: pkgsys.install_packages([]), lambda: updater.install(),
             lambda: model_store.delete('small'),
             lambda: model_preparation.download_model('small'),
             lambda: service.request_engine_setup()):
    try:
        call()
    except acceptance.AcceptanceBlocked:
        pass
    else:
        raise AssertionError('A side effect was not blocked')
print(json.dumps({'ok': True}))
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent),
                                 str(self.session), desktop_runtime],
                                capture_output=True, text=True, timeout=15,
                                env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': True})
        self.assertEqual(os.environ['XDG_RUNTIME_DIR'], desktop_runtime)

    def test_worker_stop_requires_private_registered_matching_owner(self):
        acceptance.create_session(self.session)
        code = r"""
import json, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import acceptance
from wayvoice.runtime import worker_client, locations
root = Path(sys.argv[2])
original = mock.Mock(return_value=True)
worker_client.stop_worker = original
acceptance.configure(root)
first_guard = worker_client.stop_worker
acceptance.configure(root)
assert worker_client.stop_worker is first_guard
data = acceptance.load_session(root)
locations.worker_pid_path().parent.mkdir(parents=True, exist_ok=True)
locations.worker_pid_path().write_text('123')
with mock.patch.object(worker_client, '_worker_identity', return_value=(123, 'workerstart')), \
     mock.patch.object(acceptance, '_identity', return_value='workerstart'):
    assert worker_client.stop_worker() is False
    original.assert_not_called()
    owner = {'pid':123, 'start':'workerstart', 'role':'worker', 'session':'unrelated'}
    path = root / 'owners' / '123.json'
    path.write_text(json.dumps(owner))
    assert worker_client.stop_worker() is False
    original.assert_not_called()
    owner['session'] = data['id']
    owner['start'] = 'oldidentity'
    path.write_text(json.dumps(owner))
    assert worker_client.stop_worker() is False
    original.assert_not_called()
    owner['start'] = 'workerstart'
    path.write_text(json.dumps(owner))
    assert worker_client.stop_worker(timeout=.25, expected_identity=(123, 'workerstart'), cleanup=False) is True
original.assert_called_once_with(timeout=.25, expected_identity=(123, 'workerstart'), cleanup=False)
print(json.dumps({'ok':True}))
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent), str(self.session)],
                                capture_output=True, text=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_two_acceptance_ui_ids_are_distinct_without_registering_or_opening_windows(self):
        acceptance.create_session(self.session)
        code = r"""
import json, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
except (ImportError, ValueError):
    raise SystemExit(77)
import acceptance
root = Path(sys.argv[2])
acceptance.configure(root)
from wayvoice.ui import application
ids = []
def run(app, args):
    ids.append(app.get_application_id())
    assert not app.get_is_registered()
    assert not app.get_windows()
    return 0
with mock.patch.object(application.App, 'run', run), \
     mock.patch.object(acceptance, 'register_owner'):
    acceptance.run_ui(root, 'one')
    acceptance.run_ui(root, 'two')
assert len(set(ids)) == 2
assert all(value != 'io.github.stepan.WayVoice' for value in ids)
assert all(value.startswith('io.github.stepan.WayVoice.Acceptance.') for value in ids)
print(json.dumps({'ok':True}))
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent), str(self.session)],
                                capture_output=True, text=True, timeout=15, env=dict(os.environ))
        if result.returncode == 77:
            self.skipTest('GTK UI package unavailable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_result_matrix_defaults_to_not_tested_with_explicit_delivery_applications(self):
        rows = acceptance.result_matrix([])
        self.assertTrue(rows)
        self.assertTrue(all(row['actual'] == 'NOT_TESTED' for row in rows))
        self.assertTrue(all(row['expected'] for row in rows))
        self.assertTrue(all(row['diagnostics'] is None for row in rows))
        delivery = [row for row in rows if row['scenario'] == 'delivery']
        self.assertEqual({row['application'] for row in delivery}, {'editor', 'browser', 'terminal'})
        cancellation = next(row for row in rows if row['scenario'] == 'cancel-recording')
        self.assertIn('Microphone released', cancellation['expected'])

    def test_result_matrix_preserves_latest_truthful_per_application_results(self):
        manual = [
            {'scenario': 'delivery', 'application': 'editor', 'result': 'PASS'},
            {'scenario': 'delivery', 'application': 'browser', 'result': 'BLOCKED'},
            {'scenario': 'delivery', 'application': 'terminal', 'result': 'FAIL'},
            {'scenario': 'delivery', 'application': 'editor', 'result': 'FAIL',
             'paste': 'failed', 'duration_ms': 42, 'metric_take_id': 'take123'},
        ]
        delivery = {row['application']: row for row in acceptance.result_matrix(manual)
                    if row['scenario'] == 'delivery'}
        self.assertEqual(delivery['editor']['actual'], 'FAIL')
        self.assertEqual(delivery['browser']['actual'], 'BLOCKED')
        self.assertEqual(delivery['terminal']['actual'], 'FAIL')
        self.assertEqual(delivery['editor']['duration_ms'], 42)
        self.assertEqual(delivery['editor']['diagnostics'], 'manual.jsonl#4')
        self.assertEqual(delivery['editor']['metric_take_id'], 'take123')

    def test_clear_error_cli_alias_sends_clear_status(self):
        acceptance.create_session(self.session)
        code = r"""
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import acceptance
root = Path(sys.argv[2])
acceptance.configure(root)
from wayvoice import cli
with mock.patch.object(cli, 'request', return_value={'ok': True}) as request:
    assert acceptance.main(['--session', str(root), 'command', 'clear-error']) == 0
request.assert_called_once_with('clear-status', timeout=5)
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent), str(self.session)],
                                capture_output=True, text=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def run_cli(self, *args):
        result = subprocess.run(
            [str(LAUNCHER), '--session', str(self.session), *args],
            capture_output=True, text=True, timeout=15, env=dict(os.environ))
        return result

    def test_cli_preflight_and_report_without_daemon_or_desktop(self):
        acceptance.create_session(self.session)
        result = self.run_cli('preflight')
        self.assertIn(result.returncode, (0, 1, 2), result.stderr)
        reply = json.loads(result.stdout)
        self.assertIsInstance(reply['ok'], bool)
        self.assertIsInstance(reply['blocked'], list)
        result = self.run_cli('report')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)

    def test_command_status_routes_only_to_private_socket_and_preserves_source_config(self):
        from wayvoice import config
        config.save_config({'ui_language': 'auto', 'model': 'small'})
        source = config.config_path()
        before = source.read_bytes()
        acceptance.create_session(self.session)
        code = r"""
import json, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import acceptance
root = Path(sys.argv[2])
acceptance.configure(root)
from wayvoice import cli, config, protocol
path = protocol.socket_path()
path.touch()
peer = mock.Mock()
peer.recv.return_value = b'{"ok":true,"last_text":"PRIVATE DICTATION","last_error":"PRIVATE ERROR"}\n'
with mock.patch.object(cli.socket, 'socket', return_value=peer):
    assert acceptance.main(['--session', str(root), 'command', 'status']) == 0
peer.connect.assert_called_once_with(str(root / 'run' / 'wayvoice.sock'))
peer.sendall.assert_called_once_with(b'status\n')
peer.close.assert_called_once()
config.update_config({'ui_language': 'en'}, {'ui_language': 'auto'})
assert config.config_path().is_relative_to(root)
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent), str(self.session)],
                                capture_output=True, text=True, timeout=15,
                                env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertNotIn('PRIVATE', result.stdout)
        self.assertEqual(source.read_bytes(), before)



class AcceptanceObserverTests(unittest.TestCase):
    def setUp(self):
        AcceptanceTests.setUp(self)
        acceptance.create_session(self.session)
        self.observer = acceptance.Observer(self.session)
        self.daemon = SimpleNamespace(_lock=threading.RLock(),
            recorder=SimpleNamespace(recording=True), _record_generation=1,
            _transcribe_cancel=threading.Event())

    def begin(self):
        return self.observer.stop(self.daemon, lambda: {'ok': True})

    def test_benchmark_phase_requires_matching_configuration_fingerprint(self):
        from wayvoice import config
        import hashlib
        cfg = config.load_config(strict=True)
        fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
        for phase in ('cold', 'warm'):
            for marker_hash, expected in ((fingerprint, phase), ('mismatched', 'functional')):
                with self.subTest(phase=phase, marker_hash=marker_hash):
                    (self.session / 'phase.json').write_text(json.dumps(
                        {'phase': phase, 'settings_sha256': marker_hash}))
                    self.daemon.recorder.recording = True
                    stopped = []
                    def stop():
                        stopped.append(True)
                        self.daemon.recorder.recording = False
                        return {'ok': True}
                    with mock.patch.object(acceptance, 'memory_sample', return_value={}):
                        reply = self.observer.stop(self.daemon, stop)
                    self.assertTrue(reply['ok'])
                    self.assertEqual(stopped, [True])
                    self.assertFalse(self.daemon.recorder.recording)
                    self.assertEqual(self.observer.pending['phase'], expected)

    def test_pipeline_timings_follow_stop_recognition_and_delivery_without_text(self):
        def pipeline():
            text = self.observer.recognize(lambda: 'PRIVATE DICTATION')
            return self.observer.deliver(lambda value: SimpleNamespace(pasted=True, warning='PRIVATE WARNING'), text)
        with mock.patch.object(acceptance, 'memory_sample', return_value={'daemon_123': 100}), \
             mock.patch.object(acceptance.time, 'monotonic_ns', side_effect=[0, 10_000_000, 30_000_000]):
            self.begin()
            result = self.observer.worker(self.daemon, pipeline)
        self.assertTrue(result.pasted)
        rows = acceptance.read_metrics(self.session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text_ready_ms'], 10)
        self.assertEqual(rows[0]['delivery_ready_ms'], 30)
        self.assertEqual(rows[0]['elapsed_ms'], 30)
        self.assertEqual(rows[0]['outcome'], 'completed')
        self.assertTrue(rows[0]['ok'])
        self.assertNotIn('PRIVATE', json.dumps(rows))
        self.assertNotIn('PRIVATE', repr(vars(self.observer)))

    def test_pipeline_failure_preserves_no_error_message_or_transcript(self):
        def pipeline():
            self.observer.recognize(lambda: 'PRIVATE DICTATION')
            raise RuntimeError('PRIVATE ERROR')
        with mock.patch.object(acceptance, 'memory_sample', return_value={}), \
             mock.patch.object(acceptance.time, 'monotonic_ns', side_effect=[0, 10_000_000]):
            self.begin()
            with self.assertRaisesRegex(RuntimeError, 'PRIVATE ERROR'):
                self.observer.worker(self.daemon, pipeline)
        rows = acceptance.read_metrics(self.session)
        self.assertEqual(rows[0]['outcome'], 'error')
        self.assertFalse(rows[0]['ok'])
        self.assertIsNone(rows[0]['elapsed_ms'])
        self.assertNotIn('PRIVATE', json.dumps(rows))

    def test_cancellation_between_recognition_and_delivery_is_not_an_error(self):
        def pipeline():
            self.observer.recognize(lambda: 'PRIVATE DICTATION')
            self.daemon._transcribe_cancel.set()
        with mock.patch.object(acceptance, 'memory_sample', return_value={}), \
             mock.patch.object(acceptance.time, 'monotonic_ns', side_effect=[0, 10_000_000]):
            self.begin()
            self.observer.worker(self.daemon, pipeline)
        row = acceptance.read_metrics(self.session)[0]
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertFalse(row['ok'])
        self.assertIsNone(row['delivery_ready_ms'])
        self.assertIsNone(row['elapsed_ms'])

    def test_swallowed_injection_cancel_is_still_classified_cancelled(self):
        from wayvoice.injector import InjectionCancelled
        def cancelled_delivery():
            raise InjectionCancelled('PRIVATE CANCEL REASON')
        def pipeline():
            self.observer.recognize(lambda: 'PRIVATE DICTATION')
            try:
                self.observer.deliver(cancelled_delivery)
            except InjectionCancelled:
                pass
        with mock.patch.object(acceptance, 'memory_sample', return_value={}), \
             mock.patch.object(acceptance.time, 'monotonic_ns', side_effect=[0, 10_000_000]):
            self.begin()
            self.observer.worker(self.daemon, pipeline)
        row = acceptance.read_metrics(self.session)[0]
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertFalse(row['ok'])
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_malformed_configuration_cannot_prevent_recording_stop(self):
        called = []
        def stop():
            called.append(True)
            self.daemon.recorder.recording = False
            return {'ok': True}
        with mock.patch('wayvoice.config.load_config', side_effect=ValueError('PRIVATE CONFIG')), \
             mock.patch.object(acceptance, 'memory_sample', return_value={}):
            self.assertTrue(self.observer.stop(self.daemon, stop)['ok'])
        self.assertEqual(called, [True])
        self.assertFalse(self.daemon.recorder.recording)
        self.assertNotIn('PRIVATE', json.dumps(self.observer.pending))

    def test_recognition_error_cannot_erase_previously_requested_cancellation(self):
        self.daemon.busy = True
        def cancel():
            self.daemon._transcribe_cancel.set()
        def failing_recognition():
            raise RuntimeError('PRIVATE RECOGNITION ERROR')
        def pipeline():
            try:
                self.observer.recognize(failing_recognition)
            except RuntimeError:
                self.daemon._transcribe_cancel.clear()
        with mock.patch.object(acceptance, 'memory_sample', return_value={}):
            self.begin()
            self.observer.cancel(self.daemon, cancel)
            self.observer.worker(self.daemon, pipeline)
        row = acceptance.read_metrics(self.session)[0]
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertFalse(row['ok'])
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_cancel_before_worker_starts_survives_pipeline_clearing_event(self):
        self.daemon.busy = True
        def cancel():
            self.daemon._transcribe_cancel.set()
            return {'ok': True}
        def pipeline():
            self.daemon._transcribe_cancel.clear()
        with mock.patch.object(acceptance, 'memory_sample', return_value={}):
            self.begin()
            self.assertIsNone(self.observer.active)
            self.assertTrue(self.observer.cancel(self.daemon, cancel)['ok'])
            self.observer.worker(self.daemon, pipeline)
        row = acceptance.read_metrics(self.session)[0]
        self.assertFalse(self.daemon._transcribe_cancel.is_set())
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertFalse(row['ok'])

    def test_recognition_settings_replace_stop_metadata_and_invalidate_warm_phase(self):
        from wayvoice import config
        cfg = config.load_config(strict=True)
        (self.session / 'phase.json').write_text(json.dumps(
            {'phase': 'warm', 'settings_sha256': acceptance.settings_fingerprint(cfg)}))
        actual = dict(cfg, model='actual-model', device='cpu', language='en')
        def pipeline():
            return self.observer.recognize(lambda audio, settings, event: 'PRIVATE DICTATION',
                                          Path('fake.wav'), actual, self.daemon._transcribe_cancel)
        with mock.patch.object(acceptance, 'memory_sample', return_value={}), \
             mock.patch.object(acceptance.time, 'monotonic_ns', side_effect=[0, 10_000_000]):
            self.begin()
            self.assertEqual(self.observer.pending['phase'], 'warm')
            self.observer.worker(self.daemon, pipeline)
        row = acceptance.read_metrics(self.session)[0]
        self.assertEqual(row['phase'], 'functional')
        self.assertEqual(row['model'], 'actual-model')
        self.assertEqual(row['device'], 'cpu')
        self.assertEqual(row['language'], 'en')
        self.assertEqual(row['settings_sha256'], acceptance.settings_fingerprint(actual))
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_cold_worker_accepts_absence_and_refuses_owned_or_verified_survivors(self):
        from wayvoice.runtime import locations, worker_client
        pidfile = self.session / 'run' / 'worker.pid'
        cases = [([], None, None, True),
                 ([{'role': 'worker'}], None, None, False),
                 ([{'role': 'job'}], None, None, False),
                 ([], '123', (123, 'start'), False),
                 ([], '123', None, True)]
        for owners, pid, identity, expected in cases:
            with self.subTest(owners=owners, pid=pid, identity=identity):
                if pid is None:
                    pidfile.unlink(missing_ok=True)
                else:
                    pidfile.write_text(pid)
                with mock.patch.object(worker_client, 'stop_worker') as stop, \
                     mock.patch.object(worker_client, '_worker_ping', return_value=None), \
                     mock.patch.object(acceptance, 'live_owners', return_value=owners), \
                     mock.patch.object(locations, 'worker_pid_path', return_value=pidfile), \
                     mock.patch.object(worker_client, '_worker_identity', return_value=identity):
                    self.assertEqual(acceptance.cold_worker(self.session), expected)
                stop.assert_called_once_with()

    def test_cold_worker_refuses_unknown_server_answering_private_socket(self):
        from wayvoice.runtime import locations, worker_client
        with mock.patch.object(worker_client, 'stop_worker'), \
             mock.patch.object(worker_client, '_worker_ping', return_value={'ok': True}), \
             mock.patch.object(acceptance, 'live_owners', return_value=[]), \
             mock.patch.object(locations, 'worker_pid_path', return_value=self.session / 'run' / 'missing.pid'):
            self.assertFalse(acceptance.cold_worker(self.session))

    def test_reused_pid_is_never_signalled_or_purged(self):
        owner = {'pid': 123, 'start': 'old', 'role': 'ui'}
        with mock.patch('wayvoice.cli.request', return_value={'ok': True}), \
             mock.patch.object(acceptance, 'live_owners', return_value=[owner]), \
             mock.patch.object(acceptance.time, 'monotonic', side_effect=[0, 7]), \
             mock.patch.object(acceptance.os, 'pidfd_open', return_value=99), \
             mock.patch.object(acceptance, '_identity', return_value='reused'), \
             mock.patch.object(acceptance.select, 'select') as poll, \
             mock.patch.object(acceptance.os, 'close') as close, \
             mock.patch.object(acceptance.signal, 'pidfd_send_signal') as send:
            result = acceptance.stop_session(self.session, purge=True)
        self.assertFalse(result['ok'])
        self.assertTrue(self.session.exists())
        send.assert_not_called()
        close.assert_called_once_with(99)
        poll.assert_not_called()

    def test_live_owner_without_pidfd_blocks_purge_and_audio_deletion(self):
        wav = self.session / 'tmp' / 'private.wav'
        wav.write_bytes(b'fake audio')
        owner = {'pid': 123, 'start': 'old', 'role': 'daemon'}
        with mock.patch('wayvoice.cli.request', return_value={'ok': True}), \
             mock.patch.object(acceptance, 'live_owners', return_value=[owner]), \
             mock.patch.object(acceptance.time, 'monotonic', side_effect=[0, 7]), \
             mock.patch.object(acceptance.os, 'pidfd_open', side_effect=OSError('unsupported')), \
             mock.patch.object(acceptance.signal, 'pidfd_send_signal') as send:
            result = acceptance.stop_session(self.session, purge=True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['audio_removed'])
        self.assertEqual(result['remaining_wavs'], 1)
        self.assertTrue(wav.exists())
        send.assert_not_called()

    def test_cancel_marks_current_pending_not_previous_finishing_generation(self):
        self.daemon.busy = True
        self.daemon._record_generation = 2
        previous = self.observer.active = {'record_generation': 1}
        current = self.observer.pending = {'record_generation': 2}
        cancel = mock.Mock(return_value={'ok': True})
        self.observer.cancel(self.daemon, cancel)
        self.assertNotIn('cancelled', previous)
        self.assertIs(current['cancelled'], True)
        cancel.assert_called_once_with()

    def test_missing_phase_file_cannot_prevent_microphone_stop(self):
        (self.session / 'phase.json').unlink()
        def stop():
            self.daemon.recorder.recording = False
            return {'ok': True}
        reply = self.observer.stop(self.daemon, stop)
        self.assertTrue(reply['ok'])
        self.assertFalse(self.daemon.recorder.recording)
        self.assertTrue(self.observer.pending['observer_error'])
        self.assertEqual(self.observer.pending['phase'], 'functional')
