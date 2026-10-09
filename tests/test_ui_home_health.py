"""Home recovery, accessibility and ephemeral transcript behavior with fake views."""
import unittest
from unittest import mock
try:
    from tests.ui_support import controller_context
    from wayvoice.ui.health_presentation import recovery_text
except (ImportError, ValueError):
    controller_context = None


@unittest.skipIf(controller_context is None, 'GTK unavailable')
class HomeHealthTests(unittest.TestCase):
    def setUp(self):
        self.ctx = controller_context()
        self.ctx.state.t = lambda key, **kw: key
        self.ctx.window._toast = mock.Mock()
        for name in ('mic_button','status_pill','hero_state','hero_caption','health_summary',
                     'health_detail','health_raw','health_expander','retry_button','mic_icon',
                     'hotkey_label','engine_card','model_card','paste_card','last_text',
                     'transcript_meta','transcript_copy','transcript_clear'):
            setattr(self.ctx.home, name, mock.Mock())
        self.ctx.models._apply_download_state = mock.Mock()
        self.ctx.status._maybe_restart = mock.Mock(return_value=False)

    def snapshot(self, **changes):
        from wayvoice import __version__
        reply = {'ok':True,'version':__version__,'engine':{'state':'ready'},'last_text':''}
        reply.update(changes)
        self.ctx.status._apply_status_snapshot((reply, {'shortcut':'F8'}, []))

    def test_offline_has_bounded_grace_and_persistent_cause(self):
        with mock.patch('wayvoice.ui.controllers.status.time.monotonic', return_value=0):
            self.snapshot(ok=False,error='socket missing')
        self.ctx.home.retry_button.set_visible.assert_called_with(False)
        with mock.patch('wayvoice.ui.controllers.status.time.monotonic', return_value=4):
            self.snapshot(ok=False,error='socket missing')
        self.ctx.home.retry_button.set_visible.assert_called_with(True)
        self.ctx.home.health_detail.set_text.assert_called_with('health.daemon_unavailable\nhealth.retry_hint')
        self.ctx.home.health_raw.set_text.assert_called_with('socket missing')

    def test_integration_failure_is_visible_without_waiting(self):
        self.ctx.status._integration_error = 'setup denied'
        self.snapshot(ok=False)
        self.ctx.home.health_raw.set_text.assert_called_with('setup denied')
        self.ctx.home.retry_button.set_visible.assert_called_with(True)

    def test_daemon_reply_does_not_hide_unfixed_integration_failure(self):
        self.ctx.status._integration_error = 'setup denied'
        self.snapshot()
        self.ctx.home.health_raw.set_text.assert_called_with('setup denied')
        self.assertEqual(self.ctx.status._integration_error, 'setup denied')

    def test_active_shortcut_does_not_use_draft_or_repeat_caption(self):
        self.ctx.state.shortcut_binding = 'F9'
        self.snapshot()
        self.ctx.home.hotkey_label.set_text.assert_called_with('F8')
        self.ctx.home.hero_caption.set_text.assert_called_with('home.button_clipboard')

    def test_accessibility_changes_only_with_semantic_state(self):
        self.snapshot(recording=True)
        self.snapshot(recording=True,recording_seconds=4)
        self.ctx.home.mic_button.update_property.assert_called_once()
        self.ctx.home.mic_button.set_tooltip_text.assert_called_with('mic.stop')
        self.snapshot(busy=True)
        self.ctx.home.mic_button.set_tooltip_text.assert_called_with('mic.cancel')

    def test_localized_reason_preserves_raw_backend_details(self):
        self.snapshot(last_error='pw-record: PipeWire stream failed')
        self.ctx.home.health_detail.set_text.assert_called_with('health.recorder_error\nhealth.recorder_hint')
        self.ctx.home.health_raw.set_text.assert_called_with('pw-record: PipeWire stream failed')

    def test_runtime_ready_without_model_does_not_offer_recording(self):
        self.snapshot(model={'supported': True, 'present': False, 'model': 'small'})
        self.ctx.home.mic_button.set_sensitive.assert_called_with(False)
        self.ctx.home.hero_state.set_text.assert_called_with('setup.model_missing')
        self.ctx.home.health_summary.set_text.assert_called_with('health.attention')

    def test_setup_navigation_keeps_draft_and_does_not_start_operations(self):
        from wayvoice.ui.window import WayVoiceWindow
        self.ctx.window.stack = mock.Mock()
        self.ctx.window.settings = mock.Mock()
        self.ctx.window.open_settings('model_state_row')
        self.ctx.window.stack.set_visible_child_name.assert_called_once_with('settings')
        self.ctx.window.settings.focus_section.assert_called_once_with('model_state_row')

    def test_empty_text_replaces_previous_transcript(self):
        self.snapshot(last_text='private words')
        self.snapshot(last_text='')
        self.ctx.home.last_text.set_text.assert_called_with('transcript.empty')
        self.ctx.home.transcript_copy.set_sensitive.assert_called_with(False)

    def test_clear_ignores_old_poll_but_allows_future_identical_text(self):
        transcript = self.ctx.status.transcript
        transcript.paint('same words')
        old_epoch = transcript.epoch
        with mock.patch('wayvoice.ui.controllers.transcript.request', return_value={'ok':True}) as request:
            transcript.clear()
        request.assert_called_once_with('clear-text',timeout=0.8)
        transcript.paint('same words',old_epoch)
        self.assertEqual(transcript.text,'')
        transcript.paint('same words',transcript.epoch)
        self.assertEqual(transcript.text,'same words')

    def test_failed_clear_retains_text_and_explains_error(self):
        transcript = self.ctx.status.transcript
        transcript.paint('text')
        with mock.patch('wayvoice.ui.controllers.transcript.request', return_value={'ok':False,'error':'denied'}):
            transcript.clear()
        self.assertEqual(transcript.text,'text')
        self.assertIsNone(transcript.pending)
        self.ctx.window._toast.assert_called_with('transcript.clear_failed')

    def test_copy_uses_clipboard_and_serializes_actions(self):
        transcript = self.ctx.status.transcript
        transcript.paint('text')
        jobs=[]
        self.ctx.tasks.run=lambda work,done,failed: jobs.append((work,done)) or True
        with mock.patch('wayvoice.ui.controllers.transcript.injector.copy_to_clipboard') as copy:
            transcript.copy()
            transcript.clear()
            self.assertEqual(len(jobs),1)
            self.ctx.home.transcript_copy.set_sensitive.assert_called_with(False)
            jobs[0][1](jobs[0][0]())
        copy.assert_called_once_with('text','en')
        self.ctx.window._toast.assert_called_with('transcript.copied')

    def test_error_classification_has_readable_fallback(self):
        t=lambda key:key
        self.assertEqual(recovery_text(t,'operation timed out')[0],'health.timeout_error')
        self.assertEqual(recovery_text(t,'wl-copy missing')[0],'health.clipboard_error')
        self.assertEqual(recovery_text(t,'unclassified')[0],'health.backend_error')
