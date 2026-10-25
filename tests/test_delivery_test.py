"""External check lifecycle without sending input or opening microphone."""
import unittest
from unittest import mock
from tests.ui_support import GTK_AVAILABLE
if GTK_AVAILABLE:
    from wayvoice.ui.delivery_test import DeliveryCheck, TEST_TEXT
    from wayvoice import injector


class PendingTasks:
    def __init__(self):
        self.jobs=[]
        self.closed=False
    def every(self, _interval, callback):
        self.tick=callback
        return 1
    def run(self, work, done, failed):
        self.jobs.append((work,done,failed))
        return True
    def close(self):
        self.closed=True


@unittest.skipUnless(GTK_AVAILABLE,'GTK bindings unavailable')
class DeliveryCheckTests(unittest.TestCase):
    def setUp(self):
        self.tasks=PendingTasks()
        self.publish=mock.Mock()
        self.changed=mock.Mock()
        self.check=DeliveryCheck(self.tasks,self.changed,self.publish)
        self.cfg={'paste_mode':'standard','ui_language':'en'}
        patch=mock.patch('wayvoice.ui.delivery_test.time.monotonic',return_value=0)
        self.clock=patch.start(); self.addCleanup(patch.stop)

    def start(self):
        self.check.start(self.cfg,'terminal')

    def send(self):
        self.start()
        self.clock.return_value=5
        self.assertFalse(self.check.tick())
        self.assertEqual(len(self.tasks.jobs),1)

    def test_explicit_five_second_boundary_and_snapshot(self):
        self.start()
        self.assertEqual(self.check.seconds,5)
        self.clock.return_value=4.9
        self.check.tick()
        self.assertEqual(self.tasks.jobs,[])
        self.cfg['paste_mode']='copy'
        self.clock.return_value=5
        self.check.tick()
        with mock.patch('wayvoice.ui.delivery_test.injector.inject',return_value=injector.InjectionResult(True)) as inject:
            work,done,_=self.tasks.jobs[0]
            done(work())
        self.assertEqual(inject.call_args.args,(TEST_TEXT,{'paste_mode':'terminal','ui_language':'en'}))
        self.assertNotIn('\n',TEST_TEXT)
        self.assertEqual(self.check.state,'command_sent')
        self.assertEqual(self.publish.call_args.args[0]['route'],'auto')
        self.check.confirm(True)
        self.assertEqual(self.publish.call_args.args[0]['outcome'],'confirmed')

    def test_cancel_countdown_never_sends(self):
        self.start(); self.check.cancel()
        self.clock.return_value=6
        self.assertFalse(self.check.tick())
        self.assertEqual(self.tasks.jobs,[])
        self.assertEqual(self.check.state,'cancelled')

    def test_repeat_and_double_start_cannot_overlap(self):
        self.send()
        self.check.start(self.cfg,'copy')
        self.assertEqual(len(self.tasks.jobs),1)
        self.tasks.jobs[0][1](injector.InjectionResult(False,'helper failed'))
        self.assertEqual(self.check.state,'copied')
        self.assertEqual(self.check.warning,'helper failed')
        self.check.confirm(False)
        self.assertEqual(self.check.result['outcome'],'not_received')
        self.check.start(self.cfg,'copy')
        self.assertEqual(self.check.state,'countdown')

    def test_close_cancels_pending_and_suppresses_late_result(self):
        self.send(); self.check.close()
        result=dict(self.check.result)
        self.tasks.jobs[0][1](injector.InjectionResult(True))
        self.assertTrue(self.tasks.closed)
        self.assertTrue(self.check.cancel_event.is_set())
        self.assertEqual(self.check.result,result)
        self.assertEqual(result['outcome'],'interrupted')

    def test_cancel_request_does_not_relabel_already_sent_input(self):
        self.send(); self.check.cancel()
        self.tasks.jobs[0][1](injector.InjectionResult(True))
        self.assertEqual(self.check.state,'command_sent')

    def test_delivery_failures_never_claim_copied_or_confirmed(self):
        self.send()
        self.tasks.jobs[0][2](injector.InjectionError('clipboard refused'))
        self.assertEqual(self.check.state,'failed')
        self.check.confirm(True)
        self.assertEqual(self.check.result['outcome'],'failed')

    def test_cancelled_worker_and_refused_launch_recover(self):
        self.send()
        self.tasks.jobs[0][2](injector.InjectionCancelled())
        self.assertEqual(self.check.state,'cancelled')
        self.tasks.run=lambda *_:False
        self.check.start(self.cfg,'copy')
        self.clock.return_value=10
        self.check.tick()
        self.assertEqual(self.check.state,'failed')
        self.assertFalse(self.check.running)
