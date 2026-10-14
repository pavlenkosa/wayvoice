#!/usr/bin/env python3
"""Label verified CI packages as testing without changing installed versions."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def stage(source, output, version, commit, run_url=''):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected a full source commit SHA')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Invalid project version')
    packages = []
    for extension in ('deb', 'rpm', 'flatpak'):
        matches = list(source.glob(f'*.{extension}'))
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one {extension} artifact')
        path = matches[0]
        if path.is_symlink() or not path.is_file():
            raise ValueError('Expected a regular package file')
        digest = sha256(path)
        checksum = path.with_name(path.name + '.sha256').read_text().split()
        if len(checksum) != 2 or checksum != [digest, path.name]:
            raise ValueError(f'Checksum mismatch: {path.name}')
        packages.append((path, digest))
    output.mkdir(parents=True, exist_ok=False)
    try:
        records = []
        sums = []
        for path, digest in packages:
            name = f'{path.stem}-testing-{commit[:12]}{path.suffix}'
            target = output / name
            shutil.copyfile(path, target)
            if sha256(target) != digest:
                raise ValueError(f'Copy checksum mismatch: {path.name}')
            (output / (name + '.sha256')).write_text(f'{digest}  {name}\n')
            sums.append(f'{digest}  {name}\n')
            records.append({'file': name, 'sha256': digest})
        metadata = output / 'BUILD_INFO.json'
        metadata.write_text(json.dumps({'channel': 'testing', 'version': version,
                                       'commit': commit, 'ref': 'refs/heads/develop',
                                       'run_url': run_url, 'packages': records,
                                       'installed_identity': 'unchanged'}, indent=2) + '\n')
        sums.append(f'{sha256(metadata)}  {metadata.name}\n')
        (output / 'SHA256SUMS').write_text(''.join(sums))
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('dist'))
    parser.add_argument('--output', type=Path, default=Path('build/testing-artifacts'))
    parser.add_argument('--run-url', default='')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    version = subprocess.check_output(['python3', str(root / 'scripts/check-version.py')], text=True).strip()
    stage(args.source, args.output, version, commit, args.run_url)


if __name__ == '__main__':
    main()
