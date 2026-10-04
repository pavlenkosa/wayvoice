import unittest
from pathlib import Path
from unittest import mock

from wayvoice import deps, pkgsys

ROOT = Path(__file__).resolve().parents[1]

SUPPORTED = ("apt", "dnf", "pacman", "zypper", "apk", "xbps")

EXPECTED_BINARY = {
    "apt": "apt-get",
    "dnf": "dnf",
    "pacman": "pacman",
    "zypper": "zypper",
    "apk": "apk",
    "xbps": "xbps-install",
}


def _which_returning(mapping):
    def _which(name):
        return mapping.get(name)
    return _which


class ManagerTableTests(unittest.TestCase):
    def test_minimum_set_supported(self):
        for manager in SUPPORTED:
            self.assertIn(manager, pkgsys.MANAGERS)

    def test_manager_binary_matches_expectation(self):
        for manager, binary in EXPECTED_BINARY.items():
            self.assertEqual(pkgsys.MANAGERS[manager].binary, binary)

    def test_detect_order_covers_every_manager(self):
        self.assertEqual(sorted(pkgsys.DETECT_ORDER), sorted(pkgsys.MANAGERS))

    def test_manager_candidates_lists_all(self):
        self.assertEqual(sorted(pkgsys.manager_candidates()), sorted(SUPPORTED))

    def test_command_never_uses_a_shell(self):
        for manager in SUPPORTED:
            argv = pkgsys.MANAGERS[manager].command(["some-package"])
            self.assertNotIn("sh", argv)
            self.assertNotIn("-c", argv)
            self.assertNotIn("bash", argv)
            self.assertNotIn(";", argv)


class DryRunCommandTests(unittest.TestCase):
    def test_argv_for_every_manager(self):
        for manager in SUPPORTED:
            argv = pkgsys.dry_run_command(["x"], manager)
            self.assertIsNotNone(argv, f"{manager}: no command")
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[0], EXPECTED_BINARY[manager])
            self.assertIn("x", argv)
            self.assertEqual(argv[-1], "x")

    def test_argv_has_no_shell_wrapper(self):
        for manager in SUPPORTED:
            argv = pkgsys.dry_run_command(["x"], manager)
            self.assertNotIn("sh", argv)
            self.assertNotIn("bash", argv)
            joined = " ".join(argv)
            self.assertNotIn("&&", joined)
            self.assertNotIn("|", joined)
            self.assertNotIn(";", joined)

    def test_expected_flags(self):
        cases = {
            "apt": ["-y", "--no-install-recommends"],
            "dnf": ["-y"],
            "pacman": ["--needed", "--noconfirm"],
            "zypper": ["--non-interactive"],
        }
        for manager, flags in cases.items():
            argv = pkgsys.dry_run_command(["x"], manager)
            for flag in flags:
                self.assertIn(flag, argv, f"{manager}: missing {flag}")

    def test_multiple_packages_are_appended(self):
        argv = pkgsys.dry_run_command(["a", "b"], "dnf")
        self.assertEqual(argv[-2:], ["a", "b"])

    def test_none_for_empty_package_list(self):
        self.assertIsNone(pkgsys.dry_run_command([], "apt"))

    def test_none_for_unknown_manager(self):
        self.assertIsNone(pkgsys.dry_run_command(["x"], "wayvoice-nonexistent-manager"))

    def test_none_without_detectable_manager(self):
        with mock.patch("shutil.which", _which_returning({})):
            self.assertIsNone(pkgsys.dry_run_command(["x"]))

    def test_none_when_the_package_name_is_unknown(self):
        # ydotool is not packaged on Alpine or Void; no name is invented for
        # them, so no command may be built.
        for manager in ("apk", "xbps"):
            names = pkgsys.resolve_packages("ydotool", manager)
            self.assertIsNone(names, f"{manager}: a name was invented")
            self.assertIsNone(pkgsys.dry_run_command(names, manager))

    def test_known_names_resolve_for_every_manager_that_has_one(self):
        for dep in deps.dependencies():
            for manager in dep.packages:
                names = pkgsys.resolve_packages(dep, manager)
                self.assertEqual(names, [dep.packages[manager]])
                self.assertIsNotNone(pkgsys.dry_run_command(names, manager))

    def test_resolve_all_deduplicates_and_rejects_unknown(self):
        entries = [deps.get("pipewire"), deps.get("pipewire")]
        self.assertEqual(pkgsys.resolve_all(entries, "apt"), ["pipewire-bin"])
        self.assertIsNone(pkgsys.resolve_all([deps.get("ydotool")], "apk"))
        self.assertIsNone(pkgsys.resolve_all(["wayvoice-nonexistent-id"], "apt"))

    def test_detected_manager_is_used_by_default(self):
        with mock.patch("shutil.which", _which_returning({"pacman": "/usr/bin/pacman"})):
            argv = pkgsys.dry_run_command(["x"])
        self.assertEqual(argv[0], "pacman")


