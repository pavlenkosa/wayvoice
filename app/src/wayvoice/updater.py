"""Explicit native-package updates from the project's published stable releases."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

from . import __version__

REPOSITORY = 'https://github.com/pavlenkosa/wayvoice'
API = 'https://api.github.com/repos/pavlenkosa/wayvoice/releases/latest'
MAX_PACKAGE = 256 * 1024 * 1024


def lock_path() -> Path:
    from .protocol import socket_path
    return socket_path().parent / 'wayvoice-update.lock'


def updating() -> bool:
    path = lock_path()
    if not path.exists():
        return False
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@contextmanager
def update_lock():
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another WayVoice update is already running')
        yield


@contextmanager
def runtime_update_lock():
    # Update lock is acquired first. Setup owns this same lock before checking
    # the update lock, so neither existing nor newly requested pip work can race.
    from .engine_setup import setup_lock_path
    path = setup_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Finish engine runtime preparation before updating')
        yield


@contextmanager
def installation_signals():
    # Terminal closure/Ctrl+C must not release the lock while apt/dnf still works.
    watched = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    previous = {item: signal.getsignal(item) for item in watched}
    try:
        for item in watched:
            signal.signal(item, signal.SIG_IGN)
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('Unsupported release version: ' + value)
    return tuple(map(int, value.removeprefix('v').split('.')))


def _read(url: str, limit: int) -> bytes:
    request = Request(url, headers={'User-Agent': 'WayVoice/' + __version__,
                                   'Accept': 'application/vnd.github+json'})
    deadline = time.monotonic() + 300
    data = bytearray()
    with urlopen(request, timeout=30) as response:
        if not response.geturl().startswith('https://'):
            raise ValueError('Insecure release redirect')
        while True:
            chunk = response.read(min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError('Release response exceeds size limit')
            if time.monotonic() > deadline:
                raise TimeoutError('Release download timed out')


def _output(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=15).strip()


def package_system() -> tuple[str, str]:
    from .paths import appimage_path
    if appimage_path():
        raise ValueError("Update AppImage manually by replacing its file after quitting WayVoice.")
    if os.environ.get('FLATPAK_ID') or Path('/.flatpak-info').exists():
        raise ValueError('Use flatpak update for a Flatpak installation.')
    source = str(Path(__file__).resolve())
    if shutil.which('dpkg-query'):
        try:
            owner = _output(['dpkg-query', '-S', source]).split(': ', 1)[0]
            if owner.split(':')[0] == 'wayvoice':
                return 'deb', _output(['dpkg', '--print-architecture'])
        except subprocess.SubprocessError:
            pass
    if shutil.which('rpm'):
        try:
            if _output(['rpm', '-qf', '--qf', '%{NAME}', source]) == 'wayvoice':
                return 'rpm', _output(['rpm', '--eval', '%{_arch}'])
        except subprocess.SubprocessError:
            pass
    raise ValueError('Self-update requires an installed WayVoice DEB/RPM, not a source checkout.')


def check() -> dict:
    release = json.loads(_read(API, 2 * 1024 * 1024))
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Not a stable published release')
    version(tag)
    return {'version': tag.removeprefix('v'), 'available': version(tag) > version(__version__),
            'release': release}


def select_asset(release: dict, kind: str, arch: str) -> dict:
    number = release['tag_name'].removeprefix('v')
    version(number)
    name = (f'wayvoice_{number}_{arch}.deb' if kind == 'deb'
            else f'wayvoice-{number}-1.{arch}.rpm')
    pattern = (re.escape(name) if kind == 'deb' else
               rf'wayvoice-{re.escape(number)}-1(?:\.fc[0-9]+)?\.{re.escape(arch)}\.rpm')
    matches = [asset for asset in release['assets'] if re.fullmatch(pattern, asset['name'])]
    if len(matches) != 1:
        raise ValueError('Release has no unique package for this system: ' + name)
    return matches[0]


def asset_url(release: dict, asset: dict) -> str:
    url = asset['browser_download_url']
    expected = REPOSITORY + '/releases/download/' + release['tag_name'] + '/' + asset['name']
    if url != expected or '/' in asset['name'] or '\\' in asset['name']:
        raise ValueError('Unexpected release asset URL')
    return url


def validate_package(path: Path, kind: str, arch: str, number: str) -> None:
    if kind == 'deb':
        metadata = _output(['dpkg-deb', '-f', str(path), 'Package', 'Version', 'Architecture'])
        fields = dict(line.split(': ', 1) for line in metadata.splitlines())
        actual = fields.get('Package'), fields.get('Version'), fields.get('Architecture')
    else:
        actual = tuple(_output(['rpm', '-qp', '--qf', '%{NAME}\n%{VERSION}\n%{ARCH}', str(path)]).splitlines())
    if actual != ('wayvoice', number, arch):
        raise ValueError('Package identity, version or architecture does not match release')


def install(*, confirm: bool = True, event=None) -> None:
    # CLI owns the package-manager child; no timeout or window-owned worker may kill it.
    from .cli import request
    notify = event or (lambda **fields: None)
    notify(stage='check')
    kind, arch = package_system()
    result = check()
    if not result['available']:
        if confirm:
            print('WayVoice is up to date (' + __version__ + ').')
        notify(stage='done', version=__version__, updated=False)
        return
    release = result['release']
    asset = select_asset(release, kind, arch)
    sums = [a for a in release['assets'] if a['name'] == 'SHA256SUMS']
    if len(sums) != 1:
        raise ValueError('Release checksums are missing')
    checksums = _read(asset_url(release, sums[0]), 65536).decode('utf-8')
    hashes = [line.split()[0] for line in checksums.splitlines()
              if len(line.split()) == 2 and line.split()[1].lstrip('*') == asset['name']]
    if len(hashes) != 1 or not re.fullmatch('[0-9a-fA-F]{64}', hashes[0]):
        raise ValueError('Package checksum is missing or ambiguous')
    manager = shutil.which('apt-get' if kind == 'deb' else 'dnf')
    privilege = [] if os.geteuid() == 0 else [shutil.which('pkexec') or '']
    if not manager or (privilege and not privilege[0]):
        raise ValueError('System package manager or pkexec is unavailable')
    with tempfile.TemporaryDirectory(prefix='wayvoice-update-') as directory:
        path = Path(directory) / asset['name']
        notify(stage='download', version=result['version'])
        path.write_bytes(_read(asset_url(release, asset), MAX_PACKAGE))
        notify(stage='verify')
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[0].lower():
            raise ValueError('Package SHA-256 mismatch')
        validate_package(path, kind, arch, result['version'])
        if confirm:
            print(f"Install WayVoice {result['version']}? Services will restart. [y/N]", flush=True)
            if input().strip().lower() not in {'y', 'yes'}:
                return
        with installation_signals(), update_lock(), runtime_update_lock():
            # The new daemon checks this same lock before starting recording/model work.
            # A serialized readiness command waits for any already-starting operation.
            ready = request('update-ready', timeout=10)
            if not ready.get('ok'):
                raise ValueError('Daemon is busy, unavailable or too old for safe self-update: ' + str(ready.get('error', '')))
            notify(stage='authorize' if privilege else 'install')
            _install_process(privilege + [manager, 'install', '-y', str(path)], directory, event)
            notify(stage='restart', installed=True)
            # Execute fresh installed code, never modules imported before upgrade.
            wrapper = shutil.which('wayvoice')
            if not wrapper:
                raise ValueError('Package installed, but wayvoice launcher is unavailable')
            _install_process([wrapper, 'update', '--restart', result['version']], directory, event)
    notify(stage='done', version=result['version'], updated=True, installed=True)
    if confirm:
        print('Update complete. Close and reopen the settings window to load the new UI.')



def _install_process(args: list[str], directory: str, event) -> None:
    if event is None:
        subprocess.run(args, check=True)
        return
    # Package-manager output must never become part of the GUI JSON protocol.
    # Keep transaction ownership until the child finishes; no kill timeout.
    with (Path(directory) / 'installer.log').open('w+b') as log:
        try:
            subprocess.run(args, check=True, stdout=log, stderr=log)
        except subprocess.CalledProcessError as exc:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 4096))
            detail = log.read().decode('utf-8', errors='replace').strip()
            raise ValueError(str(exc) + (': ' + detail if detail else '')) from exc


def gui_install(restart_version: str | None = None) -> None:
    installed = restart_version is not None
    selected_version = restart_version

    def emit(**fields):
        nonlocal installed, selected_version
        selected_version = fields.get("version") or selected_version
        installed = installed or fields.get('installed', False)
        try:
            print(json.dumps(fields, ensure_ascii=False), flush=True)
        except BrokenPipeError:
            # Closing the UI must not interrupt apt/dnf or release its locks.
            sys.stdout = open(os.devnull, 'w')

    try:
        if restart_version is None:
            install(confirm=False, event=emit)
        else:
            version(restart_version)
            from .cli import request
            with installation_signals(), update_lock(), runtime_update_lock():
                if not request('ping', timeout=1).get('ok'):
                    from .service import start_daemon
                    if not start_daemon(wait=15):
                        raise ValueError('Could not restore the daemon before restart')
                ready = request('update-ready', timeout=10)
                if not ready.get('ok'):
                    raise ValueError('Daemon is not ready to restart: ' + str(ready.get('error', '')))
                emit(stage='restart', installed=True)
                launcher = shutil.which('wayvoice')
                if not launcher:
                    raise ValueError('Installed launcher is unavailable')
                with tempfile.TemporaryDirectory(prefix='wayvoice-restart-') as directory:
                    _install_process([launcher, 'update', '--restart', restart_version], directory, emit)
            emit(stage='done', installed=True, version=restart_version, updated=False)
    except (OSError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as exc:
        emit(stage='error', message=str(exc), installed=installed, version=selected_version)
        raise SystemExit(1)


def restart(number: str) -> None:
    from .cli import request
    from .service import restart_daemon, systemd_available
    if __version__ != number:
        raise ValueError('Installed launcher still loads an unexpected version')
    if systemd_available():
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=15)
        subprocess.run(['systemctl', '--user', 'try-restart', 'wayvoice-ydotool.service'], check=True, timeout=30)
    if not restart_daemon(wait=15):
        raise ValueError('Package installed, but daemon restart failed')
    for _ in range(30):
        if request('status').get('version') == number:
            return
        time.sleep(.5)
    raise ValueError('Package installed, but running daemon version was not confirmed')


def command(args: list[str]) -> None:
    if len(args) == 2 and args[0] == '--gui-restart':
        gui_install(args[1])
        return
    if args == ['--gui-install']:
        gui_install()
        return
    try:
        if args in (['--install'], ['--interactive']):
            install()
        elif len(args) == 2 and args[0] == '--restart':
            version(args[1])
            restart(args[1])
        elif args in ([], ['--check']):
            result = check()
            print(json.dumps({k: v for k, v in result.items() if k != 'release'}))
        else:
            raise ValueError('Usage: wayvoice update [--check|--install]')
    except (OSError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as exc:
        print('WayVoice update: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)

    finally:
        if args == ['--interactive']:
            try:
                input('Press Enter to close…')
            except EOFError:
                pass
