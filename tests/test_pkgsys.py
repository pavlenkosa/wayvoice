import subprocess
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


class AvailabilityTests(unittest.TestCase):
    """Knowing a package name is not knowing that the package exists.

    Debian 13 ships no ``ydotool`` at all, and a manager asked to install one
    says so in four words the user has to decode - after an authorization dialog
    and a password prompt. So the question is asked first, and these tests check
    both answers and the moment the answer arrives.
    """

    #: What ``apt-cache policy`` prints in the C locale.
    PRESENT = "Package: wl-clipboard\n  Installed: (none)\n  Candidate: 2.1-2\n"
    #: What apt prints for a name it knows but has no version for - the shape a
    #: Debian 13 machine gives for ydotool.
    NO_VERSION = "ydotool:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"
    #: And for a name it has never heard of: nothing at all.
    UNKNOWN_NAME = ""

    @staticmethod
    def _policy(output: str, returncode: int = 0):
        """A stand-in for the availability query, recording what it was asked."""
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, returncode, output, "")

        return calls, mock.patch.object(pkgsys.subprocess, "run", side_effect=run)

    class _Child:
        """The part of the install that these tests must never really run."""

        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
            self.killed = False

        def communicate(self, timeout=None):
            return self.stdout, self.stderr

        def kill(self):
            self.killed = True

        def terminate(self):
            self.killed = True

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def _install(self, output: str, returncode: int = 0):
        """Run one install against a policy answer, and report what was started."""
        lookups, patched_lookup = self._policy(output, returncode)
        started = []

        def popen(argv, **kwargs):
            started.append((argv, kwargs))
            return self._Child()

        with mock.patch("os.geteuid", return_value=0), \
             mock.patch("shutil.which", _which_returning({
                 "apt-get": "/usr/bin/apt-get", "apt-cache": "/usr/bin/apt-cache",
             })), patched_lookup, \
             mock.patch.object(pkgsys.subprocess, "Popen", side_effect=popen):
            ok, message = pkgsys.install_packages(
                ["ydotool"], timeout=0.1, language="en"
            )
        return ok, message, lookups, started

    def test_a_package_the_system_lacks_is_refused_before_anything_runs(self):
        # The install must not start: no authorization dialog, no password
        # prompt, no five seconds of apt failing to find what is not there.
        ok, message, lookups, started = self._install(self.NO_VERSION)
        self.assertFalse(ok)
        self.assertIn("ydotool", message)
        self.assertEqual(len(lookups), 1, "the policy was not asked exactly once")
        self.assertIn("apt-cache", lookups[0][0][0])
        self.assertEqual(started, [], "the install ran anyway")

    def test_a_package_the_system_has_is_installed(self):
        ok, _message, _lookups, started = self._install(self.PRESENT)
        self.assertTrue(ok)
        self.assertEqual(len(started), 1, "the install never ran")
        self.assertIn("apt-get", started[0][0][0])

    def test_a_name_the_manager_has_never_heard_of_is_also_refused(self):
        # The other shape of "not there": apt prints nothing at all, which is an
        # answer rather than a shrug - and treating it as a shrug would send the
        # user to a command that cannot work.
        ok, message, lookups, started = self._install(self.UNKNOWN_NAME)
        self.assertFalse(ok)
        self.assertIn("ydotool", message)
        self.assertEqual(started, [])

    def test_output_nobody_can_read_is_not_an_answer(self):
        # Something answered, but not with something we understand. That is "don't
        # know", and refusing an install on it would be worse than the failure it
        # prevents.
        _lookups, patched = self._policy("W: apt-cache is having a bad day\n")
        with mock.patch("shutil.which", _which_returning({
            "apt-cache": "/usr/bin/apt-cache",
        })), patched:
            self.assertIsNone(pkgsys.package_available("wl-clipboard", manager="apt"))

    def test_a_lookup_that_fails_does_not_refuse(self):
        # "Don't know" must not be read as "not there": refusing an install
        # because the question could not be asked would be worse than the
        # failure it prevents.
        ok, message, _lookups, started = self._install("", returncode=1)
        self.assertTrue(ok)
        self.assertEqual(len(started), 1, "the install was refused on a shrug")
        self.assertNotIn("repositories", message)

    def test_the_question_is_asked_in_the_c_locale(self):
        # apt translates its output. A parser written against "Candidate:" finds
        # nothing on a Russian system, and answers "don't know" for every package
        # - which is how this check once passed silently for a package that is
        # there and for one that is not.
        _ok, _message, lookups, _started = self._install(self.PRESENT)
        self.assertEqual(lookups[0][1]["env"]["LC_ALL"], "C")

    def test_a_missing_apt_cache_answers_dont_know(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch("shutil.which", _which_returning({"apt-get": "/usr/bin/apt-get"})), \
             mock.patch.object(pkgsys.subprocess, "run") as run:
            done = run.return_value
            done.returncode = 0
            done.stdout = self.PRESENT
            self.assertIsNone(pkgsys.package_available("wl-clipboard"))


if __name__ == "__main__":
    unittest.main()
