"""An upgrade must restart the running per-user service, not just ship new files.

dpkg.log for 0.6.1 -> 0.6.3 -> 0.6.5 showed the upgrades succeeding while the old
daemon process kept running the old code for days: postinst reloaded the *system*
manager and enabled the units --global, but nobody restarted the user instance, and
``Restart=on-failure`` only reacts to a process that dies on its own.
``systemctl --user status`` meanwhile warned "unit file changed on disk".

These tests run the real maintscript with a sandbox PATH in which ``systemctl`` and
``deb-systemd-invoke`` are fakes that record their argv. That keeps the suite offline
and fast while pinning the actual maintscript bytes: the fakes prove what postinst
invokes, exactly the layer a broken upgrade got wrong.

``deb-systemd-invoke --user`` needs systemd >= 249 (on older systemd it prints a note
and exits without acting) and a running user manager (a headless CI root install has
none), so postinst must tolerate both outcomes: the guard is ``|| true`` and the
configure run has to succeed even when the helper fails.
"""

import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POSTINST = REPO / "packaging" / "DEBIAN" / "postinst"

#: argv recorded by every fake, one invocation per line, ``tool arg arg ...``.
FAKE_LOG_ENV = "WAYVOICE_FAKE_LOG"

#: One restart for the daemon, one for the long-running ydotool helper.
#: wayvoice-setup.service is a oneshot without RemainAfterExit - it holds no
#: process whose restart would mean anything.
RESTARTED_UNITS = "wayvoice.service wayvoice-ydotool.service"


def run_postinst(action: str, bin_dir: Path, log: Path) -> subprocess.CompletedProcess:
    """Run packaging/DEBIAN/postinst in a sandbox where every system tool is a fake.

    The PATH contains only the fakes, so the script cannot reach the real systemctl,
    udevadm or modprobe on the test machine; ``command -v`` sees exactly what the test
    put there. The fake log records each invocation the script made.
    """
    sh = shutil.which("sh")
    if not sh:
        raise unittest.SkipTest("no POSIX shell on this machine")
    env = {
        "PATH": str(bin_dir),
        FAKE_LOG_ENV: str(log),
        "LC_ALL": "C",
    }
    try:
        return subprocess.run(
            [sh, str(POSTINST), action],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as error:
        raise unittest.SkipTest(f"cannot execute the maintscript here: {error}") from error


class PostinstRestartTests(unittest.TestCase):
    """dpkg calls postinst with an action argument; only configure may touch services."""

    def setUp(self):
        if not POSTINST.exists():
            self.skipTest("postinst not found in this checkout")
        self.tmp = Path(tempfile.mkdtemp(prefix="wayvoice-postinst-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "calls.log"

    def write_fake(self, name: str, body: str):
        path = self.bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def write_recorder(self, name: str):
        self.write_fake(name, (
            "#!/bin/sh\n"
            "printf '%s\\n' \"${0##*/} $*\" >> \"$" + FAKE_LOG_ENV + "\"\n"
            "exit 0\n"
        ))

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_configure_restarts_the_running_user_service(self):
        self.write_recorder("systemctl")
        self.write_recorder("deb-systemd-invoke")
        result = run_postinst("configure", self.bin_dir, self.log)

        self.assertEqual(result.returncode, 0, result.stderr)
        restart_line = "deb-systemd-invoke --user restart " + RESTARTED_UNITS
        self.assertIn(
            restart_line,
            self.calls(),
            "configure did not restart the running user services",
        )
        # The restart must act on a refreshed definition: a user manager caches
        # unit files, which is where "unit file changed on disk" came from.
        self.assertIn("deb-systemd-invoke --user daemon-reload", self.calls())
        self.assertLess(
            self.calls().index("deb-systemd-invoke --user daemon-reload"),
            self.calls().index(restart_line),
            "the user managers must be reloaded before the restart",
        )
        # The oneshot has nothing to restart, and the pre-existing --global
        # enablement stays in place for the next login.
        for line in self.calls():
            if line.startswith("deb-systemd-invoke --user restart"):
                self.assertEqual(line, restart_line, "an unexpected set of restarted units")
        self.assertIn(
            "systemctl --global enable wayvoice.service wayvoice-setup.service",
            self.calls(),
            "the restart must not replace --global enablement",
        )

    def test_non_configure_actions_never_restart(self):
        self.write_recorder("systemctl")
        self.write_recorder("deb-systemd-invoke")
        for action in ("upgrade", "abort-upgrade", "abort-remove"):
            self.log.write_text("", encoding="utf-8")
            result = run_postinst(action, self.bin_dir, self.log)

            self.assertEqual(result.returncode, 0, result.stderr)
            restarts = [line for line in self.calls() if " restart" in line]
            self.assertEqual(
                restarts, [],
                f"postinst {action} must not restart services",
            )

    def test_a_failing_helper_never_fails_configure(self):
        # A headless CI root install has no user manager and systemd < 249 has no
        # --user support; either way the helper exits nonzero and configure must
        # still succeed.
        self.write_recorder("systemctl")
        self.write_fake("deb-systemd-invoke", (
            "#!/bin/sh\n"
            "printf '%s\\n' \"${0##*/} $*\" >> \"$" + FAKE_LOG_ENV + "\"\n"
            "echo 'systemctl version 248 does not support acting on user instance, skipping' >&2\n"
            "exit 1\n"
        ))
        result = run_postinst("configure", self.bin_dir, self.log)

        self.assertEqual(
            result.returncode, 0,
            "a failing deb-systemd-invoke must not fail dpkg configure",
        )
        self.assertIn(
            "deb-systemd-invoke --user restart " + RESTARTED_UNITS,
            self.calls(),
            "the restart must still be attempted when the helper may fail",
        )


if __name__ == "__main__":
    unittest.main()
