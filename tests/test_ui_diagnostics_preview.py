"""Preview never copies implicitly; saving uses asynchronous private file IO."""
import os
import stat
import tempfile
import time
from pathlib import Path
import unittest
from unittest import mock

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk, GLib
except (ImportError, ValueError):
    Gtk = None
else:
    from wayvoice.ui.dialogs.diagnostics import diagnostics_preview


@unittest.skipUnless(Gtk and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(), "GTK display unavailable")
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.parent = Gtk.Window()
        self.addCleanup(self.parent.destroy)
        self.copy = mock.Mock()
        self.win = diagnostics_preview(self.parent, "review this report", lambda key, **kwargs: key, self.copy)
        self.addCleanup(self.win.destroy)
        self.buttons = self.win.get_child().get_last_child()

    def test_preview_does_not_replace_clipboard_until_copy_clicked(self):
        self.copy.assert_not_called()
        scroll = self.win.get_child().get_first_child().get_next_sibling()
        buffer = scroll.get_child().get_buffer()
        self.assertEqual(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True), "review this report")
        self.buttons.get_first_child().emit("clicked")
        self.copy.assert_called_once_with()
        self.assertTrue(self.win.get_destroy_with_parent())

    def test_save_uses_async_private_file_and_late_result_is_safe(self):
        chooser = mock.Mock()
        file = chooser.get_file.return_value
        with mock.patch.object(Gtk, "FileChooserNative", return_value=chooser):
            save = self.buttons.get_first_child().get_next_sibling()
            save.emit("clicked")
            selected = chooser.connect.call_args.args[1]
            selected(chooser, Gtk.ResponseType.ACCEPT)
        args = file.replace_contents_bytes_async.call_args.args
        self.assertEqual(args[0].get_data(), b"review this report")
        self.assertTrue(args[3] & Gio.FileCreateFlags.PRIVATE)
        self.assertTrue(args[3] & Gio.FileCreateFlags.REPLACE_DESTINATION)
        self.assertFalse(save.get_sensitive())
        self.win.close()
        args[-1](file, mock.Mock())
        file.replace_contents_finish.assert_called_once()
        self.copy.assert_not_called()


    def test_real_async_save_preserves_utf8_bytes_and_private_permissions(self):
        report = 'Отчёт — проверка 🗣'
        self.win.destroy()
        self.win = diagnostics_preview(self.parent, report, lambda key, **kw: key, self.copy)
        self.addCleanup(self.win.destroy)
        self.buttons = self.win.get_child().get_last_child()
        chooser = mock.Mock()
        with tempfile.TemporaryDirectory(prefix='wayvoice-diagnostics-test-') as folder:
            target = Path(folder) / 'report.txt'
            chooser.get_file.return_value = Gio.File.new_for_path(str(target))
            with mock.patch.object(Gtk, 'FileChooserNative', return_value=chooser):
                save = self.buttons.get_first_child().get_next_sibling()
                save.emit('clicked')
                chooser.connect.call_args.args[1](chooser, Gtk.ResponseType.ACCEPT)
            # Exercise actual Gio ownership beyond the initiating callback.
            deadline = time.monotonic() + 3
            while not save.get_sensitive() and time.monotonic() < deadline:
                GLib.MainContext.default().iteration(False)
                time.sleep(0.01)
            self.assertTrue(save.get_sensitive(), 'asynchronous save did not complete')
            self.assertEqual(target.read_bytes(), report.encode('utf-8'))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.copy.assert_not_called()
