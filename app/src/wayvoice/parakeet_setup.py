"""Explicit, pinned Parakeet preparation; never called by ordinary dictation."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import time
import urllib.request

from .runtime.locations import _data_home
from .paths import app_dir

RUNTIME_VERSION = '0.2.0'
RUNTIME_REVISION = '6a3ca369370782acee0dd155e34394ba60b920c4'
RUNTIME_NAME = 'nemo-speech-0.2.0-linux-x86_64-cpu'
RUNTIME_SHA256 = 'f396057150f1b774935c7414fd32ebc04195a795a34120d78d8c9ecefa1b7507'
RUNTIME_URL = f'https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.2.0/{RUNTIME_NAME}.tar.gz'
MODEL_REVISION = '541d1f99c6b0c3cd0b11a95167540bb8edefd82b'
MODEL_NAME = 'parakeet-tdt-0.6b-v3.q8_0.gguf'
MODEL_SIZE = 713975456
MODEL_SHA256 = 'e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e'
MODEL_URL = f'https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/{MODEL_REVISION}/{MODEL_NAME}'


def default_root():
    return _data_home() / 'wayvoice' / 'parakeet'


def download(url, target, digest, limit):
    deadline = time.monotonic() + 900
    actual = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=20) as response, target.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit or time.monotonic() >= deadline:
                raise RuntimeError('Parakeet download exceeds size or time limit')
            actual.update(chunk)
            output.write(chunk)
    if actual.hexdigest() != digest:
        raise RuntimeError('Parakeet download SHA256 mismatch')


def extract_runtime(archive, destination):
    # The official archive contains shared-library symlinks. Python's data
    # filter rejects links escaping the extraction root and special files.
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 4096 or sum(m.size for m in members) > 256 * 1024 * 1024:
            raise RuntimeError('Parakeet runtime archive exceeds limits')
        if any(not m.name.startswith(RUNTIME_NAME + '/') for m in members):
            raise RuntimeError('Unexpected Parakeet runtime archive layout')
        source.extractall(destination, filter='data')


def prepare(destination=None):
    import platform
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Experimental Parakeet preparation supports Linux x86_64 only')
    root = Path(destination) if destination else default_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.prepare.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Parakeet preparation is already running') from exc
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=root) as stage_name:
            stage = Path(stage_name)
            runtime = root / RUNTIME_NAME
            bundled = app_dir().parent / 'parakeet'
            if destination is None and (bundled / 'bin/nemo-speech').is_file():
                runtime = bundled
            if not runtime.exists():
                archive = stage / 'runtime.tar.gz'
                download(RUNTIME_URL, archive, RUNTIME_SHA256, 32 * 1024 * 1024)
                extract_runtime(archive, stage)
                os.rename(stage / RUNTIME_NAME, runtime)
            binary = runtime / "bin/nemo-speech"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError("Existing Parakeet runtime is incomplete; preserved unchanged")
            model = root / MODEL_NAME
            if model.exists():
                with model.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != MODEL_SHA256:
                        raise RuntimeError('Existing Parakeet model checksum mismatch; preserved unchanged')
            else:
                temporary = stage / MODEL_NAME
                download(MODEL_URL, temporary, MODEL_SHA256, MODEL_SIZE)
                if temporary.stat().st_size != MODEL_SIZE:
                    raise RuntimeError('Parakeet model size mismatch')
                os.rename(temporary, model)
    return root


def main():
    parser = argparse.ArgumentParser(description='Explicitly download verified CPU Parakeet runtime and weights (about 690 MiB).')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = prepare(args.destination)
    bundled = app_dir().parent / "parakeet/bin/nemo-speech"
    binary = bundled if args.destination is None and bundled.is_file() else root / RUNTIME_NAME / "bin/nemo-speech"
    print(f"Runtime: {binary}")
    print(f'Model: {root / MODEL_NAME}')


if __name__ == '__main__':
    main()
