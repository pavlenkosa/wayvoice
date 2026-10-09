"""Backend detection never installs a GNOME binding on another desktop."""
import os
import subprocess
import unittest
from unittest import mock

from wayvoice import shortcut


class ShortcutBackendTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME", "FLATPAK_ID": ""}),
            mock.patch.object(shortcut.Path, "is_file", return_value=False),
            mock.patch.object(shortcut, "command_path", return_value="/opt/Voice App/wayvoice"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_other_desktops_never_write_gsettings_and_offer_quoted_command(self):
        for desktop in ("KDE", "sway", "", "X-GNOME-like"):
            with self.subTest(desktop=desktop), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}), \
                 mock.patch.object(shortcut.subprocess, "run") as run:
                ok, detail = shortcut.apply_shortcut("F8", "en")
            self.assertFalse(ok)
            self.assertIn("'/opt/Voice App/wayvoice' toggle", detail)
            run.assert_not_called()

    def test_flatpak_offers_host_command_without_probing_or_writing(self):
        with mock.patch.dict(os.environ, {"FLATPAK_ID": "io.github.stepan.WayVoice"}), \
             mock.patch.object(shortcut.subprocess, "run") as run:
            ok, detail = shortcut.apply_shortcut("F8", "en")
        self.assertFalse(ok)
        self.assertIn("flatpak run --command=wayvoice io.github.stepan.WayVoice toggle", detail)
        run.assert_not_called()

    def test_missing_active_owner_and_probe_failure_never_write_settings(self):
        for outcome in (subprocess.CompletedProcess([], 0, "(false,)"), FileNotFoundError("gdbus"),
                        subprocess.TimeoutExpired("gdbus", 1)):
            with self.subTest(outcome=outcome), mock.patch.object(shortcut.subprocess, "run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                ok, detail = shortcut.apply_shortcut("F8", "en")
            self.assertFalse(ok)
            self.assertIn("toggle", detail)
            self.assertEqual(run.call_count, 1)

    def test_active_gnome_preserves_other_bindings_and_quotes_command(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            output = "(true,)" if args[0] == "gdbus" else "true" if args[1] == "writable" else "['/existing/']" if args[1] == "get" else ""
            return subprocess.CompletedProcess(args, 0, output)
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}), \
             mock.patch.object(shortcut.subprocess, "run", side_effect=run):
            ok, _detail = shortcut.apply_shortcut("<Control>F8", "en")
        self.assertTrue(ok)
        settings = [args for args, _ in calls if args[:2] == ["gsettings", "set"]]
        self.assertIn("/existing/", settings[0][-1])
        self.assertEqual(next(args[-1] for args in settings if args[-2] == "command"), "'/opt/Voice App/wayvoice' toggle")
        self.assertTrue(all(kwargs["timeout"] == 1.0 and "shell" not in kwargs for _, kwargs in calls))

    def test_unwritable_schema_never_runs_set(self):
        with mock.patch.object(shortcut.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "(true,)"), subprocess.CompletedProcess([], 0, "false")
        ]) as run:
            self.assertFalse(shortcut.apply_shortcut("F8", "en")[0])
        self.assertEqual(run.call_count, 2)
