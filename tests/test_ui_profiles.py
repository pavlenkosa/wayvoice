"""Preset selection batches ordinary widgets without model-operation side effects."""
import unittest
from types import SimpleNamespace
from unittest import mock
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw
    from wayvoice.ui.controllers.profiles import ProfilesController, PROFILES
    from wayvoice.ui.controllers.models import ModelsController
    from wayvoice.engine import engine_ids
    from wayvoice.models import preset_index
except (ImportError, ValueError):
    ProfilesController = None


@unittest.skipIf(ProfilesController is None, 'GTK UI package unavailable')
class ProfilesTests(unittest.TestCase):
    def setUp(self):
        if not Gtk.init_check():
            self.skipTest('GTK display unavailable')
        page = SimpleNamespace()
        for name, choices in (('engine', engine_ids()), ('model', [str(n) for n in range(25)]),
                              ('device', ['auto', 'cpu', 'cuda'])):
            row = Adw.ComboRow()
            row.set_model(Gtk.StringList.new(choices))
            setattr(page, name, row)
        self.model_changed = mock.Mock()
        self.engine_changed = mock.Mock()
        page.engine_selection_handler = page.engine.connect('notify::selected', self.engine_changed)
        page.model_selection_handler = page.model.connect('notify::selected', self.model_changed)
        self.cfg = {'engine': 'custom', 'model': 'large-v3', 'language': 'ru', 'shortcut': 'F9'}
        self.ctx = SimpleNamespace(settings=page, preferences=mock.Mock(),
                                   state=SimpleNamespace(cfg=self.cfg.copy(), t=lambda key: key),
                                   window=mock.Mock())
        self.ctx.models = ModelsController(self.ctx)
        self.controller = ProfilesController(self.ctx)

    def test_each_profile_changes_only_three_draft_widgets_without_selection_jobs(self):
        for name, model in PROFILES.items():
            with self.subTest(profile=name):
                self.ctx.models._download_confirmation_for = model
                self.controller.select(name)
                self.assertEqual(self.ctx.settings.engine.get_selected(), engine_ids().index('faster-whisper'))
                self.assertEqual(self.ctx.settings.model.get_selected(), preset_index(model))
                self.assertEqual(self.ctx.settings.device.get_selected(), 1)
                self.assertEqual(self.ctx.state.cfg, self.cfg)
                self.model_changed.assert_not_called()
                self.engine_changed.assert_not_called()
                self.assertIsNone(self.ctx.models._download_confirmation_for)
        self.assertEqual(self.ctx.preferences._on_engine_selected.call_count, 3)
        self.ctx.preferences._save.assert_not_called()
        self.ctx.preferences._setup_engine.assert_not_called()

    def test_deferred_selection_check_cannot_prepare_or_offer_download(self):
        self.ctx.models._download_confirmation_for = 'small'
        self.controller.select('balanced')
        with mock.patch.object(self.ctx.models, '_ask_daemon_to_prepare_model') as prepare, \
             mock.patch.object(self.ctx.models, '_ask_about_download') as download:
            self.ctx.models._decide_what_to_do_about_the_selected_model(
                {'id': 'small', 'kind': 'repo', 'downloaded': False})
            prepare.assert_not_called()
            download.assert_not_called()

    def test_normal_selection_handlers_are_restored_after_preset(self):
        self.controller.select('balanced')
        self.ctx.settings.model.set_selected(preset_index('large-v3'))
        self.ctx.settings.engine.set_selected(engine_ids().index('custom'))
        self.model_changed.assert_called_once()
        self.engine_changed.assert_called_once()