class DetectTests(unittest.TestCase):
    def test_detect_none_when_nothing_found(self):
        with mock.patch("shutil.which", _which_returning({})):
            self.assertIsNone(pkgsys.detect_manager())

    def test_detect_each_manager(self):
        for manager, binary in EXPECTED_BINARY.items():
            with mock.patch("shutil.which", _which_returning({binary: f"/usr/bin/{binary}"})):
                self.assertEqual(pkgsys.detect_manager(), manager)

    def test_detect_prefers_the_more_specific_binary(self):
        # A Debian system that also has a dnf front-end installed must be
        # detected as apt, because apt comes first in the probe order.
        found = {"apt-get": "/usr/bin/apt-get", "dnf": "/usr/bin/dnf"}
        with mock.patch("shutil.which", _which_returning(found)):
            self.assertEqual(pkgsys.detect_manager(), "apt")

    def test_detect_does_not_execute_anything(self):
        probed = []

        def _which(name):
            probed.append(name)
            return "/usr/bin/dnf" if name == "dnf" else None

        with mock.patch("shutil.which", _which):
            self.assertEqual(pkgsys.detect_manager(), "dnf")
        # Probing stops at the first match and only looks names up in PATH;
        # no manager binary is ever run.
        self.assertEqual(probed, ["xbps-install", "pacman", "zypper", "apt-get", "dnf"])

    def test_detect_survives_a_raising_which(self):
        def _boom(name):
            raise OSError(name)
        with mock.patch("shutil.which", _boom):
            self.assertIsNone(pkgsys.detect_manager())


class PrivilegeTests(unittest.TestCase):
    def test_requires_privilege_false_for_root(self):
        with mock.patch("os.geteuid", return_value=0):
            self.assertFalse(pkgsys.requires_privilege())

    def test_requires_privilege_true_for_a_user(self):
        with mock.patch("os.geteuid", return_value=1000):
            self.assertTrue(pkgsys.requires_privilege())

    def test_requires_privilege_assumes_privilege_where_it_cannot_ask(self):
        # A platform without geteuid - or one that removed it - must not be read
        # as "no privileges needed", or an installer would be started without
        # pkexec and fail in a way the user cannot act on.  Asserting the type of
        # the answer, which is what this test did, passes for an implementation
        # that returns a constant.
        with mock.patch("os.geteuid", side_effect=AttributeError):
            self.assertTrue(pkgsys.requires_privilege())

    def test_pkexec_path_uses_which(self):
        with mock.patch("shutil.which", _which_returning({"pkexec": "/usr/bin/pkexec"})):
            self.assertEqual(pkgsys.pkexec_path(), "/usr/bin/pkexec")
        with mock.patch("shutil.which", _which_returning({})):
            self.assertIsNone(pkgsys.pkexec_path())


class InstallGuardTests(unittest.TestCase):
    """The install path must never be reached implicitly, and these tests must
    never run a package manager."""

    def test_install_refuses_without_a_detectable_manager(self):
        with mock.patch("shutil.which", _which_returning({})), \
                mock.patch("subprocess.run") as run:
            ok, message = pkgsys.install_packages(["x"])
        self.assertFalse(ok)
        self.assertTrue(message)
        run.assert_not_called()

    def test_install_refuses_an_empty_list(self):
        with mock.patch("subprocess.run") as run:
            ok, _ = pkgsys.install_packages([])
        self.assertFalse(ok)
        run.assert_not_called()

    def test_install_refuses_without_pkexec_when_unprivileged(self):
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("shutil.which", _which_returning({"apt-get": "/usr/bin/apt-get"})), \
                mock.patch("subprocess.run") as run:
            ok, message = pkgsys.install_packages(["pipewire-bin"])
        self.assertFalse(ok)
        self.assertIn("pkexec", message)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
