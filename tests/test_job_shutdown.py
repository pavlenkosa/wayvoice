"""Lifecycle checks use fake Python children, never microphone or desktop input."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import daemon as daemon_mod, engine
from tests.support import isolate_environment, isolate_engine


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


CHILD = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True: time.sleep(1)
"""

DAEMON = """
import sys
from pathlib import Path
from unittest import mock
from wayvoice import daemon as dm, engine
root = Path(sys.argv[1])
mode = sys.argv[2]
args = [sys.executable, str(root / 'child.py'), str(root / 'child.pid')]
cfg = {'engine': 'faster-whisper', 'model': 'small', 'notify': False, 'engine_worker': False}
dm.load_config = lambda: dict(cfg)
engine.model_is_present = lambda *a: False
engine._model_download_args = lambda *a: args
d = dm.WayVoiceDaemon()
d.prepare_on_start = lambda: None
d.recorder = mock.Mock(recording=False)
if mode == 'asr':
    wav = root / 'take.wav'
    wav.write_bytes(b'fake audio')
    d.recorder.recording = True
    def stop():
        d.recorder.recording = False
        return wav
    d.recorder.stop_to_wav = stop
    dm.transcribe = lambda audio, cfg, event: engine._run_cancelable(args, timeout=60, cancel_event=event).stdout
    dm.inject = lambda *a: (_ for _ in ()).throw(AssertionError('late injection'))
    d.stop_recording()
else:
    dm.prepare_model = lambda eng, cfg, progress, event, warming, download: engine.download_model('small', progress, event)
    d._start_model_prepare(cfg)
dm._install_signal_handlers(d)
d.serve()
"""


