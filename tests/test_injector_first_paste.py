"""The two ways the paste failed only on the first dictation.

Both were found by reading, in the code that raises the ydotoold helper - the
feature that made automatic paste work on a distribution with no ydotool package.

1. The environment was built *before* the helper was raised. ``YDOTOOL_SOCKET`` is
   set only when a socket already exists, so the first dictation after an install
   had no ``YDOTOOL_SOCKET`` and the client fell back to
   ``$XDG_RUNTIME_DIR/.ydotool_socket`` - while the packaged unit listens on
   ``$XDG_RUNTIME_DIR/wayvoice-ydotool.sock``. The paste failed, and the second
   dictation worked. "Works from the second time on" is the kind of bug nobody
   reports and everybody has.

2. Only two socket paths were looked for: the packaged one and /tmp. A
   distribution that ships its own ydotoold puts it in
   ``$XDG_RUNTIME_DIR/.ydotool_socket``, so on such a system the helper looked
   absent, was started a second time by us, and the two fought over /dev/uinput -
   which is exactly what the unit's own comment says would be miserable to debug.

Both paths come from the vendored ydotoold sources, which are the authority:
Daemon/ydotoold.c falls back to /tmp only when XDG_RUNTIME_DIR is unset, and
Client/ydotool.c reads YDG_RUNTIME_DIR.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import injector


class SocketLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_packaged_socket_is_found_first(self):
        packaged = self.runtime / "wayvoice-ydotool.sock"
        packaged.touch()
        self.assertEqual(injector.ydotool_socket(), packaged)

    def test_a_distributions_socket_is_found(self):
        # The one that was missing: ydotoold from the distribution, listening where
        # ydotoald itself listens.
        theirs = self.runtime / injector.DEFAULT_SOCKET_NAME
        theirs.touch()
        self.assertEqual(injector.ydotool_socket(), theirs)

    def test_the_packaged_one_wins_when_both_exist(self):
        packaged = self.runtime / "wayvoice-ydotool.sock"
        packaged.touch()
        (self.runtime / injector.DEFAULT_SOCKET_NAME).touch()
        self.assertEqual(injector.ydotool_socket(), packaged)

    def test_the_name_is_the_one_the_vendored_sources_use(self):
        # Guard rather than a test of this module: the value came from reading
        # third_party/ydotool, and a rename there would break the lookup silently.
        # Both sources build the path from a format string, so the name is looked
        # for as a fragment rather than as a quoted literal.
        vendored = Path(__file__).resolve().parents[1] / "third_party" / "ydotool"
        daemon = (vendored / "Daemon" / "ydotoold.c").read_text(encoding="utf-8")
        client = (vendored / "Client" / "ydotool.c").read_text(encoding="utf-8")
        name = injector.DEFAULT_SOCKET_NAME
        self.assertTrue(name in daemon, f"ydotoold.c no longer knows {name!r}")
        self.assertTrue(name in client, f"ydotool.c no longer knows {name!r}")
        # And /tmp is the fallback for a daemon with no XDG_RUNTIME_DIR, not the
        # normal place - which is what an earlier comment here claimed.
        self.assertTrue(injector.LEGACY_SOCKET in daemon,
                        "the /tmp fallback disappeared from ydotoold.c")


class EnvironmentAfterTheHelperTests(unittest.TestCase):
    """The client must be told where the socket is, on the first try."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, injector, "_helper_started_at", None)
        self.seen = []

    def _paste(self):
        def record(args, **kwargs):
            self.seen.append(kwargs.get("env") or {})
            return subprocess.CompletedProcess(args, 0, "", "")

        def raise_helper():
            # The helper comes up here, as it does after an install: the socket
            # exists from this moment on.
            (self.runtime / "wayvoice-ydotool.sock").touch()
            return True

        with mock.patch.object(injector.shutil, "which", return_value="/usr/bin/ydotool"), \
             mock.patch.object(injector, "ensure_helper_running", side_effect=raise_helper), \
             mock.patch.object(injector, "_run", side_effect=record), \
             mock.patch.object(injector.time, "sleep"):
            ok, message = injector.paste_with_ydotool("standard", "ru")
        self.assertTrue(ok, message)
        return self.seen[0]

    def test_the_first_paste_already_knows_the_socket(self):
        env = self._paste()
        self.assertEqual(env.get("YDOTOOL_SOCKET"),
                         str(self.runtime / "wayvoice-ydotool.sock"),
                         "the first dictation after an install had no YDOTOOL_SOCKET")

    def test_the_environment_is_not_built_before_the_helper_is_raised(self):
        # Read rather than inferred from the value: the order is the defect, and a
        # test that only checks the outcome would stop covering it if the socket
        # happened to exist already.
        import inspect

        source = inspect.getsource(injector.paste_with_ydotool)
        env_at = source.index("env = _ydotool_env()")
        helper_at = source.index("ensure_helper_running()")
        self.assertLess(helper_at, env_at,
                        "the environment is built before the helper is raised")


if __name__ == "__main__":
    unittest.main()
