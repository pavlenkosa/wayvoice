"""Home selection uses the daemon and preserves Settings drafts."""
import copy
import json
import unittest
from unittest import mock
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, Gio
    from wayvoice.ui.window import WayVoiceWindow
    from wayvoice.config import DEFAULTS
except (ImportError, ValueError):
    WayVoiceWindow = None


@unittest.skipIf(WayVoiceWindow is None, 'GTK unavailable')
class QuickSelectionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest('GTK display unavailable')
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.SelectionTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(ui_language='en', engine='faster-whisper', model='small', language='auto')
        with mock.patch('wayvoice.ui.window.TaskRunner'), mock.patch('wayvoice.ui.window.load_config', return_value=self.cfg):
            self.window = WayVoiceWindow(self.app)
        self.addCleanup(self.window.destroy)
        self.ctx = self.window.context
        self.quick = self.ctx.selection
        self.options = [dict(engine='faster-whisper', model=model, label=model, forced_language=None) for model in ('small','tiny')]
        self.reply = {'ok':True, '_prepared_choices':self.options, 'model':{'download':{'state':'idle'}}}
        self.quick.paint(self.reply, self.cfg)
        self.window.tasks.run.reset_mock()

    def test_busy_disables_model_language_and_save(self):
        for flag in ('busy','recording'):
            self.quick.paint(dict(self.reply, **{flag:True}), self.cfg)
            self.assertFalse(self.quick.model.get_sensitive())
            self.quick.save()
            self.window.tasks.run.assert_not_called()

    def test_selection_sends_only_changed_fields_and_expected_values(self):
        self.quick.model.set_selected(1)
        self.quick.language.select_code('ru')
        self.quick.save()
        work = self.window.tasks.run.call_args.args[0]
        with mock.patch('wayvoice.ui.controllers.selection.request', return_value={'ok':True}) as request:
            work()
        payload = json.loads(request.call_args.args[0].partition(' ')[2])
        self.assertEqual(payload['changes'], {'model':'tiny','language':'ru'})
        self.assertEqual(payload['expected'], {'engine':'faster-whisper','model':'small','language':'auto'})

    def test_effective_selection_refresh_preserves_unrelated_draft(self):
        controller = self.ctx.preferences
        draft = controller._draft_values()
        # An unrelated text setting can remain edited while selection fields refresh.
        self.ctx.settings.append_space.set_active(not draft['append_space'])
        cfg = dict(self.cfg, model='tiny', language='ru')
        controller.sync_effective_selection(cfg)
        current = controller._draft_values()
        self.assertEqual(current['model'], 'tiny')
        self.assertEqual(current['language'], 'ru')
        self.assertEqual(current['append_space'], not draft['append_space'])
        self.assertTrue(controller.has_unsaved_changes())
        self.assertEqual(controller._expected_config['model'], 'tiny')

    def test_edited_selection_draft_keeps_original_conflict_baseline(self):
        self.ctx.settings.language.select_code('en')
        self.ctx.preferences.sync_effective_selection(dict(self.cfg, model='tiny', language='ru'))
        self.assertEqual(self.ctx.settings.language.selected_code(), 'en')
        self.assertEqual(self.ctx.preferences._expected_config['language'], 'auto')
        self.assertEqual(self.ctx.state.cfg['language'], 'ru')

    def test_missing_models_offer_settings_without_request(self):
        self.quick.paint(dict(self.reply, _prepared_choices=[]), self.cfg)
        self.assertTrue(self.quick.configure.get_visible())
        self.assertFalse(self.quick.apply.get_sensitive())
        self.quick.save()
        self.window.tasks.run.assert_not_called()

    def test_external_poll_does_not_rebase_an_edited_choice(self):
        self.quick.language.select_code('en')
        self.quick.paint(self.reply, dict(self.cfg, language='ru'))
        self.quick.save()
        work = self.window.tasks.run.call_args.args[0]
        with mock.patch('wayvoice.ui.controllers.selection.request', return_value={'ok':False}) as request:
            work()
        payload = json.loads(request.call_args.args[0].partition(' ')[2])
        self.assertEqual(payload['expected']['language'], 'auto')
        self.assertEqual(payload['changes']['language'], 'en')

    def test_unknown_hand_edited_engine_does_not_break_effective_refresh(self):
        self.ctx.preferences.sync_effective_selection(dict(self.cfg, engine='unknown'))
        self.assertEqual(self.ctx.state.cfg['engine'], 'unknown')
        self.assertEqual(self.ctx.preferences._draft_values()['engine'], 'faster-whisper')

    def test_warm_error_is_visible_and_retry_remains_available(self):
        report = {'state':'error','engine':'faster-whisper','model':'small','error':'Warm failed'}
        self.quick.paint(dict(self.reply, model={'download':report}), self.cfg)
        self.assertEqual(self.quick.model.get_subtitle(), 'Warm failed')
        self.assertTrue(self.quick.apply.get_sensitive())
        self.quick.save()
        self.window.tasks.run.assert_called_once()

    def test_unavailable_effective_model_does_not_claim_another_model_ready(self):
        cfg = dict(self.cfg, model='missing')
        self.quick.paint(dict(self.reply, _prepared_choices=[self.options[1]]), cfg)
        self.assertIn('missing', self.quick.model.get_selected_item().get_string())
        self.assertFalse(self.quick.apply.get_sensitive())
        self.assertEqual(self.quick.model.get_subtitle(), self.ctx.state.t('hero.not_ready'))

    def test_reload_recovers_conflict_baseline_without_losing_settings_draft(self):
        draft = self.ctx.preferences._draft_values()
        self.ctx.settings.append_space.set_active(not draft['append_space'])
        self.quick.language.select_code('en')
        self.quick.paint(self.reply, dict(self.cfg, language='ru'))
        self.quick.reload()
        self.assertEqual(self.quick.language.selected_code(), 'ru')
        self.assertEqual(self.ctx.preferences._draft_values()['append_space'], not draft['append_space'])
        self.quick.save()
        work = self.window.tasks.run.call_args.args[0]
        with mock.patch('wayvoice.ui.controllers.selection.request', return_value={'ok':True}) as request:
            work()
        payload = json.loads(request.call_args.args[0].partition(' ')[2])
        self.assertEqual(payload['expected']['language'], 'ru')