class DirectShutdownTests(unittest.TestCase):
    def test_quit_and_sigterm_reap_asr_and_download_children(self):
        for mode in ("asr", "download"):
            for stop in ("quit", "sigterm"):
                with self.subTest(mode=mode, stop=stop), tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    runtime = root / "runtime"
                    runtime.mkdir(mode=0o700)
                    (root / "child.py").write_text(CHILD)
                    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime),
                               XDG_STATE_HOME=str(root / "state"),
                               HF_HUB_CACHE=str(root / "cache"))
                    proc = subprocess.Popen([sys.executable, "-c", DAEMON, name, mode],
                                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, start_new_session=True)
                    child_pid = None
                    try:
                        wait_for(lambda: (root / "child.pid").exists())
                        child_pid = int((root / "child.pid").read_text())
                        wait_for(lambda: (runtime / "wayvoice.sock").exists())
                        started = time.monotonic()
                        if stop == "sigterm":
                            proc.send_signal(signal.SIGTERM)
                        else:
                            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                                client.settimeout(3)
                                client.connect(str(runtime / "wayvoice.sock"))
                                client.sendall(b"quit\n")
                                self.assertTrue(json.loads(client.recv(4096))["ok"])
                        out, err = proc.communicate(timeout=8)
                        self.assertEqual(proc.returncode, 0, err)
                        self.assertLess(time.monotonic() - started, 8)
                        wait_for(lambda: not alive(child_pid))
                        self.assertFalse((root / "take.wav").exists())
                        self.assertFalse((runtime / "wayvoice.sock").exists())
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.communicate(timeout=3)
                        if child_pid is not None and alive(child_pid):
                            os.killpg(child_pid, signal.SIGKILL)

    def test_exited_leader_does_not_hide_sigterm_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as name:
            marker = Path(name) / "child.pid"
            leader = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
            proc = subprocess.Popen([sys.executable, "-c", leader, CHILD, str(marker)], start_new_session=True)
            try:
                wait_for(marker.exists)
                pid = int(marker.read_text())
                self.assertTrue(engine._terminate_process(proc))
                wait_for(lambda: not alive(pid))
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        isolate_engine(self)

    def test_cancelled_owner_cannot_spawn_after_shutdown_sweep(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(engine.subprocess, "Popen") as spawn:
            with self.assertRaises(engine.TranscriptionCancelled):
                engine._run_cancelable([sys.executable, "-c", "pass"], timeout=10, cancel_event=event)
        spawn.assert_not_called()

    def test_shutdown_refuses_queued_mutations_and_keeps_idle_worker(self):
        d = daemon_mod.WayVoiceDaemon()
        d.recorder = mock.Mock(recording=False)
        with mock.patch.object(engine, "stop_worker") as stop:
            d._stop_work()
            for command in ("start", "toggle", "prepare-model", "engine-setup"):
                self.assertFalse(d.dispatch(command)["ok"])
        d.recorder.start.assert_not_called()
        self.assertIsNone(d._prepare_thread)
        stop.assert_not_called()

    def test_worker_start_wait_obeys_owner_cancellation(self):
        event = threading.Event()
        with mock.patch.object(engine, "_worker_retry_after", 0), mock.patch.object(engine, "_worker_ping", return_value=None), mock.patch.object(engine, "_start_worker", return_value=True):
            timer = threading.Timer(0.05, event.set)
            timer.start()
            try:
                with self.assertRaises(engine.TranscriptionCancelled):
                    engine.ensure_worker({}, cancel_event=event)
            finally:
                timer.join()

    def test_unstarted_transcription_is_cancelled_without_join_or_wav_leak(self):
        d = daemon_mod.WayVoiceDaemon()
        d.recorder = mock.Mock(recording=False)
        with tempfile.TemporaryDirectory() as name:
            wav = Path(name) / "take.wav"
            wav.write_bytes(b"fake audio")
            d._transcribe_wav = wav
            d._transcribe_thread = threading.Thread(target=d._transcribe_worker, args=(wav,))
            d.busy = True
            d._stop_work()
            self.assertFalse(wav.exists())
            d._transcribe_thread.start()
            d._transcribe_thread.join(timeout=2)
            self.assertFalse(d.busy)
            self.assertIsNone(d._transcribe_thread)
            self.assertTrue(d._transcribe_cancel.is_set())

    def test_worker_lock_wait_is_cancellable(self):
        event = threading.Event()
        with engine._worker_lock:
            timer = threading.Timer(0.02, event.set)
            timer.start()
            try:
                with self.assertRaises(engine.TranscriptionCancelled):
                    engine.ensure_worker({}, cancel_event=event)
            finally:
                timer.join()

    def test_active_worker_is_stopped_but_idle_worker_is_preserved(self):
        event = threading.Event()
        with mock.patch.object(engine, "stop_worker") as stop:
            engine.stop_jobs(event)
            stop.assert_not_called()
            with engine._worker_job(event):
                event.set()
                engine.stop_jobs(event)
            stop.assert_called_once()
        self.assertNotIn(event, engine._worker_jobs)

    def test_reader_start_failure_reaps_registered_child(self):
        procs = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = popen(*args, **kwargs)
            procs.append(proc)
            return proc
        with mock.patch.object(engine.subprocess, "Popen", side_effect=spawn), mock.patch.object(engine, "_start_readers", side_effect=RuntimeError("reader start failed")):
            with self.assertRaisesRegex(RuntimeError, "reader start failed"):
                engine._run_cancelable([sys.executable, "-c", "import time; time.sleep(60)"], timeout=10, cancel_event=threading.Event())
        self.assertIsNotNone(procs[0].poll())
        self.assertNotIn(procs[0], engine._job_procs)
        self.assertTrue(procs[0].stdout.closed)

    def test_malformed_config_unwinds_job_and_wav(self):
        d = daemon_mod.WayVoiceDaemon()
        d.busy = True
        with tempfile.TemporaryDirectory() as name:
            wav = Path(name) / "take.wav"
            wav.write_bytes(b"fake audio")
            with mock.patch.object(daemon_mod, "load_config", side_effect=ValueError("bad config")), mock.patch.object(daemon_mod, "notify"):
                d._transcribe_worker(wav)
            self.assertFalse(wav.exists())
            self.assertFalse(d.busy)
            self.assertIn("bad config", d.last_error)

    def test_background_jobs_share_one_shutdown_deadline(self):
        d = daemon_mod.WayVoiceDaemon()
        d.recorder = mock.Mock(recording=False)
        now = [100.0]
        joined = []
        def join(timeout):
            joined.append(timeout)
            now[0] += 2
        d._transcribe_thread = mock.Mock(ident=1)
        d._prepare_thread = mock.Mock(ident=2)
        d._transcribe_thread.join.side_effect = join
        d._prepare_thread.join.side_effect = join
        with mock.patch.object(daemon_mod.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(daemon_mod, "stop_jobs"):
            d._stop_work()
        self.assertEqual(joined, [daemon_mod.JOB_DRAIN_TIMEOUT, daemon_mod.JOB_DRAIN_TIMEOUT - 2])

    def test_cancelled_warm_waiter_cannot_hide_active_worker_before_sweep(self):
        d = daemon_mod.WayVoiceDaemon()
        sent = threading.Event()
        finished = threading.Event()
        errors = []
        def warm_call(*args, **kwargs):
            sent.set()
            self.assertTrue(args[3].wait(2))
            raise engine.TranscriptionCancelled("Warm-up cancelled")
        def warm():
            try:
                engine.warm_worker({}, cancel_event=d._prepare_cancel)
            except engine.TranscriptionCancelled:
                pass
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        d.recorder = mock.Mock(recording=False)
        d.recorder.cancel.side_effect = lambda: self.assertTrue(finished.wait(2))
        with mock.patch.object(engine, "ensure_worker", return_value=True), mock.patch.object(engine, "_worker_request", side_effect=warm_call), mock.patch.object(engine, "_worker_ping", return_value={"warm": False}), mock.patch.object(engine, "stop_worker") as stop:
            d._prepare_thread = threading.Thread(target=warm)
            d._prepare_thread.start()
            self.assertTrue(sent.wait(2))
            d._stop_work()
            d._prepare_thread.join(timeout=2)
            self.assertNotIn(d._prepare_cancel, engine._worker_jobs)
            stop.assert_called_once()
        self.assertEqual(errors, [])

    def test_pending_wav_unlink_error_does_not_abort_shutdown_state_reset(self):
        d = daemon_mod.WayVoiceDaemon()
        d.recorder = mock.Mock(recording=False)
        d._transcribe_thread = threading.Thread(target=lambda: None)
        d._transcribe_wav = mock.Mock()
        d._transcribe_wav.unlink.side_effect = PermissionError("blocked")
        d.busy = True
        d._stop_work()
        self.assertFalse(d.busy)
        self.assertTrue(d._shutdown.is_set())
