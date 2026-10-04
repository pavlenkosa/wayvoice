"""What is actually on disk under the model catalogue.

The single source of truth about model files: the layout of the Hugging Face hub
cache, how big a model really is, and which blobs belong to a model exclusively
and which ones another model - or another application - still points at.

Three rules the rest of the project relies on:

* no network and no ``huggingface_hub`` import. The engine runtime is a separate
optional installation (:mod:`wayvoice.engine_setup`) and is routinely absent while
the settings window runs, so everything here comes from the filesystem and the
standard library;
* sizes are the sizes of the real files. A ``models--*`` directory holds symlinks
into ``blobs/``, and measuring it with ``du`` reports megabytes for a model whose
weights are half a gigabyte;
* the cache is shared. It holds models downloaded by other programs and blobs nobody
references any more, so nothing outside the catalogue is ever deleted and a blob
goes only when no snapshot in the whole hub points at it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from .i18n import tr
from .models import MODEL_PRESETS, display_name, preset_for

#: Prefix of a cached repository directory (``models--org--name``).
REPO_DIR_PREFIX = "models--"

#: Weights that make a snapshot "really there". faster-whisper downloads
#: ``model.bin``; a safetensors conversion of the same repo uses the other name, and
#: both are counted.
WEIGHT_NAMES = ("model.bin", "model.safetensors")

#: Sidecar files the hub keeps next to a shared blob. They belong to the blob and
#: are freed with it, so they are measured and removed as a unit.
BLOB_SIDECAR_SUFFIXES = (".lock", ".refs")

#: faster-whisper alias -> hub repository, a copy of ``faster_whisper.utils._MODELS``
#: from the installed runtime (checked against faster-whisper 1.2.1). A copy and not
#: an import, because the engine lives in its own venv and this module has to work in
#: the settings window where that venv may not exist yet.
#:
#: The catalogue offers "small", the cache directory is
#: ``models--Systran--faster-whisper-small``, and without this table every stock model
#: reads as "not downloaded" and cannot be deleted. ``tests/test_model_store.py``
#: compares the copy with the installed runtime, so an upgrade that adds a model
#: cannot pass unnoticed.
REPO_ALIASES: dict[str, str] = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base.en": "Systran/faster-whisper-base.en",
    "base": "Systran/faster-whisper-base",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
    "medium.en": "Systran/faster-whisper-medium.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

#: Files that make a faster-whisper snapshot usable. A copy of the
#: ``allow_patterns`` list in ``faster_whisper.utils.download_model`` (checked against
#: faster-whisper 1.2.1), and a copy of *that list* rather than of the whole
#: repository: the Systran repos carry safetensors weights and extra files next to
#: the CTranslate2 ``model.bin``, so fetching everything doubles the download for
#: ``tiny`` and wastes gigabytes on ``large``. Fetching less would leave the model
#: unusable, and the test that compares this copy with the runtime catches a
#: mismatch.
FETCH_PATTERNS: list[str] = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

#: Catalogue ids that name a model rather than a free-form value.
CATALOG_IDS: list[str] = [str(item["id"]) for item in MODEL_PRESETS]

#: The catalogue entry that holds whatever the user typed by hand.
CUSTOM_ID = "__custom__"


class RefusedError(RuntimeError):
    """A deletion was refused before anything was touched.

    Carries a translation key in :attr:`key` and the offending value in :attr:`detail`,
    so the UI can localize the reason without this module knowing about languages.
    """

    def __init__(self, key: str, detail: str = ""):
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key = key
        self.detail = detail

    def message(self, language: str | None = None) -> str:
        """The localized, user-facing reason for the refusal."""
        return tr(self.key, language, detail=self.detail)


def refusal_message(key: str, detail: str = "", language: str | None = None) -> str:
    """Localized text of a refusal that crossed a thread or a return value.

    Deletion reports a refusal as a key and a detail rather than a sentence, so the
    wording stays with the translations.
    """
    return tr(str(key or ""), language, detail=str(detail or ""))


# --------------------------------------------------------------------------
# Where the cache is
# --------------------------------------------------------------------------
def hub_root(root: str | os.PathLike[str] | None = None) -> Path:
    """The hub cache directory, without importing anything from the hub.

    The order ``huggingface_hub.constants`` uses, read off its source: ``HF_HUB_CACHE``,
    the legacy ``HUGGINGFACE_HUB_CACHE``, ``HF_HOME/hub`` (``HF_HOME`` defaults to
    ``$XDG_CACHE_HOME/huggingface`` and then to ``~/.cache/huggingface``, so this covers
    the last two), ``$XDG_CACHE_HOME/huggingface/hub``, ``~/.cache/huggingface/hub``.

    One deliberate difference: upstream expands ``~`` and ``$VARS`` for ``HF_HUB_CACHE``
    and ``HF_HOME`` but not for the legacy name. Expanding here as well only ever turns
    a literal ``~`` into the home directory.

    A missing directory is not an error; it means nothing was downloaded, and every
    caller answers "not downloaded" from that. ``root`` overrides the lookup, which is
    how the tests point this at a fake cache.
    """
    if root is not None:
        return Path(root).expanduser()
    default_home = os.path.join(os.path.expanduser("~"), ".cache")
    hf_home = os.path.expandvars(os.path.expanduser(os.environ.get("HF_HOME") or "")) \
        or os.path.join(os.environ.get("XDG_CACHE_HOME", default_home), "huggingface")
    legacy = os.environ.get("HUGGINGFACE_HUB_CACHE") or os.path.join(hf_home, "hub")
    return Path(os.path.expandvars(os.path.expanduser(os.environ.get("HF_HUB_CACHE") or legacy)))


# --------------------------------------------------------------------------
# What a model id is
# --------------------------------------------------------------------------
def is_repo_id(model_id: str | None) -> bool:
    """Whether ``model_id`` is a hub repository id rather than a local path.

    A repository id is exactly ``org/name``: one slash, two non-empty parts, no ``..``,
    no absolute path, no ``~``, no spaces or control characters. ``--`` is refused as
    well, because the cache directory name is built by replacing ``/`` with ``--``, which
    would make ``a--b/c`` and ``a/b--c`` indistinguishable on disk.

    Everything else is a path the user owns, and is never turned into a cache directory
    however much it resembles one.
    """
    value = str(model_id or "")
    if not value or value.count("/") != 1:
        return False
    if value.startswith(("/", "~", ".")) or value.endswith("/"):
        return False
    org, _, name = value.partition("/")
    if not org or not name or org in {".", ".."} or name in {".", ".."}:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if "\\" in value or "--" in value or ":" in value:
        return False
    return True


def is_local_path(model_id: str | None) -> bool:
    """Whether ``model_id`` is a filesystem path instead of a cached repo.

    Stock aliases ("small", "turbo") are neither: they are resolved to their repository
    first, so only genuinely unknown bare names, relative paths and absolute paths end
    up here.
    """
    return not repo_id_for(model_id)


def repo_id_for(model_id: str | None) -> str | None:
    """The hub repository id behind a catalogue entry, or ``None``.

    A stock alias is translated to its repository ("small" ->
    ``Systran/faster-whisper-small``); anything else that already is a repo id
    is returned as is.  Local paths and nonsense give ``None``.
    """
    value = str(model_id or "")
    alias = REPO_ALIASES.get(value)
    if alias:
        return alias
    return value if is_repo_id(value) else None


def repo_dir_name(model_id: str | None) -> str | None:
    """Cache directory name (``models--org--name``) for a catalogue entry."""
    repo_id = repo_id_for(model_id)
    if repo_id is None:
        return None
    org, _, name = repo_id.partition("/")
    return f"{REPO_DIR_PREFIX}{org}--{name}"


def model_dir(model_id: str, root: str | os.PathLike[str] | None = None) -> Path | None:
    """Cache directory of a catalogue entry, or ``None`` for a local path.

    Returned whether or not it exists - "does not exist" is what ``is_downloaded``
    reports - and never for a local path.
    """
    name = repo_dir_name(model_id)
    if name is None:
        return None
    return hub_root(root) / name


def local_dir(model_id: str) -> Path:
    """Directory of a user-owned local model, expanded and made absolute."""
    return Path(str(model_id or "")).expanduser()


# --------------------------------------------------------------------------
# Reading the cache
# --------------------------------------------------------------------------
def _iter_snapshot_dirs(model_dir_path: Path) -> Iterable[Path]:
    """Existing snapshot directories of a cached repository, newest last."""
    snapshots = model_dir_path / "snapshots"
    try:
        entries = sorted(item for item in snapshots.iterdir() if item.is_dir())
    except OSError:
        return []
    return entries


def snapshot_dir(model_id: str, root: str | os.PathLike[str] | None = None) -> Path | None:
    """Snapshot directory holding the files of a catalogue entry.

    ``refs/main`` names the revision the hub would use and wins; a revision without a ref
    (a leftover download) is still returned. ``None`` when there is no snapshot at all: a
    directory left by an interrupted download is not a model.
    """
    path = model_dir(model_id, root)
    if path is None:
        return None
    snapshots = _iter_snapshot_dirs(path)
    if not snapshots:
        return None
    try:
        revision = (path / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        revision = ""
    if revision:
        named = path / "snapshots" / revision
        if named.is_dir():
            return named
    return snapshots[-1]


def _weight_files(snapshot: Path) -> Iterable[Path]:
    """Weight files of a snapshot, following the snapshot's own symlinks."""
    for name in WEIGHT_NAMES:
        candidate = snapshot / name
        try:
            if candidate.stat().st_size > 0:
                yield candidate
        except OSError:
            continue


