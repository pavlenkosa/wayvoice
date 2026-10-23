import unittest
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tests.ui_support import GTK_AVAILABLE

if GTK_AVAILABLE:
    from wayvoice.ui.controllers.audio import AudioController


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class AudioUiTests(unittest.TestCase):
    def test_check_uses_daemon_and_blocks_duplicate_jobs(self):
        ctx = SimpleNamespace(state=SimpleNamespace(cfg={}, t=lambda k: k), tasks=Mock(),
                              settings=SimpleNamespace(microphone=Mock(), audio_check=Mock(),
                                  audio_level=Mock(), audio_meter=Mock(), audio_refresh=Mock()))
        ctx.settings.microphone.get_selected.return_value = 0
        controller = AudioController(ctx)
        controller.check()
        controller.check()
        ctx.tasks.run.assert_called_once()
        work, done, failed = ctx.tasks.run.call_args.args
        with patch('wayvoice.ui.controllers.audio.request', return_value={'ok': True, 'level': .5}) as request:
            self.assertEqual(work(), .5)
        payload = json.loads(request.call_args.args[0].partition(' ')[2])
        self.assertEqual(payload['target'], '')
        self.assertGreater(payload['expires'], time.monotonic())
        done(.5)
        self.assertFalse(controller.pending)
        self.assertEqual(ctx.settings.audio_meter.set_value.call_args.args, (.5,))


class AudioOwnershipTests(unittest.TestCase):
    def test_busy_and_updating_never_open_probe(self):
        from wayvoice.daemon import WayVoiceDaemon
        daemon = WayVoiceDaemon()
        daemon.recorder = Mock(recording=True)
        with patch('wayvoice.audio_devices.probe_level') as probe:
            self.assertFalse(daemon.dispatch('check-microphone ' + json.dumps({'target': '', 'expires': time.monotonic() + 2}))['ok'])
            probe.assert_not_called()
        daemon.recorder.recording = False
        with patch('wayvoice.updater.updating', return_value=True), patch('wayvoice.audio_devices.probe_level') as probe:
            self.assertFalse(daemon.dispatch('check-microphone ' + json.dumps({'target': '', 'expires': time.monotonic() + 2}))['ok'])
            probe.assert_not_called()

    def test_expired_request_never_opens_microphone(self):
        from wayvoice.daemon import WayVoiceDaemon
        daemon = WayVoiceDaemon()
        with patch('wayvoice.audio_devices.probe_level') as probe:
            reply = daemon.dispatch('check-microphone ' + json.dumps({'target': '', 'expires': time.monotonic() - 1}))
            self.assertFalse(reply['ok'])
            self.assertIn('expired', reply['error'])
            probe.assert_not_called()
