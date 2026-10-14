#!/usr/bin/env python3
"""Copy the public documentation and its local assets without internal notes."""
import argparse
from pathlib import Path
import shutil

PUBLIC_FILES = (
    'README.md', 'CHANGELOG.md', 'LICENSE', 'CONTRIBUTING.md', 'SUPPORT.md', 'SECURITY.md',
    'docs/installation.md', 'docs/getting-started.md', 'docs/desktop-support.md',
    'docs/shortcuts-and-paste.md', 'docs/engines-and-models.md', 'docs/troubleshooting.md',
    'docs/architecture.md', 'docs/development.md', 'docs/releasing.md',
    'releases/TEMPLATE.md', 'third_party/ydotool/README.wayvoice.md',
    'data/screenshots/home-en.png', 'data/screenshots/settings-en.png',
    'data/screenshots/setup-en.png',
    'data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg',
)


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
    parser.add_argument('destination', type=Path)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    copy_public_docs(args.source, args.destination)


if __name__ == '__main__':
    main()
