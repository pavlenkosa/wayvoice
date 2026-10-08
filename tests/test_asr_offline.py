"""Offline enforcement shared by the warm worker and one-shot runner."""
import argparse
import contextlib
import gc
import io
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine, fw_runner, fw_worker, model_store


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.snapshot = self.root / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
        self.snapshot.mkdir(parents=True)
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
            (self.snapshot / name).write_bytes(b"valid fixture")
        self.audio = self.root / "audio.wav"
        self.audio.write_bytes(b"fixture")
        self.model = mock.Mock()
        self.model.transcribe.return_value = (iter([types.SimpleNamespace(text="hello")]), None)
        def construct(path, **kwargs):
            directory = Path(path)
            self.assertTrue((directory / "tokenizer.json").is_file())
            self.assertFalse((directory / "tokenizer.json").is_symlink())
            self.assertTrue((directory / "model.bin").is_symlink())
            return self.model
        self.constructor = mock.Mock(side_effect=construct)
        self.addCleanup(lambda: getattr(self.model, "_wayvoice_model_files", mock.Mock()).cleanup())
        patch = mock.patch.dict("sys.modules", {"faster_whisper": types.SimpleNamespace(WhisperModel=self.constructor)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_alias_is_resolved_to_local_snapshot_with_offline_flag(self):
        fw_worker.default_model_factory("small", "cpu", "int8")
        self.assertEqual(self.constructor.call_count, 1)
        self.assertEqual(self.constructor.call_args.kwargs, dict(device="cpu", compute_type="int8", local_files_only=True))
        self.assertEqual((Path(self.constructor.call_args.args[0]) / "model.bin").resolve(), self.snapshot / "model.bin")
        self.assertTrue(engine.model_is_present("small"))

    def test_missing_or_empty_required_files_refuse_before_backend(self):
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
            for empty in (False, True):
                with self.subTest(name=name, empty=empty):
                    path = self.snapshot / name
                    data = path.read_bytes()
                    path.write_bytes(b"") if empty else path.unlink()
                    try:
                        self.assertFalse(engine.model_is_present("small"))
                        with self.assertRaisesRegex(RuntimeError, "local|Local"):
                            fw_worker.default_model_factory("small", "cpu", "int8")
                        self.constructor.assert_not_called()
                    finally:
                        path.write_bytes(data)

    def test_local_directory_and_txt_vocabulary_work_without_hub(self):
        (self.snapshot / "vocabulary.json").rename(self.snapshot / "vocabulary.txt")
        fw_worker.default_model_factory(str(self.snapshot), "cpu", "int8")
        self.assertEqual((Path(self.constructor.call_args.args[0]) / "model.bin").resolve(), self.snapshot / "model.bin")

    def test_refs_main_and_mixed_case_repo_are_preserved(self):
        repo = self.root / "models--MyOrg--MyModel"
        chosen = repo / "snapshots" / "chosen"
        chosen.mkdir(parents=True)
        for file in self.snapshot.iterdir():
            (chosen / file.name).write_bytes(file.read_bytes())
        (repo / "snapshots" / "zzz-incomplete").mkdir()
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("chosen")
        fw_worker.default_model_factory("MyOrg/MyModel", "cpu", "int8")
        self.assertEqual((Path(self.constructor.call_args.args[0]) / "model.bin").resolve(), chosen / "model.bin")

    def test_warm_and_one_shot_refuse_incomplete_snapshot(self):
        (self.snapshot / "tokenizer.json").unlink()
        reply = fw_worker.handle_request({"cmd": "warm"}, fw_worker.ModelCache(), fw_worker.WorkerConfig(device="cpu"))
        self.assertFalse(reply["ok"])
        args = argparse.Namespace(model="small", device="cpu", beam_size=5, vad=False, audio=str(self.audio), language="auto")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(fw_runner._run_once(args), 1)
        self.assertIn("tokenizer.json", errors.getvalue())
        self.constructor.assert_not_called()

    def test_warm_and_one_shot_use_same_offline_factory(self):
        cache = fw_worker.ModelCache()
        reply = fw_worker.handle_request({"cmd": "warm"}, cache, fw_worker.WorkerConfig(device="cpu"))
        self.assertTrue(reply["ok"])
        args = argparse.Namespace(model="small", device="cpu", beam_size=5, vad=False, audio=str(self.audio), language="auto")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(fw_runner._run_once(args), 0)
        self.assertEqual(output.getvalue().strip(), "hello")
        self.assertEqual(self.constructor.call_count, 2)
        for call in self.constructor.call_args_list:
            self.assertTrue(call.kwargs["local_files_only"])

    def test_unknown_alias_and_safetensors_only_are_not_inference_models(self):
        with self.assertRaises(RuntimeError):
            fw_worker.default_model_factory("unknown-alias", "cpu", "int8")
        (self.snapshot / "model.bin").rename(self.snapshot / "model.safetensors")
        self.assertTrue(model_store.is_downloaded("small"))  # disk/delete semantics unchanged
        self.assertFalse(engine.model_is_present("small"))
        self.constructor.assert_not_called()

    def test_cache_tokenizer_deleted_during_constructor_cannot_trigger_fallback(self):
        def construct(path, **kwargs):
            (self.snapshot / "tokenizer.json").unlink()
            self.assertEqual((Path(path) / "tokenizer.json").read_bytes(), b"valid fixture")
            self.assertTrue(kwargs["local_files_only"])
            return self.model
        self.constructor.side_effect = construct
        fw_worker.default_model_factory("small", "cpu", "int8")

    def test_backend_failure_removes_private_view(self):
        paths = []
        def fail(path, **kwargs):
            paths.append(Path(path))
            raise RuntimeError("bad weights")
        self.constructor.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "bad weights"):
            fw_worker.default_model_factory("small", "cpu", "int8")
        self.assertFalse(paths[0].exists())

    def test_broken_tokenizer_symlink_refuses_before_constructor(self):
        path = self.snapshot / "tokenizer.json"
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(RuntimeError, "tokenizer.json"):
            fw_worker.default_model_factory("small", "cpu", "int8")
        self.constructor.assert_not_called()

    def test_private_view_lifetime_follows_loaded_model(self):
        class Model:
            pass
        self.constructor.side_effect = lambda *args, **kwargs: Model()
        model = fw_worker.default_model_factory("small", "cpu", "int8")
        directory = Path(self.constructor.call_args.args[0])
        self.assertTrue(directory.exists())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        del model
        gc.collect()
        self.assertFalse(directory.exists())

    def test_empty_tokenizer_copy_refuses_without_backend_and_cleans_view(self):
        destinations = []
        def truncate(source, destination):
            destinations.append(Path(destination).parent)
            Path(destination).write_bytes(b"")
        with mock.patch.object(fw_worker.shutil, "copyfile", side_effect=truncate):
            with self.assertRaisesRegex(RuntimeError, "tokenizer.json"):
                fw_worker.default_model_factory("small", "cpu", "int8")
        self.constructor.assert_not_called()
        self.assertFalse(destinations[0].exists())
