"""The engine registry is the one place that knows which engines exist.

Everything else - the daemon, the CLI, the settings window - asks the registry instead
of comparing engine ids, so these tests guard the registry itself: every registered
engine reports a usable status, the ids and labels are unique, no two engines claim the
same config key, the flags match what the settings window does with them, and an id
nobody claims fails loudly instead of falling back to another engine.

No downloaded model and no network. The settings-window tests need a display and step
aside without one.
"""

import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from wayvoice import engine
from wayvoice.config import DEFAULTS
from wayvoice.engine import (
    DEFAULT_ENGINE,
    ENGINES,
    Engine,
    TranscriptionCancelled,
    _transcribe_custom,
    _transcribe_whisper_cpp,
    engine_from_config,
    engine_ids,
    engine_label,
    engine_status,
    get_engine,
    request_engine_setup,
    transcribe,
)

STATES = {"ready", "missing", "installing", "error"}

_APPLICATION = None


def _gtk_available() -> bool:
    """Whether this process can really open a GTK window.

    Not ``Gtk.init_check()``: on a machine with the bindings but no display - a build
    server, a plain ssh session - that still answers true, the tests go on to build widgets,
    and GTK takes the process down. A segfault is worse than a failure, because it takes the
    other four hundred tests with it.
    """
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk

        return bool(Gtk.init_check())
    except Exception:
        return False


def _test_application():
    """One application for the whole run.

    Registering emits ``GApplication::startup``, which GTK wants to have seen before a
    window is created, and a second registration of the same id in one process is refused.
    """
    from gi.repository import Adw

    global _APPLICATION
    if _APPLICATION is None:
        _APPLICATION = Adw.Application(application_id="io.github.stepan.WayVoice.Test")
        _APPLICATION.register()
    return _APPLICATION


@contextmanager
def _registered(engine: Engine):
    """Add an engine to the registry for the duration of a test."""
    ENGINES[engine.id] = engine
    try:
        yield engine
    finally:
        ENGINES.pop(engine.id, None)


class RegistryShapeTests(unittest.TestCase):
    def test_every_shipped_engine_is_registered(self):
        # The three engines engine.py can actually run. A fourth one added later must be
        # added here on purpose, not by accident.
        self.assertEqual(engine_ids(), ["faster-whisper", "whisper-cpp", "custom"])

    def test_ids_and_labels_are_unique(self):
        ids = engine_ids()
        labels = [engine_label(engine_id) for engine_id in ids]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len(set(labels)), len(labels))
        self.assertEqual(ids, list(ENGINES))

    def test_every_engine_provides_what_the_code_calls(self):
        for engine_id in engine_ids():
            engine = get_engine(engine_id)
            self.assertIsNotNone(engine)
            self.assertEqual(engine.id, engine_id)
            self.assertTrue(engine.label)
            self.assertTrue(callable(engine.transcribe), engine_id)
            self.assertTrue(callable(engine.status), engine_id)
            self.assertIsInstance(engine.uses_models, bool)
            self.assertIsInstance(engine.needs_setup, bool)
            self.assertIsInstance(engine.settings, tuple)
            self.assertTrue(engine.settings, engine_id)

    def test_needs_setup_matches_the_setup_hook(self):
        # A button that promises to prepare something must have something to
        # call; otherwise the registry advertises work that never happens.
        for engine_id in engine_ids():
            engine = get_engine(engine_id)
            self.assertEqual(engine.needs_setup, engine.setup is not None, engine_id)

    def test_engine_specific_keys_are_claimed_by_one_engine(self):
        # A key every engine reads (language, the timeouts) belongs to none of
        # them; the registry only claims what an engine alone owns.
        owner: dict[str, str] = {}
        for engine_id in engine_ids():
            for key in get_engine(engine_id).settings:
                self.assertIn(key, DEFAULTS, f"{engine_id}.{key} is not a config key")
                self.assertNotIn(key, owner, f"{key} is claimed by {owner.get(key)} and {engine_id}")
                owner[key] = engine_id
        for key in ("language", "transcription_timeout_sec", "max_recording_sec"):
            self.assertNotIn(key, owner)

    def test_flags_match_what_the_ui_shows(self):
        flags = {engine_id: get_engine(engine_id) for engine_id in engine_ids()}
        self.assertTrue(flags["faster-whisper"].uses_models)
        self.assertTrue(flags["faster-whisper"].needs_setup)
        self.assertFalse(flags["whisper-cpp"].uses_models)
        self.assertFalse(flags["whisper-cpp"].needs_setup)
        # The external command has no model list and nothing to prepare either.
        self.assertFalse(flags["custom"].uses_models)
        self.assertFalse(flags["custom"].needs_setup)
        # Only the engine with a model list owns the model picker.
        self.assertIn("model", flags["faster-whisper"].settings)
        self.assertNotIn("model", flags["whisper-cpp"].settings)
        self.assertNotIn("model", flags["custom"].settings)

    def test_default_engine_is_the_shipped_one(self):
        # The default lives in three places (config.DEFAULTS, the registry and
        # engine.DEFAULT_ENGINE); a new engine must not leave them disagreeing.
        self.assertEqual(DEFAULTS["engine"], DEFAULT_ENGINE)
        self.assertEqual(engine_ids()[0], DEFAULT_ENGINE)
        self.assertIsNotNone(get_engine(DEFAULTS["engine"]))

    def test_unknown_engine_has_no_entry(self):
        self.assertIsNone(get_engine("klingon"))
        self.assertIsNone(get_engine(""))
        self.assertIsNone(get_engine(None))

    def test_engine_label_falls_back_to_the_id(self):
        self.assertEqual(engine_label("whisper-cpp"), "whisper.cpp")
        self.assertEqual(engine_label("klingon"), "klingon")
        self.assertEqual(engine_label(None), "")

    def test_engine_from_config_follows_the_config(self):
        self.assertEqual(engine_from_config({}).id, "faster-whisper")
        self.assertEqual(engine_from_config({"engine": "custom"}).id, "custom")
        self.assertIsNone(engine_from_config({"engine": "klingon"}))
        self.assertIsNone(engine_from_config({"engine": ""}))

    def test_request_engine_setup_reports_when_there_is_nothing_to_do(self):
        with mock.patch("wayvoice.engine.service.request_engine_setup") as request:
            self.assertTrue(request_engine_setup(get_engine("faster-whisper")))
            self.assertTrue(request.called)
        self.assertFalse(request_engine_setup(get_engine("whisper-cpp")))
        self.assertFalse(request_engine_setup(get_engine("custom")))
        self.assertFalse(request_engine_setup(None))


