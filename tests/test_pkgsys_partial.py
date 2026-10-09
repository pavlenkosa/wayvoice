"""One missing package used to cancel the whole install.

``install_packages`` walked the names and returned on the first one the package
manager did not know, before the manager was started at all. "Install everything
missing" - what the settings button and ``wayvoice deps --install-all`` both do -
passes every missing dependency in one call, so on a system whose repositories
carry no ydotool package that call installed nothing at all: pipewire-bin,
wl-clipboard and libnotify-bin stayed uninstalled, and the only thing the user was
told about was ydotool.

Checked against this machine, where the situation is real rather than hypothetical:
``package_available("ydotool")`` is ``False`` and the other three are ``True``.
"""

import unittest
from unittest import mock

from wayvoice import pkgsys


def _apt_with(*, known):
    """A fake apt: knows ``known``, refuses everything else."""
    return lambda name: True if name in known else False


class UnavailableNamesDoNotCancelTheRest(unittest.TestCase):
    def setUp(self):
        self.ran = []

        def fake_popen(command, **kwargs):
            self.ran.append(list(command))
            self.kwargs = kwargs
            return mock.Mock(returncode=0,
                             communicate=mock.Mock(return_value=("", "")))

        patcher = mock.patch.object(pkgsys.subprocess, "Popen", side_effect=fake_popen)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(pkgsys, "requires_privilege", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ran = []

    def _install(self, names, known):
        with mock.patch.object(pkgsys, "package_available",
                               side_effect=_apt_with(known=known)):
            return pkgsys.install_packages(names, language="en")

    def test_the_available_ones_are_installed(self):
        ok, message = self._install(
            ["pipewire-bin", "wl-clipboard", "libnotify-bin", "ydotool"],
            known={"pipewire-bin", "wl-clipboard", "libnotify-bin"})
        self.assertTrue(ok, message)
        self.assertEqual(len(self.ran), 1, "the package manager was never started")
        installed = [name for name in self.ran[0] if not name.startswith("-")]
        self.assertIn("pipewire-bin", installed)
        self.assertNotIn("ydotool", installed)

    def test_the_skipped_name_is_named_in_the_message(self):
        ok, message = self._install(["pipewire-bin", "ydotool"],
                                    known={"pipewire-bin"})
        self.assertTrue(ok)
        self.assertIn("ydotool", message)

    def test_the_installed_names_are_named_too(self):
        ok, message = self._install(["pipewire-bin", "ydotool"],
                                    known={"pipewire-bin"})
        self.assertIn("pipewire-bin", message)

    def test_all_unavailable_is_still_a_refusal(self):
        # Nothing can be installed: there is no point starting a manager, and no
        # reason to show a password prompt for it.
        ok, message = self._install(["ydotool", "nonexistent-thing"], known=set())
        self.assertFalse(ok)
        self.assertEqual(self.ran, [], "the manager was started for nothing")
        self.assertIn("ydotool", message)

    def test_an_unknown_availability_is_tried_anyway(self):
        # package_available answers None for "I could not tell" - a package index
        # that was never refreshed, for one. Refusing on that would report a package
        # as missing when it is really only unlisted.
        calls = []

        def availability(name):
            calls.append(name)
            return None

        with mock.patch.object(pkgsys, "package_available", side_effect=availability):
            ok, message = pkgsys.install_packages(["mystery"], language="en")
        self.assertTrue(ok, message)
        self.assertEqual(len(self.ran), 1)
        self.assertIn("mystery", self.ran[0])

    def test_a_failure_still_reports_the_manager_output(self):
        def failing(command, **kwargs):
            self.ran.append(list(command))
            return mock.Mock(returncode=100,
                             communicate=mock.Mock(return_value=("out", "E: broken")))

        patcher = mock.patch.object(pkgsys.subprocess, "Popen", side_effect=failing)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(pkgsys, "package_available",
                               side_effect=_apt_with(known={"pipewire-bin"})):
            ok, message = pkgsys.install_packages(["pipewire-bin", "ydotool"],
                                                  language="en")
        self.assertFalse(ok)
        self.assertIn("broken", message)


if __name__ == "__main__":
    unittest.main()