def is_downloaded(model_id: str, root: str | os.PathLike[str] | None = None) -> bool:
    """Whether a catalogue entry is present on disk and usable.

    A snapshot with a non-empty ``model.bin``/``model.safetensors``, not "the directory
    exists": a repository directory survives both an interrupted download and an offline
    deletion, and they look the same from the outside.
    """
    snapshot = snapshot_dir(model_id, root)
    if snapshot is None:
        return False
    return any(True for _ in _weight_files(snapshot))


def _real_files(base: Path) -> list[tuple[Path, int]]:
    """Real files under ``base``, deduplicated by resolved path.

    Every entry of a snapshot is a symlink into a blob, so ``lstat`` would report the
    length of the link target instead of the weight. Each path is resolved once and
    counted once: two snapshots pointing at one blob are one blob, and the hub may store
    one blob under two names.
    """
    seen: dict[str, int] = {}
    for folder, _dirs, files in os.walk(base, followlinks=False):
        for name in sorted(files):
            entry = Path(folder) / name
            try:
                if entry.is_symlink():
                    # A snapshot entry is a symlink to a blob; the size of the
                    # link is the length of its target, which means nothing.
                    real = Path(os.path.realpath(entry))
                    if not real.is_file():
                        continue
                    size = real.stat().st_size
                else:
                    real = entry
                    size = entry.stat().st_size
            except OSError:
                continue
            # First occurrence wins: the size belongs to the file, not to the
            # number of names pointing at it.
            seen.setdefault(str(real), size)
    return [(Path(key), size) for key, size in sorted(seen.items())]