class EngineStatusTests(unittest.TestCase):
    def test_every_registered_engine_reports_id_label_and_state(self):
        for engine_id in engine_ids():
            with self.subTest(engine=engine_id):
                status = engine_status({"engine": engine_id})
                self.assertEqual(status["id"], engine_id)
                self.assertEqual(status["label"], engine_label(engine_id))
                self.assertIn(status["state"], STATES)
                self.assertTrue(status["message"])

    def test_default_config_reports_its_own_engine(self):
        status = engine_status(dict(DEFAULTS))
        self.assertEqual(status["id"], DEFAULTS["engine"])
        self.assertEqual(status["label"], "Faster-Whisper")
        self.assertIn(status["state"], STATES)
        self.assertNotEqual(status["state"], "error")

    def test_missing_engine_key_means_the_default_engine(self):
        self.assertEqual(engine_status({})["id"], engine_status(dict(DEFAULTS))["id"])

    def test_unknown_engine_is_an_error(self):
        status = engine_status({"engine": "klingon"})
        self.assertEqual(status["id"], "klingon")
        self.assertEqual(status["label"], "klingon")
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["message"], "Unknown recognition engine")

    def test_whisper_cpp_without_a_binary_is_missing(self):
        # An explicit path that is not there is answered as missing instead of
        # silently falling back to a whisper-cli somewhere on PATH.
        status = engine_status({
            "engine": "whisper-cpp",
            "whisper_cpp_binary": "/nonexistent/whisper-cli",
        })
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["message"], "whisper-cli was not found")

    def test_whisper_cpp_with_binary_and_model_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.gguf"
            model.write_bytes(b"gguf")
            status = engine_status({
                "engine": "whisper-cpp",
                "whisper_cpp_binary": "/bin/sh",
                "whisper_cpp_model": str(model),
            })
        self.assertEqual(status["state"], "ready")
        self.assertIn("sh", status["message"])

    def test_whisper_cpp_without_a_model_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = engine_status({
                "engine": "whisper-cpp",
                "whisper_cpp_binary": "/bin/sh",
                "whisper_cpp_model": str(Path(tmp) / "absent.gguf"),
            })
        self.assertEqual(status["state"], "missing")
        self.assertIn("model", status["message"].lower())


