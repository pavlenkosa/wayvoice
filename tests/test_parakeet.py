"""Parakeet uses local files and the shared owned one-shot process runner."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from wayvoice import engine
from wayvoice.runtime import parakeet, processes
from wayvoice.runtime.errors import TranscriptionCancelled, TranscriptionTimeout


class ParakeetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'nemo-speech'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o700)
        self.model = self.root / 'model.gguf'
        self.model.write_bytes(b'GGUF' + b'\0' * 28)
        self.audio = self.root / 'audio.wav'
        self.audio.write_bytes(b'RIFF')
        self.cfg = dict(engine='parakeet', parakeet_binary=str(self.binary),
                        parakeet_model=str(self.model), language='ru', model='small.en',
                        auto_punctuation=False, append_space=False)

    def test_missing_runtime_or_model_never_spawns(self):
        for key in ('parakeet_binary', 'parakeet_model'):
            cfg = dict(self.cfg, **{key: str(self.root / 'missing')})
            self.assertEqual(engine.engine_status(cfg)['state'], 'missing')
            with mock.patch.object(processes, '_run_cancelable') as run:
                with self.assertRaises(RuntimeError):
                    engine.transcribe(self.audio, cfg)
                run.assert_not_called()

    def test_local_absolute_paths_cpu_json_and_no_whisper_options(self):
        cp = mock.Mock(returncode=0, stdout=json.dumps({'text': 'Привет'}))
        with mock.patch.object(processes, '_run_cancelable', return_value=cp) as run:
            self.assertEqual(engine.transcribe(self.audio, self.cfg), 'Привет')
        args = run.call_args.args[0]
        self.assertEqual(args, [str(self.binary), 'transcribe', str(self.audio),
                               '--model', str(self.model), '--device', 'cpu', '--json'])
        self.assertEqual(engine._language(self.cfg), 'auto')

    def test_invalid_output_and_failure_are_errors_then_next_take_works(self):
        for output in ('broken', '[]', '{}', '{"text":42}', '{"error":"bad"}'):
            with mock.patch.object(processes, '_run_cancelable', return_value=mock.Mock(returncode=0, stdout=output)):
                with self.assertRaisesRegex(RuntimeError, 'output'):
                    engine.transcribe(self.audio, self.cfg)
        with mock.patch.object(processes, '_run_cancelable', return_value=mock.Mock(returncode=1, stderr='bad model')):
            with self.assertRaisesRegex(RuntimeError, 'bad model'):
                engine.transcribe(self.audio, self.cfg)
        with mock.patch.object(processes, '_run_cancelable', return_value=mock.Mock(returncode=0, stdout='{"text":"ok"}')):
            self.assertEqual(engine.transcribe(self.audio, self.cfg), 'ok')

    def test_invalid_gguf_is_not_ready(self):
        self.model.write_bytes(b'partial')
        self.assertEqual(engine.engine_status(self.cfg)['state'], 'missing')

    def test_real_process_timeout_cancel_output_limit_and_cleanup(self):
        event = threading.Event()
        for code, timeout, error in [('import time;time.sleep(30)', 1, TranscriptionTimeout),
                                      ('print("x"*1100000)', 5, RuntimeError)]:
            with mock.patch.object(parakeet, 'command', return_value=[sys.executable, '-c', code]):
                with self.assertRaises(error):
                    parakeet.transcribe(self.audio, dict(self.cfg, transcription_timeout_sec=timeout), event)
            self.assertEqual(processes._job_procs, {})
        timer = threading.Timer(.1, event.set)
        timer.start()
        self.addCleanup(timer.join)
        with mock.patch.object(parakeet, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(30)']):
            with self.assertRaises(TranscriptionCancelled):
                parakeet.transcribe(self.audio, self.cfg, event)
        self.assertEqual(processes._job_procs, {})

    def test_auto_only_capability(self):
        self.assertTrue(engine.get_engine('parakeet').auto_language_only)
        self.assertNotIn('device', engine.get_engine('parakeet').settings)


class ParakeetPreparationTests(unittest.TestCase):
    def test_download_rejects_wrong_digest_and_oversized_artifact(self):
        import io
        from wayvoice.parakeet_setup import download
        with tempfile.TemporaryDirectory() as tmp:
            for digest, limit in [('0' * 64, 10), ('0' * 64, 1)]:
                target = Path(tmp) / str(limit)
                with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
                    with self.assertRaises(RuntimeError):
                        download('https://example.com/model', target, digest, limit)

    def test_archive_traversal_is_rejected(self):
        import tarfile
        from wayvoice.parakeet_setup import extract_runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'runtime.tar'
            with tarfile.open(archive, 'w') as out:
                item = tarfile.TarInfo('../escape')
                out.addfile(item)
            with self.assertRaises(RuntimeError):
                extract_runtime(archive, root / 'out')
            self.assertFalse((root / 'escape').exists())

    def test_failed_preparation_cleans_staging_without_overwriting_existing_files(self):
        from wayvoice import parakeet_setup as setup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'user-file'
            marker.write_text('keep')
            with mock.patch.object(setup, 'download', side_effect=RuntimeError('network failed')):
                with self.assertRaisesRegex(RuntimeError, 'network failed'):
                    setup.prepare(root)
            self.assertEqual(marker.read_text(), 'keep')
            self.assertFalse(list(root.glob('.prepare-*')))

    def test_concurrent_preparation_is_refused(self):
        import fcntl
        from wayvoice.parakeet_setup import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (root / '.prepare.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    prepare(root)

    def test_daemon_automatic_setup_never_prepares_parakeet(self):
        from wayvoice.daemon import WayVoiceDaemon
        from wayvoice import daemon
        with mock.patch.object(daemon, 'request_engine_setup') as setup:
            WayVoiceDaemon._prepare_engine(engine.get_engine('parakeet'), {'state': 'missing'})
            setup.assert_not_called()

    def test_explicit_preparation_verifies_and_publishes_then_reuses_files(self):
        import hashlib
        import io
        import tarfile
        from wayvoice import parakeet_setup as setup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'runtime.tar'
            with tarfile.open(archive, 'w') as out:
                content = b'#!/bin/sh\nexit 0\n'
                item = tarfile.TarInfo(setup.RUNTIME_NAME + '/bin/nemo-speech')
                item.mode, item.size = 0o755, len(content)
                out.addfile(item, io.BytesIO(content))
            model = root / 'weights'
            model.write_bytes(b'GGUF' + b'\0' * 28)
            destination = root / 'prepared'
            with mock.patch.multiple(setup, RUNTIME_URL=archive.as_uri(),
                                     RUNTIME_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                                     MODEL_URL=model.as_uri(), MODEL_SIZE=model.stat().st_size,
                                     MODEL_SHA256=hashlib.sha256(model.read_bytes()).hexdigest()):
                setup.prepare(destination)
                self.assertEqual((destination / setup.MODEL_NAME).read_bytes(), model.read_bytes())
                with mock.patch.object(setup, 'download') as download:
                    setup.prepare(destination)
                    download.assert_not_called()
            self.assertFalse(list(destination.glob('.prepare-*')))

    def test_missing_parakeet_startup_hotkey_and_socket_do_not_download(self):
        from tests.support import isolate_environment
        from wayvoice.config import DEFAULTS
        from wayvoice import daemon
        isolate_environment(self)
        cfg = dict(DEFAULTS, engine='parakeet', onboarding_completed=True)
        owner = daemon.WayVoiceDaemon()
        with mock.patch.object(daemon, 'load_config', return_value=cfg), \
             mock.patch.object(daemon, '_sweep_stale_recordings'), \
             mock.patch.object(daemon, 'request_engine_setup') as setup, \
             mock.patch.object(owner.recorder, 'start') as capture, \
             mock.patch.object(daemon, 'notify'):
            owner.prepare_on_start()
            owner.start_recording()
            reply = owner.dispatch('engine-setup')
            self.assertFalse(reply['ok'])
            capture.assert_not_called()
            setup.assert_not_called()
