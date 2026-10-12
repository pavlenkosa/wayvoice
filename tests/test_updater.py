import tempfile
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from wayvoice import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for target, name in [('wayvoice.updater.lock_path', 'update.lock'),
                             ('wayvoice.engine_setup.setup_lock_path', 'setup.lock')]:
            patcher = patch(target, return_value=self.directory / name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_runtime_setup_and_update_are_mutually_exclusive(self):
        import fcntl
        from wayvoice import engine_setup
        with engine_setup.setup_lock_path().open('a') as setup:
            fcntl.flock(setup, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with updater.update_lock():
                with self.assertRaisesRegex(ValueError, 'runtime preparation'):
                    with updater.runtime_update_lock():
                        self.fail('Update entered active runtime setup')
        self.assertFalse(updater.updating())
        with updater.update_lock(), updater.runtime_update_lock():
            with engine_setup.setup_lock_path().open('a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with updater.runtime_update_lock():
            pass  # failure/success paths release ownership

    def test_setup_and_daemon_start_do_not_prepare_during_update(self):
        from wayvoice import engine_setup
        from wayvoice.daemon import WayVoiceDaemon
        import os
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.directory)}), \
             patch.object(engine_setup, '_install_once') as install, \
             patch.object(engine_setup, 'faster_runtime') as runtime, \
             patch('wayvoice.daemon.load_config') as config, \
             updater.update_lock():
            self.assertEqual(engine_setup.main(), 1)
            runtime.assert_not_called()
            install.assert_not_called()
            WayVoiceDaemon.prepare_on_start(object.__new__(WayVoiceDaemon))
            config.assert_not_called()

    def release(self, name='wayvoice_0.6.9_amd64.deb'):
        release = {'tag_name': 'v0.6.9', 'assets': []}
        for item in (name, 'SHA256SUMS'):
            release['assets'].append({'name': item, 'browser_download_url':
                updater.REPOSITORY + '/releases/download/v0.6.9/' + item})
        return release

    def test_asset_selection_and_origin(self):
        for kind, arch, name in [('deb', 'amd64', 'wayvoice_0.6.9_amd64.deb'),
                                 ('rpm', 'x86_64', 'wayvoice-0.6.9-1.fc44.x86_64.rpm')]:
            release = self.release(name)
            asset = updater.select_asset(release, kind, arch)
            self.assertEqual(asset['name'], name)
            self.assertTrue(updater.asset_url(release, asset).startswith(updater.REPOSITORY))
            asset['browser_download_url'] = 'https://example.com/' + name
            with self.assertRaises(ValueError):
                updater.asset_url(release, asset)
            with self.assertRaises(ValueError):
                updater.select_asset(release, kind, 'wrong-arch')

    def test_release_version_order_and_prerelease_rejected(self):
        self.assertGreater(updater.version('v0.6.10'), updater.version('0.6.9'))
        for value in ('v0.6.9-beta', '../0.6.9', 'latest'):
            with self.assertRaises(ValueError):
                updater.version(value)
        with patch.object(updater, '_read', return_value=b'{"tag_name":"v0.6.9","prerelease":true}'):
            with self.assertRaises(ValueError):
                updater.check()

    def test_package_metadata_must_match(self):
        with patch.object(updater, '_output', return_value='Package: wayvoice\nVersion: 0.6.9\nArchitecture: amd64'):
            updater.validate_package(Path('/tmp/example.deb'), 'deb', 'amd64', '0.6.9')
            with self.assertRaises(ValueError):
                updater.validate_package(Path('/tmp/example.deb'), 'deb', 'arm64', '0.6.9')

    def test_checksum_failure_never_installs(self):
        release = self.release()
        sums = ('0' * 64 + '  wayvoice_0.6.9_amd64.deb\n').encode()
        with patch.object(updater, 'package_system', return_value=('deb', 'amd64')), \
             patch.object(updater, 'check', return_value={'available': True, 'version': '0.6.9', 'release': release}), \
             patch.object(updater, '_read', side_effect=[sums, b'corrupt']), \
             patch.object(updater.shutil, 'which', return_value='/usr/bin/tool'), \
             patch.object(updater.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                updater.install()
            run.assert_not_called()

    def test_package_metadata_failure_never_installs(self):
        release = self.release()
        data = b'package'
        sums = (hashlib.sha256(data).hexdigest() + '  wayvoice_0.6.9_amd64.deb\n').encode()
        with patch.object(updater, 'package_system', return_value=('deb', 'amd64')), \
             patch.object(updater, 'check', return_value={'available': True, 'version': '0.6.9', 'release': release}), \
             patch.object(updater, '_read', side_effect=[sums, data]), \
             patch.object(updater, 'validate_package', side_effect=ValueError('metadata mismatch')) as validate, \
             patch.object(updater, '_install_process') as install:
            with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
                updater.install()
        validate.assert_called_once()
        install.assert_not_called()

    def test_install_then_fresh_launcher_restart(self):
        release = self.release()
        data = b'package'
        sums = (hashlib.sha256(data).hexdigest() + '  wayvoice_0.6.9_amd64.deb\n').encode()
        with patch.object(updater, 'package_system', return_value=('deb', 'amd64')), \
             patch.object(updater, 'check', return_value={'available': True, 'version': '0.6.9', 'release': release}), \
             patch.object(updater, '_read', side_effect=[sums, data]), \
             patch.object(updater, 'validate_package'), \
             patch.object(updater.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch('builtins.input', return_value='y'), \
             patch('wayvoice.cli.request', return_value={'ok': True}), \
             patch.object(updater.subprocess, 'run') as run:
            updater.install()
            self.assertIn('apt-get', run.call_args_list[0].args[0][-4])
            self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/wayvoice', 'update', '--restart', '0.6.9'])
            run.reset_mock()
            with patch.object(updater, '_read', side_effect=[sums, data]), \
                 patch('wayvoice.cli.request', return_value={'ok': False, 'error': 'busy'}):
                with self.assertRaisesRegex(ValueError, 'busy'):
                    updater.install()
                run.assert_not_called()
                self.assertFalse(updater.updating())


    def test_gui_install_is_noninteractive_and_confirms_restart_before_done(self):
        import io
        import json
        from contextlib import redirect_stdout
        release = self.release()
        data = b'package'
        sums = (hashlib.sha256(data).hexdigest() + '  wayvoice_0.6.9_amd64.deb\n').encode()
        output = io.StringIO()
        with patch.object(updater, 'package_system', return_value=('deb', 'amd64')), \
             patch.object(updater, 'check', return_value={'available': True, 'version': '0.6.9', 'release': release}), \
             patch.object(updater, '_read', side_effect=[sums, data]), \
             patch.object(updater, 'validate_package'), \
             patch.object(updater.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(updater.os, 'geteuid', return_value=1000), \
             patch('builtins.input', side_effect=AssertionError('GUI must not read stdin')), \
             patch('wayvoice.cli.request', return_value={'ok': True}), \
             patch.object(updater.subprocess, 'run') as run, redirect_stdout(output):
            updater.command(['--gui-install'])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item['stage'] for item in events],
                         ['check', 'download', 'verify', 'authorize', 'restart', 'done'])
        self.assertTrue(events[-1]['installed'])
        self.assertTrue(events[-1]['updated'])
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/wayvoice', 'update', '--restart', '0.6.9'])
        for call in run.call_args_list:
            self.assertIs(call.kwargs['stdout'], call.kwargs['stderr'])
            self.assertNotIn('timeout', call.kwargs)
        self.assertFalse(updater.updating())

    def test_gui_restart_failure_reports_installed_and_bounded_log(self):
        import io
        import json
        from contextlib import redirect_stdout
        output = io.StringIO()

        def failed_install(*, confirm, event):
            self.assertFalse(confirm)
            event(stage='restart', installed=True)
            def failed_process(args, **kwargs):
                kwargs['stderr'].write(b'x' * 10000 + b' daemon failed')
                raise updater.subprocess.CalledProcessError(1, args)
            with patch.object(updater.subprocess, 'run', side_effect=failed_process):
                updater._install_process(['wayvoice', 'update', '--restart', '0.6.9'], str(self.directory), event)

        with patch.object(updater, 'install', side_effect=failed_install), redirect_stdout(output):
            with self.assertRaises(SystemExit) as failure:
                updater.command(['--gui-install'])
        self.assertEqual(failure.exception.code, 1)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item['stage'] for item in events], ['restart', 'error'])
        self.assertTrue(events[-1]['installed'])
        self.assertTrue(events[-1]['message'].endswith('daemon failed'))
        self.assertLess(len(events[-1]['message']), 4500)

    def test_update_lock_blocks_daemon_start_and_releases(self):
        from wayvoice.daemon import WayVoiceDaemon
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updater, 'lock_path', return_value=Path(directory) / 'update.lock'):
            self.assertFalse(updater.updating())
            with updater.update_lock():
                self.assertTrue(updater.updating())
                daemon = object.__new__(WayVoiceDaemon)
                import threading
                daemon._lock = threading.RLock()
                self.assertFalse(daemon.start_recording()['ok'])
                with self.assertRaises(ValueError):
                    with updater.update_lock():
                        self.fail('Second updater entered')
            self.assertFalse(updater.updating())

    def test_readiness_rejects_active_work(self):
        from wayvoice.daemon import WayVoiceDaemon
        import threading
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updater, 'lock_path', return_value=Path(directory) / 'update.lock'):
            daemon = object.__new__(WayVoiceDaemon)
            daemon._lock = threading.RLock()
            daemon.recorder = SimpleNamespace(recording=False)
            daemon.busy = False
            daemon.preparation = SimpleNamespace(running=False)
            daemon._model_maintenance = None
            self.assertFalse(daemon.dispatch('update-ready')['ok'])
            with updater.update_lock():
                self.assertTrue(daemon.dispatch('update-ready')['ok'])
                for field in ('busy', '_model_maintenance'):
                    setattr(daemon, field, True)
                    self.assertFalse(daemon.dispatch('update-ready')['ok'])
                    setattr(daemon, field, False)
                daemon.preparation.running = True
                self.assertFalse(daemon.dispatch('update-ready')['ok'])
                daemon.preparation.running = False
                daemon.recorder.recording = True
                self.assertFalse(daemon.dispatch('update-ready')['ok'])

    def test_terminal_signals_do_not_release_transaction_ownership(self):
        import os
        import signal
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(updater, 'lock_path', return_value=Path(directory) / 'update.lock'):
            original = signal.getsignal(signal.SIGINT)
            with updater.installation_signals(), updater.update_lock():
                child = updater.subprocess.Popen(['/bin/sleep', '0.1'])
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                    os.kill(os.getpid(), signal.SIGHUP)
                    self.assertTrue(updater.updating())
                    self.assertIsNone(child.poll())
                    child.wait(timeout=2)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
            self.assertFalse(updater.updating())
            self.assertEqual(signal.getsignal(signal.SIGINT), original)

    def test_gui_restart_restores_absent_daemon_without_reinstalling(self):
        import contextlib, io, json
        output = io.StringIO()
        with patch('wayvoice.cli.request', side_effect=[{'ok': False}, {'ok': True}]), \
             patch('wayvoice.service.start_daemon', return_value=True) as start, \
             patch.object(updater, '_install_process') as run, \
             patch.object(updater, 'install') as install, \
             patch.object(updater.shutil, 'which', return_value='/prefix/bin/wayvoice'), \
             contextlib.redirect_stdout(output):
            updater.gui_install('0.6.10')
        start.assert_called_once_with(wait=15)
        install.assert_not_called()
        self.assertEqual(run.call_args.args[0], ['/prefix/bin/wayvoice', 'update', '--restart', '0.6.10'])
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['stage'], 'done')
        self.assertFalse(updater.updating())

    def test_gui_restart_does_not_restart_busy_daemon(self):
        import contextlib, io, json
        output = io.StringIO()
        with patch('wayvoice.cli.request', side_effect=[{'ok': True}, {'ok': False, 'error': 'busy'}]), \
             patch.object(updater, '_install_process') as run, \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            updater.gui_install('0.6.10')
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['stage'], 'error')
        self.assertFalse(updater.updating())
