"""The model store has to describe the cache the way it really is.

Every test here builds a fake hub in a temporary directory that reproduces the
shape of ``~/.cache/huggingface/hub`` on this machine:

* ``models--<org>--<name>/`` with ``refs/main``, ``snapshots/<rev>/`` and a
  private ``blobs/``;
* snapshot entries are **symlinks**, and some of those symlinks point at the
  shared ``<hub>/blobs/<2 hex>/<hash>`` store (the Xet layout) -- so
  ``du``/``lstat`` of the model directory says 2.6 MB while the weights are
  483 MB;
* ``tokenizer.json`` and ``vocabulary.txt`` are **one and the same blob** shared
  by two models, which is what makes a naive delete break the other one;
* a repository of another application lives in the same hub and must survive;
* a directory left by an interrupted download has no snapshot at all.

No network, no huggingface_hub, no real cache: ``HF_HUB_CACHE`` and the legacy
variables are pointed at the temporary directory for the duration of each test.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wayvoice import engine, model_store

WEIGHT = 483  # bytes, stands in for the 483 MB real weight file
TOKENIZER = 40
VOCABULARY = 20
CONFIG = 12
FOREIGN_WEIGHT = 300

SMALL_REV = "aaaa1111"
MEDIUM_REV = "bbbb2222"
FOREIGN_REV = "cccc3333"


class FakeHub:
    """A stand-in for the hub cache, with the traps of the real one."""

    def __init__(self, root: Path):
        self.root = root
        self.hub = root / "hub"
        self.hub.mkdir(parents=True)
        # Small: weights in the SHARED blob store (Xet), tokenizer/vocabulary in
        # its own private blobs -- and those two are shared with medium.
        self._repo("Systran", "faster-whisper-small", SMALL_REV, {
            "model.bin": ("shared", "WEIGHT_SMALL", WEIGHT),
            "config.json": ("private", "CONFIG_SMALL", CONFIG),
            # Both of these are the SAME blob as in medium.
            "tokenizer.json": ("shared", "TOKENIZER", TOKENIZER),
            "vocabulary.txt": ("shared", "VOCABULARY", VOCABULARY),
        })
        # Medium: weights private, the SAME tokenizer and vocabulary blobs.
        self._repo("Systran", "faster-whisper-medium", MEDIUM_REV, {
            "model.bin": ("private", "WEIGHT_MEDIUM", 700),
            "config.json": ("private", "CONFIG_MEDIUM", 11),
            "tokenizer.json": ("shared", "TOKENIZER", TOKENIZER),
            "vocabulary.txt": ("shared", "VOCABULARY", VOCABULARY),
        })
        # Another application sharing the very same hub.
        self._repo("handy-computer", "gigaam-v3-e2e-ctc-gguf", FOREIGN_REV, {
            "model.gguf": ("private", "FOREIGN_WEIGHT", FOREIGN_WEIGHT),
        })
        # A directory left behind by an interrupted download: refs, no snapshot.
        self.orphan = self.hub / "models--handy-computer--nemotron-0.6b-gguf"
        (self.orphan / "blobs").mkdir(parents=True)
        (self.orphan / "blobs" / "PART").write_bytes(b"x" * 500)
        (self.orphan / "refs").mkdir()
        (self.orphan / "refs" / "main").write_text("dddd4444", encoding="utf-8")

    def _repo(self, org: str, name: str, rev: str, files: dict) -> Path:
        """Lay out one cached repository.

        Every snapshot entry is a symlink to ``../../blobs/<name>``, and for the
        "shared" kinds that ``blobs/<name>`` is itself a symlink into
        ``<hub>/blobs/<2 hex>/<name>`` -- the two hops the real cache has.
        """
        folder = self.hub / f"models--{org}--{name}"
        (folder / "refs").mkdir(parents=True)
        (folder / "refs" / "main").write_text(rev, encoding="utf-8")
        (folder / "blobs").mkdir()
        snapshot = folder / "snapshots" / rev
        snapshot.mkdir(parents=True)
        for filename, (kind, key, size) in files.items():
            entry = folder / "blobs" / key
            if kind == "private":
                entry.write_bytes(b"\0" * size)
            else:
                shared = self.shared_blob(key, size)
                entry.symlink_to(Path("../../blobs/42") / shared.name)
            (snapshot / filename).symlink_to(Path("../../blobs") / key)
        return folder

    def shared_blob(self, key: str, size: int) -> Path:
        """One blob in the shared store, created once and reused after that."""
        existing = self.hub / "blobs" / "42" / key
        if existing.exists():
            return existing
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"\1" * size)
        # The hub keeps a lock file and a refcount file next to a shared blob.
        (existing.parent / f"{key}.lock").write_bytes(b"")
        (existing.parent / f"{key}.refs").write_bytes(b"2")
        return existing


class ModelStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.fake = FakeHub(self.base)
        # Point every hub-resolution variable at the fake cache; the tests must
        # never look at the user's real one.
        for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
            self.addCleanup(self._unset_env, name)
            os.environ[name] = str(self.base / ("hf" if name == "HF_HOME" else "xdg"))
        os.environ["HF_HUB_CACHE"] = str(self.fake.hub)

    def _unset_env(self, name):
        os.environ.pop(name, None)

    def size(self, model_id):
        return model_store.size_of(model_id)

    # ---- the blob that lives inside somebody else's model ---------------
    #
    # The fake hub above keeps every shared blob in the shared store, which is
    # the friendly case.  The cache on a real machine has the other one too, and
    # it is the dangerous one: a blob can sit in the private ``blobs/`` of
    # whichever repository fetched it first, with the other model's snapshot
    # symlinking straight into that private directory.  Deleting the first model
    # with ``rmtree`` then takes a file the second model still needs.
    # ``_borrow_shared_private_blob`` builds exactly that.
    def _borrow_shared_private_blob(self):
        """Make small's private config.json the file medium points at too."""
        small = self.fake.hub / "models--Systran--faster-whisper-small"
        medium = self.fake.hub / "models--Systran--faster-whisper-medium"
        private = small / "blobs" / "BORROWED"
        private.write_bytes(b"\0" * 77)
        # medium's snapshot now reaches into small's private blobs directory.
        (medium / "snapshots" / MEDIUM_REV / "extra.json").unlink(missing_ok=True)
        (medium / "snapshots" / MEDIUM_REV / "extra.json").symlink_to(
            Path("../../..") / "models--Systran--faster-whisper-small" / "blobs" / "BORROWED"
        )
        return private

    def test_shared_blob_inside_the_deleted_directory_survives(self):
        private = self._borrow_shared_private_blob()
        medium_snapshot = self.fake.hub / "models--Systran--faster-whisper-medium" / "snapshots" / MEDIUM_REV
        before = self.size("small")
        result = model_store.delete("small")
        self.assertTrue(result["ok"])
        # medium's link must resolve, or medium is broken from now on.
        borrowed = medium_snapshot / "extra.json"
        self.assertTrue(borrowed.exists(), "dangling symlink left in medium")
        self.assertTrue(private.is_file(), "a blob another model uses was deleted")
        self.assertEqual(borrowed.read_bytes(), b"\0" * 77)
        # The bytes that survived must not be reported as freed space: the borrowed
        # blob is 77 bytes that are still on disk afterwards.
        self.assertEqual(result["kept_bytes"], 77)
        # Everything small held, except the two blobs it shares with medium --
        # those live in the shared store and are none of this deletion's
        # business -- plus the one-byte ``.refs`` sidecar of the freed weight.
        self.assertEqual(result["freed_bytes"], before - TOKENIZER - VOCABULARY + 1)

    def test_the_model_is_gone_even_when_a_blob_had_to_stay(self):
        self._borrow_shared_private_blob()
        model_store.delete("small")
        self.assertFalse(model_store.is_downloaded("small"))
        self.assertEqual(model_store.size_of("small"), 0)
        self.assertTrue(model_store.is_downloaded("medium"))
        # A later download of small reuses what stayed instead of refetching it.
        self.assertIn("small", {row["id"] for row in model_store.inventory()})

    def test_unused_private_blobs_still_go_with_the_directory(self):
        private = self._borrow_shared_private_blob()
        unreferenced = self.fake.hub / "models--Systran--faster-whisper-small" / "blobs" / "ONLY_SMALL"
        unreferenced.write_bytes(b"\0" * 33)
        result = model_store.delete("small")
        self.assertTrue(private.is_file())
        self.assertFalse(unreferenced.exists(), "a blob only this model used must be freed")
        self.assertEqual(result["kept_bytes"], 77)
        self.assertGreaterEqual(result["freed_bytes"], 33)

    # ---- sizes ----------------------------------------------------------
    def test_size_follows_the_symlinks_to_the_real_bytes(self):
        # The weights live in a shared blob reached through two symlinks; the
        # model directory itself would measure a few kilobytes.
        self.assertEqual(self.size("small"), WEIGHT + CONFIG + TOKENIZER + VOCABULARY)
        du_like = sum(
            p.stat().st_size
            for p in (self.fake.hub / "models--Systran--faster-whisper-small" / "snapshots" / SMALL_REV).iterdir()
            if p.is_file() and not p.is_symlink()
        )
        self.assertLess(du_like, WEIGHT, "the trap this guards against is a size read off the symlinks")

    def test_shared_blob_is_not_counted_twice(self):
        # small and medium share tokenizer.json and vocabulary.txt as one file.
        # A size that added them per reference would exceed the real total.
        self.assertEqual(self.size("small"), WEIGHT + CONFIG + TOKENIZER + VOCABULARY)
        self.assertEqual(self.size("medium"), 700 + 11 + TOKENIZER + VOCABULARY)
        self.assertLess(self.size("small") + self.size("medium"), WEIGHT + 700 + 2 * CONFIG + 2 * TOKENIZER + 2 * VOCABULARY + 11)

    def test_total_size_counts_the_shared_blob_once(self):
        # Only catalogue models count, and the two shared blobs count once even
        # though two models reference them.
        self.assertEqual(
            model_store.total_size(),
            WEIGHT + 700 + CONFIG + 11 + TOKENIZER + VOCABULARY,
        )
        # Adding another model's size would have to add zero here: the tokenizer
        # is the blob both of them share.
        self.assertLess(model_store.total_size(), self.size("small") + self.size("medium"))

    # ---- downloaded -----------------------------------------------------
    def test_is_downloaded_needs_a_weight_not_just_a_directory(self):
        self.assertTrue(model_store.is_downloaded("small"))
        self.assertTrue(model_store.is_downloaded("medium"))
        # Neither the orphan nor an unknown model is a downloaded model.
        self.assertFalse(model_store.is_downloaded("handy-computer/nemotron-0.6b-gguf"))
        self.assertFalse(model_store.is_downloaded("large-v3"))

    def test_snapshot_dir_follows_the_ref(self):
        self.assertEqual(
            model_store.snapshot_dir("small"),
            self.fake.hub / "models--Systran--faster-whisper-small" / "snapshots" / SMALL_REV,
        )
        self.assertIsNone(model_store.snapshot_dir("handy-computer/nemotron-0.6b-gguf"))

    # ---- delete ---------------------------------------------------------
    def test_delete_frees_the_weight_and_keeps_the_shared_blob(self):
        # small's weight is its own shared blob; the tokenizer is also medium's.
        result = model_store.delete("small")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["freed_bytes"], WEIGHT)
        self.assertFalse((self.fake.hub / "models--Systran--faster-whisper-small").exists())
        # The shared tokenizer/vocabulary blobs survived, sidecars included.
        for name in ("TOKENIZER", "VOCABULARY"):
            blob = self.fake.hub / "blobs" / "42" / name
            self.assertTrue(blob.exists(), f"shared blob {name} must not be deleted")
            self.assertTrue(blob.with_name(name + ".lock").exists())

    def test_no_dangling_symlinks_in_the_surviving_model(self):
        model_store.delete("small")
        snapshot = self.fake.hub / "models--Systran--faster-whisper-medium" / "snapshots" / MEDIUM_REV
        entries = list(snapshot.iterdir())
        self.assertTrue(entries, "medium must still have its files")
        for entry in entries:
            self.assertTrue(entry.exists(), f"dangling symlink left behind: {entry.name}")

    def test_medium_still_loads_its_weights_after_small_is_deleted(self):
        model_store.delete("small")
        self.assertTrue(model_store.is_downloaded("medium"))
        self.assertEqual(self.size("medium"), 700 + 11 + TOKENIZER + VOCABULARY)

    def test_delete_does_not_touch_another_applications_model(self):
        folder = self.fake.hub / "models--handy-computer--gigaam-v3-e2e-ctc-gguf"
        foreign = folder / "blobs" / "FOREIGN_WEIGHT"
        before = foreign.read_bytes()
        model_store.delete("small")
        model_store.delete("medium")
        self.assertTrue(folder.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(foreign.read_bytes(), before)

    def test_delete_of_a_foreign_model_is_refused(self):
        # Even a repository that looks perfectly normal is not ours to remove:
        # WayVoice cannot tell who put it there.
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("handy-computer/gigaam-v3-e2e-ctc-gguf")
        self.assertTrue((self.fake.hub / "models--handy-computer--gigaam-v3-e2e-ctc-gguf").exists())

    def test_delete_removes_private_blobs_of_the_deleted_model(self):
        # medium's weight is private to it, so deleting medium frees it.
        result = model_store.delete("medium")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["freed_bytes"], 700)
        self.assertFalse((self.fake.hub / "models--Systran--faster-whisper-medium").exists())
        # small is untouched: it does not use medium's private blobs.
        self.assertTrue(model_store.is_downloaded("small"))

    def test_delete_is_idempotent(self):
        model_store.delete("small")
        again = model_store.delete("small")
        self.assertTrue(again["ok"])
        self.assertEqual(again["freed_bytes"], 0)

    def test_delete_does_not_leave_a_shared_blob_for_a_later_model(self):
        # Delete small, then medium: the shared tokenizer has no referrer left
        # and must now go too, or the cache leaks forever.
        model_store.delete("small")
        model_store.delete("medium")
        for name in ("TOKENIZER", "VOCABULARY"):
            blob = self.fake.hub / "blobs" / "42" / name
            self.assertFalse(blob.exists(), f"blob {name} has no referrer any more and must be freed")
            self.assertFalse(blob.with_name(name + ".lock").exists())
            self.assertFalse(blob.with_name(name + ".refs").exists())

    # ---- refusals -------------------------------------------------------
    def test_refuse_local_path(self):
        local = self.base / "my-model"
        local.mkdir()
        (local / "model.bin").write_bytes(b"\0" * 5)
        with self.assertRaises(model_store.RefusedError):
            model_store.delete(str(local))
        self.assertTrue((local / "model.bin").exists())

    def test_refuse_relative_local_path(self):
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("./tiny")

    def test_refuse_id_outside_the_catalogue(self):
        # Not a catalogue entry and not a path: refuse rather than guess.
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("some/other-model")

    def test_refuse_missing_model(self):
        # A catalogue model that was never downloaded: by default a harmless
        # no-op, and a refusal for a caller that insists it must be there.
        result = model_store.delete("large-v3")
        self.assertTrue(result["ok"])
        self.assertEqual(result["freed_bytes"], 0)
        self.assertEqual(result["message_key"], "store.nothing_to_delete")
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("large-v3", must_exist=True)

    def test_refuse_symlinked_model_dir(self):
        # A repository directory that is itself a symlink is not safe to
        # rmtree: refuse instead of following it somewhere else.
        target = self.fake.hub / "models--Systran--faster-whisper-large-v3"
        outside = self.base / "elsewhere"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("large-v3")
        self.assertTrue(outside.exists(), "a symlinked model dir must not delete its target")

    def test_refuse_path_that_escapes_the_hub(self):
        # An id that resolves outside the hub root must be refused even if the
        # folder "exists".
        outside = self.base / "outside-cache"
        repo = outside / "models--org--name"
        (repo / "snapshots" / "rev").mkdir(parents=True)
        (repo / "snapshots" / "rev" / "model.bin").write_bytes(b"\0" * 8)
        with self.assertRaises(model_store.RefusedError):
            model_store.delete("org/name", root=outside)

    def test_refuse_empty_and_unknown(self):
        for bad in ("", None, "  "):
            with self.assertRaises(model_store.RefusedError):
                model_store.delete(bad)

    # ---- local models ---------------------------------------------------
    def test_local_model_reports_a_real_size_and_is_not_downloaded(self):
        local = self.base / "custom-local"
        (local / "sub").mkdir(parents=True)
        (local / "model.bin").write_bytes(b"\0" * 50)
        (local / "sub" / "extra.bin").write_bytes(b"\0" * 20)
        self.assertEqual(self.size(str(local)), 70)
        # A local model is "downloaded" in the sense that its files exist, and it
        # is marked local so it can never be deleted.
        entry = model_store.describe(str(local))
        self.assertEqual(entry["kind"], "local")
        self.assertTrue(entry["downloaded"])
        self.assertEqual(entry["size_bytes"], 70)

    def test_local_model_without_weights(self):
        local = self.base / "empty-local"
        local.mkdir()
        entry = model_store.describe(str(local))
        self.assertFalse(entry["downloaded"])

    # ---- inventory ------------------------------------------------------
    def test_inventory_covers_the_whole_catalogue(self):
        rows = model_store.inventory()
        ids = [row["id"] for row in rows]
        from wayvoice.models import PRESET_IDS
        self.assertEqual(ids, PRESET_IDS)
        kinds = {row["id"]: row["kind"] for row in rows}
        self.assertEqual(kinds["small"], "repo")
        self.assertEqual(kinds["__custom__"], "custom")

    def test_inventory_marks_downloaded_and_sizes(self):
        rows = {row["id"]: row for row in model_store.inventory()}
        self.assertTrue(rows["small"]["downloaded"])
        self.assertEqual(rows["small"]["size_bytes"], WEIGHT + CONFIG + TOKENIZER + VOCABULARY)
        self.assertEqual(rows["small"]["repo_id"], "Systran/faster-whisper-small")
        self.assertFalse(rows["large-v3"]["downloaded"])
        self.assertEqual(rows["large-v3"]["size_bytes"], 0)

    def test_inventory_with_a_local_custom_model(self):
        local = self.base / "models" / "mine"
        local.mkdir(parents=True)
        (local / "model.bin").write_bytes(b"\0" * 33)
        row = model_store.describe(str(local))
        self.assertEqual(row["kind"], "local")
        self.assertEqual(row["size_bytes"], 33)

    # ---- what counts as a repo id --------------------------------------
    def test_repo_id_needs_exactly_one_slash(self):
        for good in ("org/name", "Systran/faster-whisper-small", "a/b"):
            self.assertTrue(model_store.is_repo_id(good), good)
        for bad in (
            "name",            # no org
            "a/b/c",           # a path, not a repo
            "/org/name",       # absolute
            "~/org/name",      # home-relative
            "./foo",           # relative to the cwd
            "../foo",
            "org/",            # empty name
            "/name",
            ".hidden/name",    # not something the hub would serve
            "org name/x",      # spaces
            "org/na\x00me",    # control characters
            "a--b/c",          # ambiguous once "/" becomes "--"
            "org/b--c",
            "C:/models/foo",
            "",
        ):
            self.assertFalse(model_store.is_repo_id(bad), bad)

    def test_stock_alias_resolves_to_its_repository(self):
        self.assertEqual(model_store.repo_id_for("small"), "Systran/faster-whisper-small")
        self.assertEqual(model_store.repo_dir_name("small"), "models--Systran--faster-whisper-small")
        self.assertEqual(model_store.repo_dir_name("turbo"), "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo")
        # A path is never turned into a cache directory name.
        self.assertIsNone(model_store.repo_dir_name("/home/u/models/foo"))
        self.assertIsNone(model_store.model_dir("./foo"))

    def test_aliases_match_the_installed_runtime(self):
        """The alias copy must not drift from the engine that is installed.

        It has to be a copy, because the settings window runs without the
        engine venv, but a stale copy is worse than none: a model the runtime
        knows and this table does not is reported as a local path, gets no size
        and can never be deleted.  So compare against the real thing whenever
        the runtime is there, and skip when it is not (a fresh checkout, CI).
        """
        runtime = engine.faster_runtime()
        python = runtime / "bin" / "python"
        if not python.exists():
            self.skipTest("the Faster-Whisper runtime is not installed")
        probe = (
            "import json;"
            "from faster_whisper import utils;"
            "print(json.dumps(utils._MODELS))"
        )
        try:
            out = subprocess.run(
                [str(python), "-c", probe],
                capture_output=True, text=True, timeout=120, check=True,
            )
            installed = json.loads(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.skipTest(f"the runtime could not be asked: {exc}")
        missing = {k: v for k, v in installed.items() if model_store.REPO_ALIASES.get(k) != v}
        self.assertEqual(
            missing, {},
            "REPO_ALIASES disagrees with faster_whisper.utils._MODELS: "
            f"{installed!r} vs {model_store.REPO_ALIASES!r}",
        )

    def test_local_paths_are_recognised_as_such(self):
        for path in ("/home/u/models/foo", "./foo", "~/models/foo", "C:/models/foo"):
            self.assertTrue(model_store.is_local_path(path), path)
        # Stock aliases are repositories, not paths.
        self.assertFalse(model_store.is_local_path("small"))

    # ---- hub root -------------------------------------------------------
    def test_hub_root_prefers_hf_hub_cache(self):
        # The tests set HF_HUB_CACHE; it must win over everything else.
        self.assertEqual(model_store.hub_root(), self.fake.hub)

    def test_hub_root_falls_back_to_legacy_then_default(self):
        os.environ.pop("HF_HUB_CACHE", None)
        legacy = self.base / "legacy"
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(legacy)
        self.assertEqual(model_store.hub_root(), legacy)
        os.environ.pop("HUGGINGFACE_HUB_CACHE", None)
        os.environ["HF_HOME"] = str(self.base / "hfhome")
        self.assertEqual(model_store.hub_root(), self.base / "hfhome" / "hub")
        os.environ.pop("HF_HOME", None)
        os.environ["XDG_CACHE_HOME"] = str(self.base / "xdg")
        self.assertEqual(model_store.hub_root(), self.base / "xdg" / "huggingface" / "hub")

    def test_missing_hub_is_not_an_error(self):
        # Nothing downloaded yet is a normal state, not a failure.
        os.environ["HF_HUB_CACHE"] = str(self.base / "nope")
        self.assertFalse(model_store.hub_root().exists())
        self.assertFalse(model_store.is_downloaded("small"))
        self.assertEqual(model_store.size_of("small"), 0)
        result = model_store.delete("small")
        self.assertTrue(result["ok"])
        self.assertEqual(result["freed_bytes"], 0)

    def test_disk_free_is_positive(self):
        self.assertGreater(model_store.disk_free(), 0)

    # ---- sizes as the user reads them ----------------------------------
    def test_human_size_uses_the_local_decimal_comma(self):
        self.assertEqual(model_store.human_size(483 * 1024 * 1024, "ru"), "483 МБ")
        self.assertEqual(model_store.human_size(483 * 1024 * 1024, "en"), "483 MB")
        self.assertEqual(model_store.human_size(1_500_000_000, "ru"), "1,4 ГБ")
        self.assertEqual(model_store.human_size(0, "ru"), "0 Б")

    def test_describe_reports_the_preset_label(self):
        entry = model_store.describe("small")
        self.assertEqual(entry["label"], "Small")
        # A free-form id has no preset and keeps its own text.
        self.assertEqual(model_store.describe("org/name")["label"], "name")


if __name__ == "__main__":
    unittest.main()
