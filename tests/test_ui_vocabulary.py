import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from wayvoice.engine import _postprocess
from tests.ui_support import GTK_AVAILABLE, gtk_display_available

if GTK_AVAILABLE:
    from wayvoice.ui.widgets.vocabulary import VocabularyEditor
    from gi.repository import Gtk


class PipelineVocabularyTests(unittest.TestCase):
    def test_replacements_follow_formatting_and_preserve_target_case(self):
        cfg = {'language': 'en', 'auto_punctuation': True, 'append_space': True,
               'vocabulary_enabled': True, 'vocabulary_rules': 'way voice => WayVoice'}
        self.assertEqual(_postprocess('way voice', cfg), 'WayVoice. ')
        self.assertEqual(_postprocess('way voice', dict(cfg, vocabulary_enabled=False)), 'Way voice. ')

    def test_malformed_saved_rules_reported_and_do_not_rewrite_file(self):
        from wayvoice import config
        import tempfile
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            raw = json.dumps({'vocabulary_enabled': True, 'vocabulary_rules': 'bad rule'})
            path.write_text(raw)
            with patch.object(config, 'config_path', return_value=path):
                cfg = config.load_config()
                self.assertEqual(cfg['vocabulary_rules'], '')
                self.assertIn('vocabulary_rules', config.config_error())
            self.assertEqual(path.read_text(), raw)


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class VocabularyEditorTests(unittest.TestCase):
    def test_preview_uses_draft_and_invalid_rules_block_save(self):
        if not gtk_display_available():
            self.skipTest('no GTK display')
        ctx = SimpleNamespace(state=SimpleNamespace(cfg={}, t=lambda k: k), preferences=Mock())
        editor = VocabularyEditor(ctx)
        ctx.preferences._draft_values.return_value = {'auto_punctuation': False, 'append_space': False,
            'vocabulary_enabled': True, 'vocabulary_rules': 'alpha => Beta'}
        editor.buffer.set_text('alpha => Beta')
        editor.sample.set_text('alpha')
        editor.preview()
        self.assertEqual(editor.result.get_text(), 'Beta')
        editor.buffer.set_text('invalid')
        self.assertFalse(editor.validate())
        editor.buffer.set_text('a' * 16385)
        self.assertLessEqual(editor.buffer.get_char_count(), 16384)
