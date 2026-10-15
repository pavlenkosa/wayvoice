#!/usr/bin/env python3
"""Construct an explicit GTK4/Python AppDir inside the Ubuntu build container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOTS = ('python3', 'python3-gi', 'python3-gi-cairo', 'python3-cairo',
         'gir1.2-gtk-4.0', 'gir1.2-adw-1', 'gir1.2-freedesktop', 'libglib2.0-bin', 'libglib2.0-data',
         'libgdk-pixbuf2.0-bin', 'librsvg2-common', 'fonts-dejavu-core',
         'pipewire-bin', 'libpipewire-0.3-modules', 'libspa-0.2-modules',
         'wl-clipboard', 'libnotify-bin', 'ca-certificates', 'shared-mime-info')
# libc and hardware drivers belong to the host. Bundling Mesa/LLVM blindly
# can break the host driver ABI; the portable UI defaults to Cairo rendering.
EXCLUDE = {'libc6', 'libc-bin', 'libgcc-s1', 'libstdc++6', 'libglx-mesa0',
           'libegl-mesa0', 'libgl1-mesa-dri', 'mesa-vulkan-drivers'}
TOOL_URL = 'https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage'
TOOL_SHA = 'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0'
RUNTIME_URL = 'https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64'
RUNTIME_SHA = '2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, path, expected):
    with urllib.request.urlopen(url, timeout=30) as response, path.open('xb') as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                raise ValueError('Build tool download exceeds size limit')
            output.write(chunk)
    if digest(path) != expected:
        raise ValueError(f'Build tool SHA256 mismatch: {path.name}')
    path.chmod(0o755)


def installed_closure():
    text = run('apt-cache', 'depends', '--recurse', '--installed', '--no-recommends',
               '--no-suggests', '--no-conflicts', '--no-breaks', '--no-replaces',
               '--no-enhances', *ROOTS, capture_output=True).stdout
    packages = {line.strip() for line in text.splitlines()
                if line and not line[0].isspace() and not line.startswith('<')}
    return sorted(p for p in packages if p.split(':')[0] not in EXCLUDE
                  and not p.startswith('libllvm'))


def copy_packages(appdir):
    packages = installed_closure()
    for package in packages:
        listing = run('dpkg-query', '-L', package, capture_output=True).stdout
        for name in listing.splitlines():
            source = Path(name)
            if not source.is_file():
                continue
            if name.startswith(('/usr/share/man/', '/usr/share/lintian/', '/usr/share/bug/')):
                continue
            if not name.startswith(('/usr/', '/lib/', '/etc/fonts/')):
                continue
            relative = name.lstrip('/')
            if relative.startswith('lib/'):
                relative = 'usr/' + relative
            target = appdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Dereference package alternatives/symlinks: no absolute link may
            # escape into the build host's Python or native library installation.
            if not target.exists():
                shutil.copy2(source, target, follow_symlinks=True)
    return run('dpkg-query', '-W', '-f=${Package}=${Version}\n', *packages, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    source, work = args.source, args.work
    appdir = work / 'WayVoice.AppDir'
    appdir.mkdir()
    package_versions = copy_packages(appdir)
    fonts = appdir / 'etc/fonts'
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / 'wayvoice.conf').write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig><dir>/usr/share/fonts</dir><dir prefix="relative">../../usr/share/fonts</dir><cachedir prefix="xdg">fontconfig</cachedir><include ignore_missing="yes">conf.d</include></fontconfig>')
    prefix = appdir / 'usr'
    target = prefix / 'lib/wayvoice/app'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'app', target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('wayvoice', 'wayvoice-settings', 'wayvoice-daemon', 'wayvoice-engine-setup'):
        shutil.copy2(source / 'scripts' / name, prefix / 'bin' / name)
    runtime = prefix / 'lib/wayvoice/runtime'
    site = runtime / 'lib/python3.12/site-packages'
    site.mkdir(parents=True)
    run('python3', '-m', 'pip', 'install', '--break-system-packages', '--only-binary=:all:',
        '--require-hashes', '--no-deps', '--target', str(site), '-r',
        str(source / 'packaging/appimage/runtime-requirements.txt'))
    (runtime / 'bin').mkdir()
    (runtime / 'bin/python').symlink_to('../../../../bin/python3.12')
    (runtime / '.engine-v1-ready').touch()
    sys.path.insert(0, str(source / 'app/src'))
    from wayvoice import parakeet_setup
    archive = work / 'parakeet.tar.gz'
    fetch(parakeet_setup.RUNTIME_URL, archive, parakeet_setup.RUNTIME_SHA256)
    with tarfile.open(archive) as bundle:
        bundle.extractall(work, filter='data')
    shutil.move(str(work / parakeet_setup.RUNTIME_NAME), prefix / 'lib/wayvoice/parakeet')
    for relative in ('applications', 'metainfo', 'icons/hicolor/scalable/apps', 'icons/hicolor/symbolic/apps'):
        (prefix / 'share' / relative).mkdir(parents=True, exist_ok=True)
    for ext, directory in (('desktop', 'applications'), ('metainfo.xml', 'metainfo')):
        name = 'io.github.stepan.WayVoice.' + ext
        shutil.copy2(source / 'data' / name, prefix / 'share' / directory / name)
    for relative in ('scalable/apps/io.github.stepan.WayVoice.svg', 'symbolic/apps/io.github.stepan.WayVoice-symbolic.svg'):
        shutil.copy2(source / 'data/icons/hicolor' / relative, prefix / 'share/icons/hicolor' / relative)
    run('glib-compile-schemas', str(prefix / 'share/glib-2.0/schemas'))
    # MIME caches are generated by dpkg triggers, not owned package files.
    run('update-mime-database', str(prefix / 'share/mime'))
    # Debian package copyright notices refer to these shared license texts.
    shutil.copytree('/usr/share/common-licenses', prefix / 'share/common-licenses',
                    dirs_exist_ok=True)
    docs = prefix / 'share/doc/wayvoice'
    run('python3', str(source / 'scripts/package-readme.py'), str(docs))
    shutil.copytree(source / 'packaging/appimage/licenses', docs / 'third-party')
    (prefix / 'share/wayvoice').mkdir(parents=True, exist_ok=True)
    shutil.copy2('/etc/ssl/certs/ca-certificates.crt', prefix / 'share/wayvoice/ca-certificates.crt')
    shutil.copy2(source / 'packaging/appimage/AppRun', appdir / 'AppRun')
    (appdir / 'AppRun').chmod(0o755)
    shutil.copy2(prefix / 'share/applications/io.github.stepan.WayVoice.desktop', appdir / 'io.github.stepan.WayVoice.desktop')
    run('rsvg-convert', '-o', str(appdir / 'io.github.stepan.WayVoice.png'),
        str(source / 'data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg'))
    (appdir / '.DirIcon').symlink_to('io.github.stepan.WayVoice.png')
    (appdir / 'BUILD_INFO.json').write_text(json.dumps({'version': args.version, 'commit': args.commit,
        'architecture': 'x86_64', 'glibc_minimum': '2.39', 'packages': package_versions.splitlines()}, indent=2)+'\n')
    # Prove GUI/resources/recognition imports before compressing the artifact.
    env = dict(os.environ, GTK_A11Y='none', GDK_BACKEND='x11')
    run('xvfb-run', '-a', 'dbus-run-session', '--', str(appdir / 'AppRun'), '--check-runtime', env=env)
    tool, embedded = work / 'appimagetool.AppImage', work / 'runtime-x86_64'
    fetch(TOOL_URL, tool, TOOL_SHA)
    fetch(RUNTIME_URL, embedded, RUNTIME_SHA)
    name = f'wayvoice-{args.version}-x86_64.AppImage'
    run(str(tool), '--runtime-file', str(embedded), str(appdir), str(work / name),
        env=dict(os.environ, APPIMAGE_EXTRACT_AND_RUN='1', ARCH='x86_64'))
    # Inspect the bytes actually shipped, not just the pre-compression tree.
    extracted = work / 'extracted with spaces'
    extracted.mkdir()
    run(str(work / name), '--appimage-extract', cwd=extracted, stdout=subprocess.DEVNULL)
    actual = extracted / 'squashfs-root'
    info = json.loads((actual / 'BUILD_INFO.json').read_text())
    if info['version'] != args.version or info['commit'] != args.commit:
        raise ValueError('Extracted AppImage identity mismatch')
    run('xvfb-run', '-a', 'dbus-run-session', '--', str(actual / 'AppRun'), '--check-runtime', env=env)
    print(f'Inspected AppImage: {work / name}', flush=True)


if __name__ == '__main__':
    main()
