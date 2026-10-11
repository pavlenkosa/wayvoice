import json
import signal
import subprocess
import threading
import unittest
from unittest import mock

from wayvoice import audio_devices


class AudioDevicesTests(unittest.TestCase):
    def test_discovery_uses_stable_names_and_excludes_monitors(self):
        def node(name, media="Audio/Source", label="Microphone"):
            return {"id": 42, "type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": name, "media.class": media, "node.description": label}}}
        payload = [node("alsa.input"), node("alsa.input"), node("sink", "Audio/Sink"),
                   node("sink.monitor"), {"type": "PipeWire:Interface:Port"}]
        with mock.patch.object(audio_devices.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(payload))) as run:
            self.assertEqual(audio_devices.list_inputs(), [{"name": "alsa.input", "label": "Microphone"}])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_missing_explicit_input_never_falls_back_or_opens_microphone(self):
        with mock.patch.object(audio_devices, "list_inputs", return_value=[]), \
                mock.patch.object(audio_devices.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                audio_devices.probe_level("unplugged")
            spawn.assert_not_called()

    def test_level_check_stops_gracefully_and_returns_pcm_peak(self):
        proc = mock.Mock(returncode=1)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("pw-record", .1), (b"\x00\x00\x00\x40", b"")]
        proc.poll.return_value = 1
        with mock.patch.object(audio_devices.subprocess, "Popen", return_value=proc) as spawn, \
                mock.patch.object(audio_devices.time, "monotonic", side_effect=[0, 0, 2]):
            with mock.patch.object(audio_devices, "validate_target", return_value="input"):
                self.assertEqual(audio_devices.probe_level("input"), .5)
            self.assertIn("--target=input", spawn.call_args.args[0])
            self.assertIn("--properties=node.dont-reconnect=true node.dont-fallback=true", spawn.call_args.args[0])
        proc.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertIn("--raw", spawn.call_args.args[0])
        proc.kill.assert_not_called()
        proc.stdout.close.assert_called_once()

    def test_cancel_during_check_kills_and_reaps_recorder(self):
        event = threading.Event()
        proc = mock.Mock()
        proc.poll.return_value = None
        def communicate(**kwargs):
            if not event.is_set():
                event.set()
                raise subprocess.TimeoutExpired("pw-record", .1)
            return b"", b""
        proc.communicate.side_effect = communicate
        with mock.patch.object(audio_devices.subprocess, "Popen", return_value=proc), \
                mock.patch.object(audio_devices.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                audio_devices.probe_level(cancel_event=event)
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)

    def test_finish_timeout_still_kills_and_reaps(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.side_effect = [subprocess.TimeoutExpired("pw-record", 2), (b"", b"")]
        with mock.patch.object(audio_devices.subprocess, "Popen", return_value=proc), \
                mock.patch.object(audio_devices.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired):
                audio_devices.probe_level()
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
