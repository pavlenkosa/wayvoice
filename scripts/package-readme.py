#!/usr/bin/env python3
"""Copy the public documentation and its local assets without internal notes."""
import argparse
import json
from pathlib import Path
import shutil

PUBLIC_FILES = tuple(json.loads(
    (Path(__file__).resolve().parent.parent / 'packaging/public-docs.json').read_text()))


def copy_public_docs(root, destination):
    root = Path(root).resolve()
    destination = Path(destination)
    # Validate every input before writing. Never follow a source symlink into local data.
    for name in PUBLIC_FILES:
        source = root / name
        if not source.is_file() or source.resolve() != source:
            raise ValueError(f'Missing or symlinked public documentation: {name}')
    for name in PUBLIC_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
        target.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, nargs='?')
    parser.add_argument('--source-files', action='store_true', help='List public files outside the source directories already archived by RPM')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if args.source_files:
        print('\n'.join(name for name in PUBLIC_FILES if Path(name).parts[0] not in {'data', 'third_party'}))
    elif args.destination is None:
        parser.error('destination is required unless --source-files is used')
    else:
        copy_public_docs(args.source, args.destination)


if __name__ == '__main__':
    main()
