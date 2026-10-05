"""Deleting a model must not break another program's model.

Three ways it did, all confirmed on a fake cache before the fix and all of them
reported ``ok: True``:

1. ``_prune_repository`` kept what others needed by comparing the *entries* of
   ``blobs/`` against the blob paths in ``needed``. A blob lives at
   ``blobs/<2 hex>/<name>``, so the entries are the two-hex directories and the
   comparison never matched: the branch whose whole purpose is to protect another
   model's files deleted the very files it was asked to keep. The answer even said
   ``kept_bytes: 500000`` while the file was gone.

2. A snapshot was read with ``iterdir()``, which is the top level only. A repository
   with an ``onnx/`` keeps real subdirectories inside ``snapshots/<revision>/``, so a
   blob another program reached through one of them looked unreferenced and was deleted.

3. The two sides of every comparison were different kinds of path: the model directory
   came straight from the configured cache root while the hub was resolved. With a
   symlink anywhere on the way - ``HF_HUB_CACHE=~/models-hf`` on another disk,
   ``~/.cache`` a link, which is an ordinary setup - the model being deleted never
   matched ``keep``, so it counted as "another repository" and every number in the
   answer was wrong: ``freed_bytes: 0`` for a deletion that freed everything.

Nothing here touches a real cache: every test builds its own directory under a
temporary one and passes it as ``root``.
"""

import os
import tempfile
import unittest
from pathlib import Path

from wayvoice import model_store as ms


class FakeCache:
    """A hub cache with one model of ours and one model belonging to somebody else."""

    def __init__(self, base: Path):
        self.root = base / "cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.repo = "models--Systran--faster-whisper-small"
        self.foreign_repo = "models--vendor--tts"
        self.foreign_rev = self.root / self.foreign_repo / "snapshots" / "bbbb"

    def _blob(self, relative: str, name: str, size: int) -> Path:
        target = self.root / relative
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_bytes(b"x" * size)
        return path

    def _link(self, snapshot: Path, name: str, target: Path) -> Path:
        snapshot.mkdir(parents=True, exist_ok=True)
        link = snapshot / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(os.path.relpath(target, snapshot))
        return link

    def build(self, *, shared_inside_repo: bool, foreign_link_in_subdir: bool,
              via_symlink: bool) -> tuple[Path, Path]:
        """Return the cache root to hand to delete(), and the foreign link to check."""
        where = f"{self.repo}/blobs/ab" if shared_inside_repo else "blobs/ab"
        shared = self._blob(where, "shared.bin", 500_000)
        own = self._blob(where, "tokenizer.json", 4096)
        snapshot = self.root / self.repo / "snapshots" / "aaaa"
        self._link(snapshot, "model.bin", shared)
        self._link(snapshot, "tokenizer.json", own)

        parent = self.foreign_rev / "onnx" if foreign_link_in_subdir else self.foreign_rev
        foreign = self._link(parent, "model.bin", shared)

        root = self.root
        if via_symlink:
            link = self.root.parent / "envlink"
            if link.is_symlink():
                link.unlink()
            link.symlink_to(self.root)
            root = link
        return root, foreign


class DeletingKeepsOtherModelsIntactTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def _run(self, **kwargs):
        cache = FakeCache(self.base)
        root, foreign = cache.build(**kwargs)
        result = ms.delete("small", root=root)
        return result, foreign

    def test_a_shared_blob_inside_the_model_directory_survives(self):
        # The regression: this branch exists to protect another model's files, and it
        # deleted them - while the answer reported the size as kept.
        result, foreign = self._run(shared_inside_repo=True, foreign_link_in_subdir=False,
                                    via_symlink=False)
        self.assertTrue(foreign.exists(), "another program's model lost its file")
        self.assertEqual(result["kept_bytes"], 500_000)

    def test_a_blob_reached_through_a_subdirectory_of_a_snapshot_survives(self):
        # A repository with onnx/ keeps real subdirectories inside snapshots/<rev>/.
        _result, foreign = self._run(shared_inside_repo=False, foreign_link_in_subdir=True,
                                     via_symlink=False)
        self.assertTrue(foreign.exists(), "another program's model lost its file")

    def test_the_same_through_a_symlinked_cache_root(self):
        _result, foreign = self._run(shared_inside_repo=True, foreign_link_in_subdir=False,
                                     via_symlink=True)
        self.assertTrue(foreign.exists(), "another program's model lost its file")

    def test_the_freed_size_is_reported_through_a_symlink(self):
        # With the two sides compared as different kinds of path, our own model counted
        # as somebody else's and the answer was 0 for a deletion that freed everything.
        cache = FakeCache(self.base)
        root, _foreign = cache.build(shared_inside_repo=False, foreign_link_in_subdir=False,
                                     via_symlink=True)
        result = ms.delete("small", root=root)
        self.assertEqual(result["freed_bytes"], 4096)

    def test_a_shared_blob_in_the_shared_store_survives_and_is_named(self):
        # A blob in the shared store that another model points at is none of this
        # deletion's business: it stays, and it is reported as kept - but not as
        # "kept because of this deletion". The window's "files stayed, another model
        # uses them" message is about what was inside the deleted directory, and every
        # preset of a family shares its tokenizer, so counting these would make that
        # message fire on every ordinary deletion.
        result, foreign = self._run(shared_inside_repo=False, foreign_link_in_subdir=False,
                                    via_symlink=False)
        self.assertTrue(foreign.exists())
        self.assertEqual(result["kept_bytes"], 0)
        self.assertEqual(result["kept_blobs"], 1)
        self.assertEqual(result["freed_bytes"], 4096)

    def test_nothing_of_ours_is_left_when_nothing_is_shared(self):
        cache = FakeCache(self.base)
        root, _foreign = cache.build(shared_inside_repo=False, foreign_link_in_subdir=False,
                                     via_symlink=False)
        # Remove the foreign reference, so both blobs are ours alone.
        shutil_target = cache.root / cache.foreign_repo
        for path in sorted(shutil_target.rglob("*"), reverse=True):
            path.unlink() if path.is_symlink() or path.is_file() else path.rmdir()
        result = ms.delete("small", root=root)
        self.assertFalse((cache.root / cache.repo).exists())
        self.assertEqual(result["freed_bytes"], 504_096)
        self.assertEqual(result["kept_bytes"], 0)


class SnapshotWalkingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_a_file_in_a_subdirectory_of_a_snapshot_is_reachable(self):
        cache = FakeCache(self.base)
        cache.build(shared_inside_repo=False, foreign_link_in_subdir=True, via_symlink=False)
        snapshot = cache.root / cache.repo / "snapshots" / "aaaa"
        found = {path.name for path in ms._snapshot_links(snapshot)}
        self.assertEqual(found, {"model.bin", "tokenizer.json"})

    def test_a_symlinked_directory_is_not_followed(self):
        # A snapshot cannot send the walk out of the cache or into a loop.
        cache = FakeCache(self.base)
        snapshot = cache.root / cache.repo / "snapshots" / "aaaa"
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "loop").symlink_to(snapshot)
        (snapshot / "model.bin").write_bytes(b"m")
        self.assertEqual({path.name for path in ms._snapshot_links(snapshot)},
                         {"model.bin"})


if __name__ == "__main__":
    unittest.main()