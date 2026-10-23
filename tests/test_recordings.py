"""Recording cleanup requires private paths and proof that their owner is gone."""
import fcntl
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from wayvoice import recordings


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "recordings"
        patch = mock.patch.object(recordings, "recording_root", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.sessions = []
        self.addCleanup(self._close_sessions)

    def _close_sessions(self):
        for session in self.sessions:
            session.close()
        session = recordings._sessions.pop((os.getpid(), self.root), None)
        if session is not None:
            session.close()

    def session(self):
        session = recordings._RecordingSession(self.root)
        self.sessions.append(session)
        return session

    def old(self, path):
        timestamp = time.time() - 7200
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)
        return path

    def test_allocations_are_private_and_share_process_lifetime_lock(self):
        first = recordings.create_recording()
        second = recordings.create_recording()
        self.assertEqual(first.parent, second.parent)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent.parent, self.root)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.old(first)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(first.exists())

    def test_abandoned_old_take_removed_but_fresh_take_preserved(self):
        session = self.session()
        old = self.old(session.create())
        fresh = session.create()
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((session.path / ".owner").exists())

    def test_empty_abandoned_session_is_removed(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 1)
        self.assertFalse(old.exists())
        self.assertFalse(session.path.exists())

    def test_locked_session_survives_regardless_of_file_age(self):
        session = self.session()
        old = self.old(session.create())
        self.assertEqual(recordings.sweep_stale_recordings(max_age=0), 0)
        self.assertTrue(old.exists())
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 1)

    def test_legacy_and_unrelated_files_are_never_removed(self):
        legacy = self.base / "wayvoice-other-app.wav"
        legacy.write_bytes(b"legacy")
        self.old(legacy)
        session = self.session()
        unrelated = session.path / "notes.wav"
        unrelated.write_bytes(b"unrelated")
        self.old(unrelated)
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertEqual(legacy.read_bytes(), b"legacy")
        self.assertEqual(unrelated.read_bytes(), b"unrelated")

    def test_symlink_take_and_hardlink_do_not_remove_unrelated_file(self):
        session = self.session()
        outside = self.base / "outside.wav"
        outside.write_bytes(b"keep")
        self.old(outside)
        linked = session.path / "take-symlink.wav"
        linked.symlink_to(outside)
        hardlink = session.path / "take-hardlink.wav"
        os.link(outside, hardlink)
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertTrue(linked.is_symlink())
        self.assertTrue(hardlink.exists())

    def test_symlink_session_and_owner_are_rejected(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        owner = session.path / ".owner"
        outside_owner = self.base / "owner"
        owner.rename(outside_owner)
        owner.symlink_to(outside_owner)
        (self.root / "session-link").symlink_to(session.path, target_is_directory=True)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(old.exists())
        self.assertTrue(outside_owner.exists())

    def test_missing_owner_and_unsafe_session_are_skipped(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        os.chmod(session.path, 0o755)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        os.chmod(session.path, 0o700)
        (session.path / ".owner").unlink()
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(old.exists())

    def test_foreign_owned_regular_take_is_skipped(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        real_stat = os.stat

        def foreign_take(path, **kwargs):
            info = real_stat(path, **kwargs)
            if path == old.name and kwargs.get("dir_fd") is not None:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        with mock.patch.object(recordings.os, "stat", side_effect=foreign_take):
            self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(old.exists())

    def test_non_regular_owner_is_skipped_without_waiting_on_fifo(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        owner = session.path / ".owner"
        owner.unlink()
        os.mkfifo(owner, 0o600)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(old.exists())

    def test_unsafe_or_symlink_root_is_neither_used_nor_swept(self):
        session = self.session()
        old = self.old(session.create())
        session.close()
        os.chmod(self.root, 0o755)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        with self.assertRaises(PermissionError):
            recordings.create_recording()
        os.chmod(self.root, 0o700)
        actual = self.base / "actual"
        self.root.rename(actual)
        self.root.symlink_to(actual, target_is_directory=True)
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        with self.assertRaises(OSError):
            recordings.create_recording()
        self.assertTrue((actual / old.parent.name / old.name).exists())

    def test_incomplete_session_publication_is_excluded_by_root_lock(self):
        self.root.mkdir(mode=0o700)
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        fcntl.flock(root_fd, fcntl.LOCK_SH)
        session = self.session()
        old = self.old(session.create())
        session.close()
        self.assertEqual(recordings.sweep_stale_recordings(), 0)
        self.assertTrue(old.exists())
        fcntl.flock(root_fd, fcntl.LOCK_UN)
        self.assertEqual(recordings.sweep_stale_recordings(), 1)
