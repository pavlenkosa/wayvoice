"""Installed GTK must not turn application import failures into skipped tests."""
import importlib
import unittest

from tests.ui_support import GTK_AVAILABLE


@unittest.skipUnless(GTK_AVAILABLE, 'GTK/libadwaita bindings unavailable')
class UiImportContractTests(unittest.TestCase):
    def test_public_entry_and_all_units_import_without_starting_jobs(self):
        ui = importlib.import_module('wayvoice.ui')
        self.assertTrue(callable(ui.main))
        self.assertTrue(callable(ui.WayVoiceWindow))
        self.assertTrue(callable(ui.App))
        for name in ('window','application','state','async_tasks','model_presentation',
                     'pages.home','pages.settings','controllers.models','controllers.settings',
                     'controllers.status','controllers.integration','controllers.shortcut',
                     'dialogs.about','dialogs.confirmations','dialogs.shortcut_window'):
            with self.subTest(module=name):
                importlib.import_module('wayvoice.ui.'+name)
