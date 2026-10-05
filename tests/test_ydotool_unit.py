"""The ydotool helper must not restart itself for ever.

``wayvoice-ydotoold`` is installed whether or not the bundled binary was built - it is
a shell wrapper that finds ``../lib/wayvoice/ydotool/ydotoold`` relative to itself. When
that binary is missing, which is what a package built without a compiler looks like, the
wrapper explained that and exited **1**.

The unit's ``ExecCondition`` only checks that a wrapper exists on ``PATH``, so it passed;
``ExecStart`` then ran the wrapper, which exited 1; and ``Restart=on-failure`` with
``RestartSec=2`` restarted the unit every two seconds until logout, printing the same
three lines each time. The package was working exactly as intended - automatic paste
falls back to the clipboard - and the journal filled with a failure loop.

The wrapper now exits 78 (``EX_CONFIG`` from ``sysexits.h``), which the unit lists in
``RestartPreventExitStatus``, and a start limit covers any other permanent failure.

These tests read the two files and run the wrapper in a layout without the binary.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "systemd" / "wayvoice-ydotool.service"
WRAPPER = REPO / "scripts" / "wayvoice-ydotoold"


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def setting(name: str) -> list[str]:
    match = re.search(rf"^{name}=(.*)$", unit_text(), re.MULTILINE)
    return [part.strip() for part in match.group(1).split()] if match else []


def _section_of(name: str) -> str:
    """The section the directive lives in, or "" when it is absent.

    Parsing is line-based because the unit files here have no mid-line section
    markers: a directive belongs to the section header most recently seen above it.
    """
    section = ""
    for line in unit_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif re.match(rf"{re.escape(name)}=", stripped):
            return section
    return ""


class WrapperWithoutTheBundledBinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.wrapper = self.bin / "wayvoice-ydotoold"
        shutil.copy(WRAPPER, self.wrapper)
        self.wrapper.chmod(0o755)

    def _run(self) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.wrapper)], capture_output=True, text=True)

    def test_it_says_why_and_does_not_raise(self):
        result = self._run()
        self.assertEqual(result.returncode, 78,
                         "1 is a plain failure, which is what the unit restarts")
        self.assertIn("clipboard", result.stderr)

    def test_it_writes_nothing_to_stdout(self):
        # The daemon reads this wrapper's stdout as its own start-up chatter.
        self.assertEqual(self._run().stdout, "")

    def test_the_exit_code_is_the_one_the_unit_refuses_to_restart(self):
        result = self._run()
        prevented = [int(code) for code in setting("RestartPreventExitStatus")
                     if code.isdigit()]
        self.assertEqual(prevented, [result.returncode],
                         "the unit would restart a failure that is not going to pass")

    def test_the_unit_still_restarts_other_failures(self):
        self.assertEqual(setting("Restart"), ["on-failure"])

    def test_a_start_limit_bounds_the_remaining_cases(self):
        # systemd only reads StartLimit* from [Unit]; the same keys in [Service] are
        # unknown keys and are silently ignored (verified with systemd-analyze verify
        # and systemctl show on systemd 257: the effective interval fell back to 10 s).
        section = _section_of("StartLimitIntervalSec")
        self.assertEqual(section, "Unit",
                         "StartLimitIntervalSec outside [Unit] is ignored by systemd")
        self.assertEqual(_section_of("StartLimitBurst"), "Unit",
                         "StartLimitBurst outside [Unit] is ignored by systemd")
        self.assertEqual(setting("StartLimitIntervalSec"), ["60"])
        self.assertEqual(setting("StartLimitBurst"), ["5"])


class ConditionTests(unittest.TestCase):
    def test_the_condition_passes_when_the_wrapper_is_on_the_path(self):
        # What made the loop possible: the condition cannot tell a wrapper that will
        # run from one that will not.
        self.assertIn("command -v wayvoice-ydotoold", " ".join(setting("ExecCondition")))

    def test_the_daemons_own_socket_is_still_requested(self):
        self.assertIn("wayvoice-ydotool.sock", unit_text())


if __name__ == "__main__":
    unittest.main()