import unittest
from unittest import mock
from wayvoice import audio_feedback


class FeedbackTests(unittest.TestCase):
    def test_disabled_never_starts_playback(self):
        with mock.patch.object(audio_feedback.threading, 'Thread') as thread:
            audio_feedback.play_cue('start')
        thread.assert_not_called()

    def test_failure_releases_slot_and_has_bounded_playback(self):
        with mock.patch.object(audio_feedback.threading, 'Thread') as thread, mock.patch.object(audio_feedback.subprocess, 'run', side_effect=OSError) as run:
            thread.side_effect = lambda **kw: mock.Mock(start=kw['target'])
            audio_feedback.play_cue('stop', True)
            self.assertEqual(run.call_args.kwargs['timeout'], 2)
            self.assertTrue(audio_feedback._slot.acquire(blocking=False))
            audio_feedback._slot.release()
