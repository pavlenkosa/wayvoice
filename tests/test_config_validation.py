"""Malformed hand-edited settings remain recoverable without rewriting the file."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from wayvoice import config, languages, i18n
from wayvoice.daemon import _recording_limit


class ConfigValidationTests(unittest.TestCase):
    def test_invalid_known_values_are_reported_without_losing_unknown_settings(self):
        bad_values = {"language": 42, "ui_language": [], "notify": "false",
                      "model": {}, "shortcut_mode": {}, "max_recording_sec": float("inf"),
                      "transcription_timeout_sec": float("nan")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = json.dumps(dict(bad_values, extra={"keep": True}, device="cpu"))
            path.write_text(raw)
            with mock.patch.object(config, "config_path", return_value=path):
                loaded = config.load_config()
                for key in bad_values:
                    self.assertEqual(loaded[key], config.DEFAULTS[key])
                    self.assertIn(key, config.config_error())
                self.assertEqual(loaded["extra"], {"keep": True})
                self.assertEqual(loaded["device"], "cpu")
                self.assertEqual(path.read_text(), raw)
                path.write_text('{"beam_size": "3", "language": "en"}')
                self.assertEqual(config.load_config()["beam_size"], "3")
                self.assertEqual(config.config_error(), "")

    def test_concurrent_writers_use_distinct_private_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / "config.json"
            original = Path.replace
            temporary_names = []
            def replace(temporary, destination):
                temporary_names.append(temporary)
                return original(temporary, destination)
            with mock.patch.object(config, "config_dir", return_value=folder), mock.patch.object(config, "config_path", return_value=target), mock.patch.object(Path, "replace", replace):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    futures = [workers.submit(config.save_config, {"model": model}) for model in ("small", "tiny")]
                    for future in futures:
                        future.result(timeout=5)
            self.assertEqual(len(set(temporary_names)), 2)
            self.assertIn(json.loads(target.read_text())["model"], ("small", "tiny"))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(folder.iterdir()), {target, target.with_suffix(".lock")})

    def test_nonfinite_numbers_fall_back_and_recording_refuses_them(self):
        for value in (float("inf"), float("-inf"), float("nan"), "Infinity", "NaN"):
            with self.subTest(value=value):
                self.assertEqual(config.number({"limit": value}, "limit", 120), 120)
                self.assertEqual(config.number({"limit": value}, "limit", 120.0), 120.0)
                with self.assertRaisesRegex(ValueError, "max_recording_sec"):
                    _recording_limit({"max_recording_sec": value})

    def test_language_normalizers_defend_direct_callers_too(self):
        for value in (42, [], {}, True):
            # resolve_language consults LC_ALL / LC_MESSAGES / LANG / LANGUAGE
            # before the locale fallback the mock stands in for, so the message
            # environment must be cleared too: a developer shell in ru_RU would
            # otherwise answer before the mocked locale is ever asked.
            with self.subTest(value=value), \
                 mock.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "",
                                              "LANG": "", "LANGUAGE": ""}), \
                 mock.patch.object(i18n.locale, "getlocale", return_value=("en_US", "UTF-8")):
                self.assertEqual(languages.normalize(value), "auto")
                self.assertFalse(languages.is_valid(value))
                self.assertEqual(i18n.resolve_language(value), "en")