class WhisperCppThreadTests(unittest.TestCase):
    """The engine has to tell whisper.cpp how many threads it may use.

    whisper.cpp defaults to four. On a sixteen-thread laptop the same medium model
    took 21.8 s for twelve seconds of speech with that default and 12.5 s with
    ``-t 16`` - measured, same machine, same audio, nothing else changed.
    """

    def _transcribed(self, cfg_extra=None):
        cfg = {
            "engine": "whisper-cpp",
            "whisper_cpp_binary": "/bin/echo",
            "whisper_cpp_model": "/bin/sh",
            "whisper_cpp_gpu": False,
        }
        cfg.update(cfg_extra or {})
        seen: list[list[str]] = []

        def record(args, **kwargs):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, "текст", "")

        with mock.patch.object(engine, "_find_whisper_cpp", return_value="/bin/echo"), \
             mock.patch.object(engine, "_run_cancelable", side_effect=record):
            text = _transcribe_whisper_cpp(Path("/nonexistent.wav"), cfg, None)
        self.assertEqual(text, "текст")
        return seen[0]

    def test_the_thread_count_is_passed_explicitly(self):
        args = self._transcribed()
        self.assertIn("-t", args, "whisper.cpp would fall back to its default of four")
        threads = args[args.index("-t") + 1]
        self.assertEqual(threads, str(engine._thread_count()))

    def test_the_thread_count_is_the_number_of_cpus(self):
        with mock.patch.object(engine.os, "cpu_count", return_value=16):
            self.assertEqual(engine._thread_count(), 16)

    def test_a_missing_cpu_count_still_gives_a_usable_number(self):
        for reported in (None, 0, -3):
            with mock.patch.object(engine.os, "cpu_count", return_value=reported):
                self.assertEqual(engine._thread_count(), 1)
        with mock.patch.object(engine.os, "cpu_count", return_value="many"):
            self.assertEqual(engine._thread_count(), 1)

    def test_an_absurd_cpu_count_is_bounded(self):
        # A container reporting hundreds of CPUs is not a laptop, and the number
        # goes straight to the program.
        with mock.patch.object(engine.os, "cpu_count", return_value=4096):
            self.assertEqual(engine._thread_count(), 64)

    def test_gpu_is_still_switched_off_when_asked(self):
        args = self._transcribed({"whisper_cpp_gpu": False})
        self.assertIn("-ng", args)


class CustomEngineTests(unittest.TestCase):
    """The external-command engine is only usable if it really runs."""

    def test_status_without_a_command_is_missing(self):
        status = engine_status({"engine": "custom", "custom_command": "   "})
        self.assertEqual(status["state"], "missing")
        self.assertIn("stdout", status["message"])

    def test_status_with_a_command_is_ready(self):
        status = engine_status({"engine": "custom", "custom_command": "cat {audio}"})
        self.assertEqual(status["state"], "ready")

    def test_transcribe_without_a_command_fails(self):
        with self.assertRaises(RuntimeError):
            transcribe(Path("/nonexistent.wav"), {"engine": "custom"})

    def test_audio_placeholder_is_substituted(self):
        # shlex.quote is what protects the space, so the template must leave the
        # placeholder unquoted for it to do its job.
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take 1.wav"
            audio.write_bytes(b"RIFF")
            rendered = _transcribe_custom(
                audio,
                {"engine": "custom", "custom_command": 'printf "%s|" {audio}'},
                None,
            )
        self.assertEqual(rendered, f"{audio}|")

    def test_transcribe_reads_the_text_the_command_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take.wav"
            audio.write_text("привет мир", encoding="utf-8")
            text = transcribe(
                audio,
                {
                    "engine": "custom",
                    "custom_command": "cat {audio}",
                    # Keep the engine's raw words, so the test only measures the
                    # command and not the punctuation pass.
                    "auto_punctuation": False,
                    "append_space": False,
                },
            )
        self.assertEqual(text, "привет мир")

    def test_failing_command_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take.wav"
            audio.write_bytes(b"RIFF")
            with self.assertRaises(RuntimeError):
                transcribe(
                    audio,
                    {
                        "engine": "custom",
                        "custom_command": "echo boom >&2; exit 3",
                        "auto_punctuation": False,
                    },
                )

    def test_cancellation_stops_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take.wav"
            audio.write_bytes(b"RIFF")
            event = threading.Event()
            timer = threading.Timer(0.15, event.set)
            timer.start()
            try:
                with self.assertRaises(TranscriptionCancelled):
                    _transcribe_custom(
                        audio,
                        {"engine": "custom", "custom_command": "sleep 5"},
                        event,
                    )
            finally:
                timer.cancel()