def size_of(model_id: str, root: str | os.PathLike[str] | None = None) -> int:
    """Real size of a catalogue entry in bytes.

    For a cached repository, the sum of the sizes the snapshots really point at, each
    counted once. For a local path, the plain size of the directory the user keeps
    themselves - those files are real files, so the numbers agree.
    """
    snapshot = snapshot_dir(model_id, root)
    if snapshot is None:
        path = local_dir(model_id)
        if not path.is_dir():
            return 0
        return sum(size for _, size in _real_files(path))
    return sum(size for _, size in _real_files(snapshot))


def referenced_blobs(model_id: str, root: str | os.PathLike[str] | None = None) -> set[Path]:
    """Blobs the snapshots of one catalogue entry point at.

    Both locations count: the repository's own ``blobs/`` and the shared
    ``<hub>/blobs/<2 hex>/`` store used by the Xet backend. A path that is not inside a
    ``blobs`` directory is not a blob and is left alone.
    """
    path = model_dir(model_id, root)
    if path is None:
        return set()
    blobs: set[Path] = set()
    for snapshot in _iter_snapshot_dirs(path):
        for entry in snapshot.iterdir() if snapshot.is_dir() else ():
            try:
                if not entry.is_symlink() and not entry.is_file():
                    continue
                real = Path(os.path.realpath(entry))
            except OSError:
                continue
            if real.is_file() and "blobs" in real.parts:
                blobs.add(real)
    return blobs


def _other_referenced_blobs(hub: Path, keep: Path | None) -> set[Path]:
    """Every blob any *other* cached repository in the hub still points at.

    The whole hub is scanned, not just the WayVoice catalogue: models of one family share
    blobs, and other applications download into the same cache.
    """
    blobs: set[Path] = set()
    try:
        entries = sorted(hub.iterdir())
    except OSError:
        return blobs
    for entry in entries:
        if not entry.name.startswith(REPO_DIR_PREFIX) or not entry.is_dir():
            continue
        if keep is not None and entry == keep:
            continue
        try:
            snapshots = sorted(item for item in (entry / "snapshots").iterdir() if item.is_dir())
        except OSError:
            continue
        for snapshot in snapshots:
            for item in snapshot.iterdir():
                try:
                    if not item.is_symlink() and not item.is_file():
                        continue
                    real = Path(os.path.realpath(item))
                except OSError:
                    continue
                if real.is_file() and "blobs" in real.parts:
                    blobs.add(real)
    return blobs


