"""Bounded recovery against a real local fake worker; no model or microphone."""
import json
import os
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine, fw_worker

SERVER = '''
import json,os,signal,socket,sys,threading,time
path,mode=sys.argv[1:3]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
active=set(); events={}
s=socket.socket(socket.AF_UNIX);s.bind(path);s.listen()
def serve(c):
    try:
        p=json.loads(c.makefile().readline());cmd=p['cmd'];r=p.get('request_id','')
        if cmd=='cancel':
            if r in events: events[r].set()
            reply={'ok':True}
        elif cmd=='request-status':
            reply={'ok':True,'pid':os.getpid(),'request_id':r,'state':'running' if r in active else 'finished'}
        elif cmd=='ping': reply={'ok':True}
        else:
            active.add(r);events[r]=threading.Event()
            if mode=='hang': threading.Event().wait()
            elif mode=='cooperate': events[r].wait(2)
            active.discard(r)
            reply={'ok':True,'request_id':'foreign' if mode=='wrong-id' else r,'text':'late text'}
        c.sendall((json.dumps(reply)+'\\n').encode())
    finally: c.close()
while True:
    c,_=s.accept();threading.Thread(target=serve,args=(c,),daemon=True).start()
'''

class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'worker.sock'
        self.pidfile = Path(self.tmp.name) / 'worker.pid'
        self.script = Path(self.tmp.name) / 'fake.py'
        self.script.write_text(SERVER)
        for name, value in [('worker_socket_path', self.path), ('worker_pid_path', self.pidfile)]:
            p = mock.patch.object(engine, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(engine, 'WORKER_CANCEL_GRACE', 0.08)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(engine, 'WORKER_POLL_INTERVAL', 0.02)
        p.start(); self.addCleanup(p.stop)

    def worker(self, mode):
        self.path.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, str(self.script), str(self.path), mode, 'fw_runner.py', '--serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if proc.poll() is None: proc.kill()
            proc.wait(timeout=2)
        self.addCleanup(cleanup)
        deadline = time.monotonic()+2
        while not self.path.exists() and time.monotonic()<deadline: time.sleep(0.01)
        self.assertTrue(self.path.exists())
        self.pidfile.write_text(str(proc.pid))
        return proc

    def request(self, cancel=None, timeout=2, request_id='take'):
        return engine._worker_transcribe({'cmd':'transcribe','request_id':request_id}, request_id, timeout, cancel)

    def test_hung_cancel_retires_worker_and_next_request_succeeds(self):
        proc = self.worker('hang')
        event = threading.Event()
        timer = threading.Timer(0.06, event.set);timer.start();self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(engine.TranscriptionCancelled): self.request(event)
        proc.wait(timeout=2)
        self.assertLess(time.monotonic()-started, 2)
        self.assertNotIn(event, engine._worker_jobs)
        self.worker('instant')
        self.assertEqual(self.request(request_id='next')['text'], 'late text')

    def test_hung_timeout_retires_worker(self):
        proc = self.worker('hang')
        with self.assertRaises(engine.TranscriptionTimeout): self.request(timeout=0.05)
        proc.wait(timeout=2)

    def test_cooperative_cancel_preserves_worker_and_discards_late_text(self):
        proc = self.worker('cooperate')
        event = threading.Event()
        timer = threading.Timer(0.06,event.set);timer.start();self.addCleanup(timer.cancel)
        with self.assertRaises(engine.TranscriptionCancelled): self.request(event)
        self.assertIsNone(proc.poll())

    def test_cooperative_timeout_preserves_worker_but_never_returns_text(self):
        proc = self.worker('cooperate')
        with self.assertRaises(engine.TranscriptionTimeout): self.request(timeout=0.05)
        self.assertIsNone(proc.poll())

    def test_foreign_reply_is_never_accepted(self):
        self.worker('wrong-id')
        with self.assertRaises(engine.WorkerUnavailable): self.request()

    def test_shutdown_during_grace_keeps_active_owner_until_worker_stops(self):
        proc = self.worker('hang')
        event = threading.Event()
        result=[]
        def run():
            try: self.request(event)
            except Exception as exc: result.append(exc)
        thread=threading.Thread(target=run)
        thread.start()
        deadline=time.monotonic()+1
        while event not in engine._worker_jobs and time.monotonic()<deadline:time.sleep(0.005)
        active=engine.cancel_jobs(event)
        self.assertTrue(active)
        engine.stop_jobs(event, deadline=time.monotonic()+2, active_worker=active)
        thread.join(timeout=2)
        proc.wait(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result[0], engine.TranscriptionCancelled)
        self.assertNotIn(event, engine._worker_jobs)

    def test_recovery_is_bounded_while_startup_lock_is_owned_elsewhere(self):
        proc = self.worker('hang')
        engine._worker_lock.acquire()
        self.addCleanup(lambda: engine._worker_lock.release() if engine._worker_lock.locked() else None)
        started=time.monotonic()
        with self.assertRaises(engine.TranscriptionTimeout): self.request(timeout=0.05)
        self.assertLess(time.monotonic()-started, 2)
        proc.wait(timeout=2)
        engine._worker_lock.release()

    def test_socket_error_after_timeout_never_changes_terminal_failure(self):
        sock = mock.Mock()
        sock.getsockopt.return_value = struct.pack('3i', os.getpid(), os.getuid(), os.getgid())
        sock.recv.side_effect = OSError('connection reset')
        with mock.patch.object(engine.socket, 'socket', return_value=sock), \
             mock.patch.object(engine, '_worker_send_cancel'), \
             mock.patch.object(engine, '_worker_call', return_value={'ok':True,'pid':os.getpid(),'request_id':'take','state':'finished'}), \
             mock.patch.object(engine.time, 'monotonic', side_effect=[0.0, 1.0, 1.0, 1.0]):
            with self.assertRaises(engine.TranscriptionTimeout): self.request(timeout=0.05)
        sock.close.assert_called_once()

    def test_finished_health_check_does_not_retire_worker(self):
        proc=self.worker('instant')
        identity=engine._worker_identity(proc.pid)
        with mock.patch.object(engine, 'stop_worker') as stop:
            engine._recover_worker_request('finished', identity)
        stop.assert_not_called()
        self.assertIsNone(proc.poll())

    def test_failed_preflight_does_not_start_decode_and_allows_fallback(self):
        sock=mock.Mock()
        sock.getsockopt.return_value=struct.pack('3i',os.getpid(),os.getuid(),os.getgid())
        for reply in (OSError('lost worker'), ValueError('bad reply'), TimeoutError('late'), {'ok':False}):
            with self.subTest(reply=reply):
                kwargs={'side_effect':reply} if isinstance(reply,Exception) else {'return_value':reply}
                with mock.patch.object(engine.socket,'socket',return_value=sock), \
                     mock.patch.object(engine,'_worker_call',**kwargs):
                    with self.assertRaises(engine.WorkerUnavailable): self.request()
                sock.sendall.assert_not_called()

    def test_recycled_identity_never_signals_process(self):
        with mock.patch.object(engine, '_worker_identity', return_value=(123, 'new')), mock.patch.object(engine.os, 'kill') as kill:
            engine._recover_worker_request('take', (123,'old'))
        kill.assert_not_called()

    def test_targeted_stop_preserves_replacement_pid_file(self):
        old = self.worker('hang')
        identity = engine._worker_identity(old.pid)
        self.pidfile.write_text(str(os.getpid()))
        engine.stop_worker(timeout=0.03, expected_identity=identity)
        old.wait(timeout=2)
        self.assertEqual(self.pidfile.read_text(), str(os.getpid()))

class QueueCancellationTests(unittest.TestCase):
    def test_cancelled_queue_does_not_wait_for_foreign_decode(self):
        state = fw_worker._WorkerState()
        state.serial.acquire()
        result=[]
        t=threading.Thread(target=lambda: result.append(fw_worker._dispatch({'cmd':'transcribe','request_id':'queued'}, None, None, state)))
        t.start()
        deadline=time.monotonic()+1
        while state.request_status('queued')=='finished' and time.monotonic()<deadline:time.sleep(0.005)
        self.assertEqual(state.request_status('queued'),'queued')
        state.cancel('queued')
        t.join(timeout=1)
        state.serial.release()
        self.assertFalse(t.is_alive())
        self.assertTrue(result[0]['cancelled'])
        self.assertEqual(state.request_status('queued'),'finished')
