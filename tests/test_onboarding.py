from wayvoice.runtime import processes, model_preparation
import json
import os
import tempfile
import unittest
from subprocess import CompletedProcess
from threading import Event
from unittest.mock import patch

from wayvoice import config, engine, onboarding


class OnboardingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=self.tmp.name)
        env.start()
        self.addCleanup(env.stop)

    def write(self, data):
        config.config_dir().mkdir(parents=True, exist_ok=True)
        config.config_path().write_text(json.dumps(data))

    def test_first_run_upgrade_resume_and_corruption(self):
        self.assertTrue(onboarding.needs_onboarding())
        for data, expected in [({}, False), ({"onboarding_completed": False}, True),
                               ({"onboarding_completed": True}, False), ([], False),
                               ({"language": 42, "onboarding_completed": False}, False)]:
            with self.subTest(data=data):
                self.write(data)
                self.assertEqual(onboarding.needs_onboarding(), expected)

    def test_choices_are_pure_and_preserve_settings(self):
        self.write({"paste_mode": "terminal", "shortcut": "F9"})
        before = config.config_path().read_bytes()
        cfg = onboarding.finish_config("ru", "medium")
        self.assertEqual(config.config_path().read_bytes(), before)
        self.assertEqual((cfg["model"], cfg["device"], cfg["language"]), ("medium", "cpu", "auto"))
        self.assertEqual(cfg["shortcut"], "F9")
        onboarding.save_selection("en", "tiny", completed=False)
        self.assertTrue(onboarding.needs_onboarding())
        onboarding.save_selection("en", "tiny", completed=True)
        self.assertFalse(onboarding.needs_onboarding())
        onboarding.save_selection("en", "small", completed=True, deferred=True)
        self.assertTrue(config.load_config()["onboarding_deferred"])
        for language, model in [("xx", "small"), ("auto", "../../model")]:
            with self.assertRaises(ValueError):
                onboarding.finish_config(language, model)

    def test_dialog_baseline_detects_changes_during_preparation(self):
        baseline = config.load_config(strict=True)
        accepted = onboarding.selection_changes("en", "tiny", False, False, baseline)
        onboarding.save_selection("en", "tiny", completed=False, expected=baseline)
        baseline.update(accepted)
        config.update_config({"model": "medium"}, {"model": "tiny"})
        # Completion does not overwrite an untouched model, but a later changed
        # choice must still conflict against the model actually seen by the dialog.
        onboarding.save_selection("en", "tiny", completed=True, expected=baseline)
        self.assertEqual(config.load_config()["model"], "medium")
        with self.assertRaises(config.ConfigConflictError):
            onboarding.save_selection("en", "small", completed=True, expected=baseline)
        self.assertEqual(config.load_config()["model"], "medium")

    def test_all_interface_languages_persist_without_changing_speech_language(self):
        from wayvoice.i18n import SUPPORTED_UI_LANGUAGES
        self.write({'language': 'ja'})
        for language in SUPPORTED_UI_LANGUAGES:
            with self.subTest(language=language):
                onboarding.save_selection(language, 'small', completed=True)
                saved = config.load_config()
                self.assertEqual(saved['ui_language'], language)
                # Onboarding deliberately starts recognition in automatic mode.
                self.assertEqual(saved['language'], 'auto')
                saved['language'] = 'ja'
                config.save_config(saved)
                self.assertEqual(config.load_config()['ui_language'], language)
                self.assertEqual(config.load_config()['language'], 'ja')


class OnboardingPreparationTests(unittest.TestCase):
    cfg = {"engine": "faster-whisper", "model": "small", "engine_worker": True}

    @patch.object(model_preparation, "prepare_model", return_value={"state": "ready"})
    @patch.object(processes, "_run_cancelable")
    @patch.object(engine, "engine_status", return_value={"state": "ready"})
    def test_ready_runtime_only_downloads_no_worker(self, status, run, prepare):
        phases = []
        onboarding.prepare_selection(self.cfg, Event(), on_phase=phases.append)
        run.assert_not_called()
        self.assertFalse(prepare.call_args.args[1]["engine_worker"])
        self.assertTrue(self.cfg["engine_worker"])
        self.assertEqual(phases, ["download"])

    @patch.object(model_preparation, "prepare_model", return_value={"state": "ready"})
    @patch.object(processes, "_run_cancelable", return_value=CompletedProcess([], 0, "", ""))
    @patch.object(engine, "engine_status", side_effect=[{"state": "missing"}, {"state": "ready"}])
    def test_setup_is_owned_and_bounded(self, status, run, prepare):
        event = Event()
        onboarding.prepare_selection(self.cfg, event)
        self.assertIs(run.call_args.kwargs["cancel_event"], event)
        self.assertEqual(run.call_args.kwargs["timeout"], onboarding.SETUP_TIMEOUT)
        self.assertIn("PYTHONPATH", run.call_args.kwargs["env"])

    @patch.object(onboarding, "_setup_running", return_value=True)
    @patch.object(model_preparation, "prepare_model", return_value={"state": "error", "error": "disk full"})
    @patch.object(processes, "_run_cancelable")
    @patch.object(engine, "engine_status", side_effect=[{"state": "installing"}, {"state": "ready"}])
    def test_existing_setup_is_not_owned_and_download_failure_propagates(self, status, run, prepare, running):
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            onboarding.prepare_selection(self.cfg, Event())
        run.assert_not_called()

    @patch.object(processes, "_run_cancelable")
    @patch.object(model_preparation, "prepare_model")
    def test_cancellation_has_no_side_effects(self, prepare, run):
        event = Event()
        event.set()
        with self.assertRaises(engine.TranscriptionCancelled):
            onboarding.prepare_selection(self.cfg, event)
        run.assert_not_called()
        prepare.assert_not_called()

    @patch.object(onboarding, "_setup_running", return_value=False)
    @patch.object(model_preparation, "prepare_model", return_value={"state": "ready"})
    @patch.object(processes, "_run_cancelable", return_value=CompletedProcess([], 0, "", ""))
    @patch.object(engine, "engine_status", side_effect=[{"state": "installing"}, {"state": "ready"}])
    def test_stale_cancelled_setup_can_be_retried(self, status, run, prepare, running):
        onboarding.prepare_selection(self.cfg, Event())
        run.assert_called_once()
