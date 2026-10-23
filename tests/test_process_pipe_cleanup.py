"""Inherited pipes must not extend recognition ownership after its child exits."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from wayvoice.runtime import processes


class ProcessPipeCleanupTests(unittest.TestCase):
    def _detached_pipe(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'detached-pid'
            parent_marker = Path(directory) / 'parent-pid'
            release = Path(directory) / 'release-parent'
            child = (
                'import os,time,pathlib\n'
                'pid=os.fork()\n'
                'if pid==0:\n'
                ' os.setsid()\n'
                f' marker=pathlib.Path({str(marker)!r})\n'
                ' temporary=marker.with_suffix(".tmp")\n'
                ' temporary.write_text(str(os.getpid()))\n'
                ' temporary.replace(marker)\n'
                ' time.sleep(60)\n'
                ' os._exit(0)\n'
                f'while not pathlib.Path({str(release)!r}).exists(): time.sleep(.005)\n'
                + ('print("transcript",flush=True)\n' if mode == 'success'
                   else 'time.sleep(60)\n')
            )
            driver = f'''
import json,os,pathlib,sys,threading,time
from wayvoice.runtime import processes
from unittest import mock
children=[]
popen=processes.subprocess.Popen
def spawn(*args,**kwargs):
    proc=popen(*args,**kwargs)
    children.append(proc)
    marker=pathlib.Path({str(parent_marker)!r})
    temporary=marker.with_suffix('.tmp')
    temporary.write_text(str(proc.pid))
    temporary.replace(marker)
    return proc
event=threading.Event()
timer=threading.Timer(.3,event.set)
readers=processes._start_readers
ready=False
ready_at=None
def await_descendant(proc,overflow):
    global ready,ready_at
    marker=pathlib.Path({str(marker)!r})
    deadline=time.monotonic()+5
    while not marker.exists():
        if proc.poll() is not None or time.monotonic()>=deadline:
            raise RuntimeError('detached descriptor owner did not become ready')
        time.sleep(.005)
    os.kill(int(marker.read_text()),0)
    ready=True
    ready_at=time.monotonic()
    pathlib.Path({str(release)!r}).touch()
    if {mode!r}=='cancel': timer.start()
    return readers(proc,overflow)
started=time.monotonic()
try:
    with mock.patch.object(processes.subprocess,'Popen',side_effect=spawn), mock.patch.object(processes,'_start_readers',side_effect=await_descendant):
        result=processes._run_cancelable([sys.executable,'-c',{child!r}],timeout=.3 if {mode!r}=='timeout' else 10,cancel_event=event)
    outcome=result.stdout.strip()
except Exception as exc:
    outcome=type(exc).__name__
finally:
    timer.cancel()
print(json.dumps({{'outcome':outcome,'ready':ready,'operation_elapsed':time.monotonic()-ready_at if ready_at is not None else None,'elapsed':time.monotonic()-started,'registered':bool(processes._job_procs),'reaped':children[0].poll() is not None,'closed':all(s.closed for s in (children[0].stdout,children[0].stderr))}}))
'''
            # Run outside this test process so a regression cannot strand the
            # whole suite waiting on a reader's buffered stream lock.
            driver_proc = subprocess.Popen(
                [sys.executable, '-c', driver], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            try:
                stdout, stderr = driver_proc.communicate(timeout=10)
                self.assertEqual(driver_proc.returncode, 0, stderr)
                evidence = json.loads(stdout)
                self.assertTrue(evidence['ready'], 'no escaped pipe owner was exercised')
                self.assertLess(evidence['elapsed'], 7)
                self.assertLess(evidence['operation_elapsed'], 2)
                self.assertFalse(evidence['registered'])
                self.assertTrue(evidence['reaped'])
                self.assertTrue(evidence['closed'])
                return evidence['outcome']
            finally:
                # The watchdog owns the driver, whose child starts another
                # session and whose grandchild deliberately escapes that one.
                # Retire every known group even if communicate times out.
                pids = [driver_proc.pid]
                for path in (parent_marker, marker):
                    try:
                        pids.append(int(path.read_text()))
                    except (OSError, ValueError):
                        continue
                try:
                    for pid in pids:
                        with contextlib.suppress(OSError):
                            os.killpg(pid, signal.SIGKILL)
                finally:
                    # Always reap the watchdog child, including failed startup
                    # or malformed PID evidence, without hiding the test failure.
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        driver_proc.communicate(timeout=3)

    def test_timeout_does_not_wait_for_detached_pipe_owner(self):
        self.assertEqual(self._detached_pipe('timeout'), 'TranscriptionTimeout')

    def test_cancel_does_not_wait_for_detached_pipe_owner(self):
        self.assertEqual(self._detached_pipe('cancel'), 'TranscriptionCancelled')

    def test_success_does_not_wait_for_detached_pipe_owner(self):
        self.assertEqual(self._detached_pipe('success'), 'transcript')

    def test_incremental_unicode_and_newline_decoding(self):
        code = (
            'import os,time\n'
            "for part in (b'hello \\xe2', b'\\x82', b'\\xac\\r', b'\\nend\\r'):\n"
            ' os.write(1,part)\n'
            ' time.sleep(.02)\n'
        )
        result = processes._run_cancelable(
            [sys.executable, '-c', code], timeout=3, cancel_event=None,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'),
        )
        self.assertEqual(result.stdout, 'hello €\nend\n')

    def test_malformed_diagnostic_bytes_preserve_successful_transcript(self):
        result = processes._run_cancelable(
            [sys.executable, '-c',
             "import os; os.write(1,b'transcript'); os.write(2,b'warning: \\xff')"],
            timeout=3, cancel_event=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'transcript')
        self.assertEqual(result.stderr, 'warning: \ufffd')

    def test_malformed_transcript_bytes_are_reported_and_release_job(self):
        with self.assertRaises(UnicodeDecodeError):
            processes._run_cancelable(
                [sys.executable, '-c', "import os,time; os.write(1,b'\\xff'); time.sleep(60)"],
                timeout=3, cancel_event=None,
            )
        self.assertFalse(processes._job_procs)