class TranscribeDispatchTests(unittest.TestCase):
    def test_transcribe_goes_through_the_registry(self):
        seen: list[tuple] = []

        def _fake(audio, cfg, cancel_event):
            seen.append((audio, cfg["engine"], cancel_event))
            return "raw words"

        engine = Engine(
            id="test-double",
            label="Test double",
            transcribe=_fake,
            status=lambda cfg: {"state": "ready", "message": "Ready"},
            uses_models=False,
            needs_setup=False,
            settings=("test_only_key",),
        )
        with _registered(engine):
            self.assertEqual(engine_from_config({"engine": "test-double"}), engine)
            self.assertEqual(engine_status({"engine": "test-double"})["label"], "Test double")
            text = transcribe(
                Path("/nonexistent.wav"),
                {"engine": "test-double", "append_space": False},
            )
        # The engine returned the raw words; the punctuation pass that every
        # engine shares still runs on top of it, here.
        self.assertEqual(text, "Raw words.")
        self.assertEqual(seen, [(Path("/nonexistent.wav"), "test-double", None)])

    def test_unknown_engine_raises(self):
        with self.assertRaises(RuntimeError):
            transcribe(Path("/nonexistent.wav"), {"engine": "klingon"})

    def test_engine_errors_are_not_swallowed(self):
        def _boom(audio, cfg, cancel_event):
            raise RuntimeError("engine said no")

        engine = Engine(
            id="test-failing",
            label="Failing",
            transcribe=_boom,
            status=lambda cfg: {"state": "error", "message": "engine said no"},
            uses_models=False,
            needs_setup=False,
            settings=(),
        )
        with _registered(engine):
            with self.assertRaisesRegex(RuntimeError, "engine said no"):
                transcribe(Path("/nonexistent.wav"), {"engine": "test-failing"})


@unittest.skipUnless(_gtk_available(), "no GTK display available")
class SettingsWindowRegistryTests(unittest.TestCase):
    """The settings window must show what the registry says, not its own list."""

    def setUp(self):
        import gi

        gi.require_version("Adw", "1")
        from gi.repository import Adw

        from wayvoice import ui

        Adw.init()
        self.ui = ui
        self._tmp = tempfile.TemporaryDirectory()
        # An isolated config home: the window reads config.json on construction.
        self._env = mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": self._tmp.name, "XDG_DATA_HOME": self._tmp.name},
        )
        self._env.start()
        self.window = self._build_window()

    def tearDown(self):
        self.window.destroy()
        self._env.stop()
        self._tmp.cleanup()

    def _build_window(self):
        app = _test_application()
        # The window arms timers and an idle callback on construction; they never fire
        # without a main loop, but replacing them keeps a run from touching the real
        # daemon or the real user's config.
        with (
            mock.patch.object(self.ui.WayVoiceWindow, "_background_start", lambda self: 0),
            mock.patch.object(self.ui.WayVoiceWindow, "_poll_status", lambda self: 0),
            mock.patch.object(self.ui.WayVoiceWindow, "_poll_engine_settings", lambda self: 0),
        ):
            return self.ui.WayVoiceWindow(app)

    def _combo_labels(self) -> list[str]:
        model = self.window.engine.get_model()
        return [model.get_string(i) for i in range(model.get_n_items())]

    def test_engine_list_is_the_registry(self):
        ids, labels = self.ui.engine_choices()
        self.assertEqual(ids, engine_ids())
        self.assertEqual(labels, [engine_label(engine_id) for engine_id in ids])
        self.assertEqual(self._combo_labels(), labels)
        self.assertEqual(len(self._combo_labels()), len(ENGINES))

    def test_selecting_an_index_selects_that_engine(self):
        for index, engine_id in enumerate(engine_ids()):
            with self.subTest(engine=engine_id):
                self.window.engine.set_selected(index)
                self.assertEqual(self.window._selected_engine(), engine_id)
                self.assertEqual(self.window._selected_engine_object(), get_engine(engine_id))

    def test_rows_follow_the_registry_settings(self):
        rows = dict(self.window._engine_rows)
        for index, engine_id in enumerate(engine_ids()):
            with self.subTest(engine=engine_id):
                self.window.engine.set_selected(index)
                self.window._update_engine_visibility()
                owned = set(get_engine(engine_id).settings)
                for key, row in rows.items():
                    self.assertEqual(
                        row.get_visible(),
                        key in owned,
                        f"{engine_id}: row {key}",
                    )
                self.assertEqual(
                    self.window.engine_setup_btn.get_visible(),
                    get_engine(engine_id).needs_setup,
                    engine_id,
                )

    def test_the_external_command_has_a_row_of_its_own(self):
        rows = dict(self.window._engine_rows)
        self.assertIn("custom_command", rows)
        self.window.engine.set_selected(engine_ids().index("custom"))
        self.window._update_engine_visibility()
        self.assertTrue(rows["custom_command"].get_visible())
        self.assertFalse(self.window.engine_setup_btn.get_visible())
        # The model list belongs to faster-whisper only.
        self.assertFalse(rows["model"].get_visible())

    def test_custom_command_row_is_filled_and_explained(self):
        rows = dict(self.window._engine_rows)
        row = rows["custom_command"]
        hint = row.get_tooltip_text() or ""
        self.assertTrue(hint)
        # The placeholder is the whole interface of the command template, so it
        # has to be documented where the template is typed.
        self.assertIn("{audio}", hint)

    def test_engine_card_shows_the_registry_label(self):
        self.window._update_cards()
        self.assertEqual(self.window.engine_card.get_text(), engine_label(DEFAULTS["engine"]))


