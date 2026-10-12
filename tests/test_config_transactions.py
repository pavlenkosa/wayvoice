"""Cross-process settings transactions preserve independent edits and raw failures."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wayvoice import config


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, XDG_CONFIG_HOME=self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        config.save_config({"model": "small", "notify": True})
        self.path = config.config_path()

    def writers(self, patches):
        """Both processes use the same pre-edit baseline and start via stdin."""
        code = '''import json,sys
from wayvoice import config
changes,expected=json.loads(sys.argv[1])
sys.stdin.readline()
try:
    config.update_config(changes,expected)
except config.ConfigConflictError as exc:
    print(json.dumps(list(exc.keys)))
    sys.exit(3)
'''
        children = []
        try:
            for patch in patches:
                children.append(subprocess.Popen(
                    [sys.executable, "-c", code, json.dumps(patch)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True))
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            results = []
            for child in children:
                out, err = child.communicate(timeout=10)
                self.assertEqual(err, "")
                results.append((child.returncode, out.strip()))
            return results
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_real_processes_merge_distinct_fields(self):
        result = self.writers([
            ({"model": "tiny"}, {"model": "small"}),
            ({"notify": False}, {"notify": True}),
        ])
        self.assertEqual([code for code, _ in result], [0, 0])
        saved = config.load_config(strict=True)
        self.assertEqual(saved["model"], "tiny")
        self.assertFalse(saved["notify"])

    def test_real_processes_reject_same_field_and_entire_losing_patch(self):
        result = self.writers([
            ({"model": "tiny", "winner_a": True}, {"model": "small", "winner_a": None}),
            ({"model": "medium", "winner_b": True}, {"model": "small", "winner_b": None}),
        ])
        self.assertEqual(sorted(code for code, _ in result), [0, 3])
        loser = next(out for code, out in result if code == 3)
        self.assertEqual(json.loads(loser), ["model"])
        saved = config.load_config(strict=True)
        winner = 0 if result[0][0] == 0 else 1
        self.assertEqual(saved["model"], ("tiny", "medium")[winner])
        self.assertTrue(saved[("winner_a", "winner_b")[winner]])
        self.assertNotIn(("winner_b", "winner_a")[winner], saved)

    def test_same_desired_value_is_not_a_conflict(self):
        config.update_config({"model": "tiny"}, {"model": "small"})
        result = config.update_config({"model": "tiny", "notify": False},
                                      {"model": "small", "notify": True})
        self.assertEqual(result["model"], "tiny")
        self.assertFalse(result["notify"])

    def test_malformed_file_is_preserved(self):
        for raw in (b'{broken', b'{"notify": "false"}',
                    b'{"vocabulary_rules": "broken rule"}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    config.update_config({"model": "tiny"}, {"model": "small"})
                self.assertEqual(self.path.read_bytes(), raw)

    def test_unavailable_lock_does_not_change_config(self):
        before = self.path.read_bytes()
        with mock.patch.object(config.os, "open", side_effect=PermissionError("lock denied")):
            with self.assertRaises(PermissionError):
                config.update_config({"model": "tiny"}, {"model": "small"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_owned_lock_times_out_without_changing_config(self):
        before = self.path.read_bytes()
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(TimeoutError):
                config.update_config({"model": "tiny"}, {"model": "small"})
        self.assertEqual(self.path.read_bytes(), before)
        config.update_config({"model": "tiny"}, {"model": "small"})

    def test_private_mode_and_cleanup_after_replace_failure(self):
        before = self.path.read_bytes()
        with mock.patch.object(Path, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                config.update_config({"model": "tiny"}, {"model": "small"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".config-*.tmp")), [])
        config.update_config({"model": "tiny"}, {"model": "small"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob(".config-*.tmp")), [])

    def test_missing_expected_value_rejected_before_write(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            config.update_config({"model": "tiny"}, {})
        self.assertEqual(self.path.read_bytes(), before)