def _sidecars(blob: Path) -> list[Path]:
    """The ``.lock``/``.refs`` files that belong to one shared blob."""
    found: list[Path] = []
    for suffix in BLOB_SIDECAR_SUFFIXES:
        candidate = blob.with_name(blob.name + suffix)
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def _prune_repository(path: Path, needed: set[Path]) -> None:
    """Take a repository directory apart, keeping what others still need.

    Used when the model has to disappear but some of its blobs turned out to be shared:
    the hub keeps such a blob either in the shared store or in the private ``blobs/`` of
    whichever repository fetched it first, so a private blob can outlive the model that
    downloaded it and a plain ``rmtree`` would leave another model with dangling links.

    The directory is emptied rather than deleted - revisions, refs and unreferenced blobs
    go, the blobs in ``needed`` stay - and a directory left completely empty is removed.
    Anything left behind is a deliberate outcome, not a cleanup that failed.
    """
    for name in ("snapshots", "refs", "trees"):
        target = path / name
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError:
                pass
    blobs = path / "blobs"
    if blobs.is_dir():
        try:
            entries = sorted(blobs.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            base = Path(os.path.realpath(entry)) if entry.is_symlink() else entry
            if base in needed:
                continue
            for victim in (entry, *_sidecars(entry)):
                try:
                    if victim.is_dir() and not victim.is_symlink():
                        shutil.rmtree(victim)
                    else:
                        victim.unlink()
                except OSError:
                    continue
        try:
            blobs.rmdir()
        except OSError:
            pass
    try:
        # Whatever is left apart from the blob directory is not part of a usable cache
        # entry any more. ``blobs`` is skipped on purpose: it still holds the shared
        # blobs another model links to, and sweeping it here would create exactly the
        # dangling symlink this whole dance exists to avoid.
        for entry in path.iterdir():
            if entry.name == "blobs" and entry.is_dir():
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        path.rmdir()
    except OSError:
        pass


def _is_inside(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` itself or something under it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _refuse(model_id: str, key: str, detail: str = "") -> RefusedError:
    return RefusedError(key, detail or str(model_id or ""))


# --------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------
def _check_deletable(model_id: str, root: str | os.PathLike[str] | None) -> Path:
    """Resolve the directory to delete, or refuse with a reason.

    Every refusal happens before any file is touched, ordered from "this is not ours to
    delete" to "this path is not what it claims to be": the first explain the decision to
    the user, the last only protect against a doctored cache.
    """
    value = str(model_id or "")
    if not value.strip():
        raise _refuse(value, "store.refuse_unknown")
    if repo_id_for(value) is None:
        # A local path, a relative path or nonsense. WayVoice did not put those bytes
        # there and does not own them.
        raise _refuse(value, "store.refuse_local")
    if value not in CATALOG_IDS:
        # A valid repository id, but not one this application offers. The custom
        # field accepts any repo id and the cache cannot say who downloaded one, so
        # WayVoice deletes only what it lists itself.
        raise _refuse(value, "store.refuse_not_in_catalogue")
    path = model_dir(value, root)
    if path is None:  # pragma: no cover - guarded by the checks above
        raise _refuse(value, "store.refuse_local")
    if path.is_symlink():
        # Removing a symlink here would either delete a link to somewhere else
        # or, with a recursive delete, follow it into another tree.
        raise _refuse(value, "store.refuse_symlink")
    hub = hub_root(root).resolve()
    if not _is_inside(path.resolve(), hub):
        raise _refuse(value, "store.refuse_outside")
    return path


def delete(
    model_id: str,
    root: str | os.PathLike[str] | None = None,
    must_exist: bool = False,
) -> dict[str, Any]:
    """Delete a cached model and the blobs only it needed.

    The order is the point of this function:

    1. decide, before touching anything - a local path, an id outside the catalogue, a
    symlinked directory or a path leaving the hub raises :class:`RefusedError`;
    2. collect this model's own blobs, since "may I delete this blob?" can only be
    answered once it is known that this model was the last snapshot pointing at it;
    3. collect every other snapshot's blobs in the whole hub, not just the WayVoice
    catalogue: one ``tokenizer.json`` is shared inside a family, and other
    applications download into the same cache;
    4. empty the directory instead of deleting it when some of its blobs are still
    referenced - the shared ones stay put, the directory disappears only if nothing
    is left;
    5. remove only the blobs of step 2 that step 3 did not report, and only those inside
    the hub, so no dangling symlink is left anywhere;
    6. report the real bytes freed and, separately, the bytes that survived because
    another model uses them.

    Steps 2 and 3 have to precede step 4: once the directory is gone, the only way to
    find out which blobs were shared is to guess, and a wrong guess leaves either an
    unloadable model or a blob store that grows forever.

    Deleting a model that is not there frees nothing and says so with ``message_key``, so
    a second Delete is a no-op rather than an error; a caller that wants to be told passes
    ``must_exist=True``.

    Returns ``ok``, ``model_id``, ``freed_bytes``, ``kept_bytes``, ``removed_dir``,
    ``removed_blobs``, ``kept_blobs`` and, when nothing was there, a ``message_key``.
    """
    path = _check_deletable(model_id, root)
    value = str(model_id)
    if not path.exists():
        if must_exist:
            raise RefusedError("store.refuse_missing", value)
        return {
            "ok": True,
            "model_id": value,
            "freed_bytes": 0,
            "kept_bytes": 0,
            "removed_dir": False,
            "removed_blobs": 0,
            "kept_blobs": 0,
            "message_key": "store.nothing_to_delete",
        }

    own = referenced_blobs(value, root)
    hub = hub_root(root).resolve()
    others = _other_referenced_blobs(hub, keep=path)
    exclusive = own - others
    #: Every blob inside this repository's directory that some *other* snapshot still
    #: points at - not only the ones this model's own snapshots used. A repository
    #: directory also holds blobs of revisions that are no longer there, and another
    #: model may be the one still using those: ``rmtree`` would take them with the
    #: directory and leave that model with dangling links.
    shared_inside = {blob for blob in others if _is_inside(blob, path)}

    # Measured before anything is removed, because a repository's own blobs live inside
    # the directory step 5 deletes: asking the filesystem afterwards reports them as
    # "already gone" and understates the freed space.
    #
    # Two different things are counted and must not be added up: what the disk really
    # gets back, and what survives because another model links to it. A relocated
    # blob belongs to the second group - its content lives on under the new name, so
    # counting it as freed would promise space that did not become free.
    freed: dict[Path, int] = {}
    kept_bytes = 0
    for blob in own | shared_inside:
        try:
            size = blob.stat().st_size
        except OSError:
            continue
        if blob in shared_inside:
            kept_bytes += size
            continue
        if _is_inside(blob, path):
            freed[blob] = size
    for blob in exclusive:
        if _is_inside(blob, path):
            continue  # already counted, it dies with the directory
        try:
            freed[blob] = blob.stat().st_size
        except OSError:
            continue
        for sidecar in _sidecars(blob):
            try:
                freed[sidecar] = sidecar.stat().st_size
            except OSError:
                continue

    result: dict[str, Any] = {
        "ok": True,
        "model_id": value,
        "freed_bytes": 0,
        "kept_bytes": kept_bytes,
        "removed_dir": False,
        "removed_blobs": 0,
        "kept_blobs": len(own & others),
    }
    # Before the directory goes: make every blob another model needs survive it.
    if shared_inside:
        # The model goes, but not the whole directory: the blobs in it are still
        # somebody else's model files. Removing them would break that model, and
        # moving them to the shared store would leave the blob store growing with
        # content nobody points at.
        _prune_repository(path, needed=shared_inside)
        result["removed_dir"] = not path.exists()
    else:
        try:
            shutil.rmtree(path)
            result["removed_dir"] = True
        except OSError as exc:
            raise RefusedError("store.delete_failed", str(exc)) from exc
    result["freed_bytes"] += sum(freed.values())

    # What is left to unlink is only what lived outside the model directory:
    # the shared blob store, and the sidecars of a shared blob.
    for blob in sorted(exclusive):
        if not _is_inside(blob, hub) or not blob.exists():
            continue
        for sidecar in _sidecars(blob):
            try:
                sidecar.unlink()
            except OSError:
                pass
        try:
            blob.unlink()
        except OSError:
            continue
        result["removed_blobs"] += 1
    return result


# --------------------------------------------------------------------------
# The catalogue as a whole
# --------------------------------------------------------------------------
def describe(model_id: str, root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Kind, state and size of any single model id.

    Works for a catalogue entry, a free-form repository id and a local path, which is
    what the settings window needs: it shows a row for whatever the user typed in the
    custom field, not only for the presets.
    """
    preset = preset_for(model_id)
    label = str(preset["label"]) if preset is not None else display_name(model_id)
    return _entry(model_id, label, root)


def _entry(model_id: str, label: str, root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """One inventory row: kind, whether it is there, and how big it is."""
    repo_id = repo_id_for(model_id)
    if model_id == CUSTOM_ID:
        # The free-form entry names whatever the user typed, which is unknown
        # until they type it; the settings window asks about it directly.
        return {
            "id": CUSTOM_ID,
            "label": label,
            "kind": "custom",
            "repo_id": None,
            "path": None,
            "downloaded": False,
            "size_bytes": 0,
        }
    if repo_id is None:
        path = local_dir(model_id)
        present = path.is_dir()
        return {
            "id": model_id,
            "label": label,
            "kind": "local",
            "repo_id": None,
            "path": str(path),
            "downloaded": present and any(True for _ in _weight_files(path)),
            "size_bytes": size_of(model_id, root) if present else 0,
        }
    path = model_dir(model_id, root)
    return {
        "id": model_id,
        "label": label,
        "kind": "repo",
        "repo_id": repo_id,
        "path": str(path) if path is not None else None,
        "downloaded": is_downloaded(model_id, root),
        "size_bytes": size_of(model_id, root),
    }


def inventory(root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Every catalogue entry with its kind, state and size.

    ``kind`` is ``repo`` for a cached repository, ``local`` for a user-owned path and
    ``custom`` for the free-form entry, which is why a local model is never reported as
    "not downloaded" just because it is not in the hub.
    """
    return [_entry(str(item["id"]), str(item["label"]), root) for item in MODEL_PRESETS]


def total_size(root: str | os.PathLike[str] | None = None) -> int:
    """Bytes taken by the catalogue models, shared blobs counted once.

    Two models of one family share ``tokenizer.json``, and adding their sizes would report
    more than the cache actually holds.
    """
    seen: dict[str, int] = {}
    for item in inventory(root):
        if item["kind"] != "repo":
            continue
        path = model_dir(str(item["id"]), root)
        if path is None:
            continue
        for snapshot in _iter_snapshot_dirs(path):
            for real, size in _real_files(snapshot):
                seen.setdefault(str(real), size)
    return sum(seen.values())


def hub_size(root: str | os.PathLike[str] | None = None) -> int:
    """Bytes the whole hub cache occupies, models of other programs included.

    :func:`total_size` counts only what this application downloaded, which on its own does
    not explain the disk: the same directory holds other applications' models and blobs
    nobody references any more. Both numbers are shown, so the settings window and a disk
    analyser can be compared.
    """
    hub = hub_root(root)
    if not hub.is_dir():
        return 0
    seen: dict[str, int] = {}
    for folder, _dirs, files in os.walk(hub):
        for name in files:
            entry = Path(folder) / name
            try:
                real = Path(os.path.realpath(entry))
                size = real.stat().st_size
            except OSError:
                continue
            seen.setdefault(str(real), size)
    return sum(seen.values())


def disk_free(root: str | os.PathLike[str] | None = None) -> int:
    """Free bytes on the filesystem the cache lives on (0 when unknowable)."""
    target = hub_root(root)
    for candidate in (target, *target.parents):
        try:
            return int(shutil.disk_usage(candidate).free)
        except OSError:
            continue
    return 0


def human_size(num_bytes: int, language: str | None = None) -> str:
    """A size the way the settings window shows it: ``483 МБ`` / ``1,5 ГБ``."""
    value = float(max(0, int(num_bytes)))
    if value < 1024:
        return f"{int(value)} {tr('size.b', language)}"
    if value < 1024 ** 2:
        scaled, unit = value / 1024, tr("size.kb", language)
    elif value < 1024 ** 3:
        scaled, unit = value / 1024 ** 2, tr("size.mb", language)
    else:
        scaled, unit = value / 1024 ** 3, tr("size.gb", language)
    digits = 1 if scaled < 10 else 0
    return f"{scaled:.{digits}f}".replace(".", ",") + f" {unit}"