class DaemonSetupTests(unittest.TestCase):
    """The daemon prepares the engine that is selected, or says there is none."""

    def setUp(self):
        from wayvoice import daemon

        self.daemon = daemon

    def _dispatch(self, cfg):
        with (
            mock.patch.object(self.daemon, "load_config", return_value=cfg),
            mock.patch("wayvoice.engine.service.request_engine_setup") as request,
        ):
            reply = self.daemon.WayVoiceDaemon.dispatch(
                self.daemon.WayVoiceDaemon.__new__(self.daemon.WayVoiceDaemon),
                "engine-setup",
            )
        return reply, request

    def test_engine_setup_prepares_faster_whisper(self):
        reply, request = self._dispatch({"engine": "faster-whisper"})
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(request.called)

    def test_engine_setup_is_quiet_when_nothing_to_prepare(self):
        reply, request = self._dispatch({"engine": "whisper-cpp"})
        self.assertFalse(reply["ok"])
        self.assertIn("whisper.cpp", reply["error"])
        self.assertFalse(request.called)

    def test_engine_setup_reports_an_unknown_engine(self):
        reply, request = self._dispatch({"engine": "klingon"})
        self.assertFalse(reply["ok"])
        self.assertIn("klingon", reply["error"])
        self.assertFalse(request.called)

    def test_engine_setup_of_a_broken_config_still_prepares(self):
        # The default engine is prepared even when the key is missing entirely.
        reply, request = self._dispatch({})
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(request.called)


class CliSetupTests(unittest.TestCase):
    """``wayvoice engine-setup`` asks the registry, not the runtime directly.

    It used to prepare Faster-Whisper whatever the config said, so asking it to set up
    whisper.cpp installed a Python runtime nothing would ever use.
    """

    def _run(self, cfg, prepared):
        from wayvoice import cli

        argv = ["wayvoice", "engine-setup"]
        with (
            mock.patch.object(cli, "load_config", return_value=cfg),
            mock.patch.object(cli, "request_engine_setup", return_value=prepared) as request,
            mock.patch.object(sys, "argv", argv),
            mock.patch("sys.stderr", io.StringIO()),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        return code, request

    def test_prepares_the_selected_engine(self):
        code, request = self._run({"engine": "faster-whisper"}, True)
        self.assertEqual(code, 0)
        self.assertTrue(request.called)

    def test_refuses_for_an_engine_without_setup(self):
        code, request = self._run({"engine": "whisper-cpp"}, False)
        self.assertEqual(code, 1)
        self.assertTrue(request.called)

    def test_reports_an_unknown_engine_instead_of_preparing(self):
        code, request = self._run({"engine": "klingon"}, True)
        self.assertEqual(code, 1)
        self.assertFalse(request.called)


if __name__ == "__main__":
    unittest.main()
