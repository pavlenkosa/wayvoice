#!/usr/bin/env python3
"""Check public Markdown links and keep local engineering documents out of Git."""
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DOCS = {f'docs/{name}.md' for name in (
    'installation', 'getting-started', 'desktop-support', 'shortcuts-and-paste',
    'engines-and-models', 'troubleshooting', 'architecture', 'development', 'releasing')}


def check(root=ROOT):
    root = root.resolve()
    tracked = set(subprocess.check_output(
        ['git', '-C', str(root), 'ls-files', '-z'], text=True).split('\0')) - {''}
    errors = []
    private_names = {'AGENTS.md', 'ACCEPTANCE.md', 'ACCEPTANCE_CURRENT.md', 'PRODUCT_TASKS.md', 'PROJECT_TASKS.md'}
    for name in tracked:
        if (name.startswith('docs/') and name not in PUBLIC_DOCS) or Path(name).name in private_names:
            errors.append(f'Internal document tracked: {name}')
    documents = [root / name for name in ('README.md', 'CONTRIBUTING.md', 'SUPPORT.md', 'SECURITY.md')]
    documents += [root / name for name in sorted(PUBLIC_DOCS)]
    documents += list((root / 'releases').glob('*.md'))
    for document in documents:
        if not document.is_file():
            errors.append(f'Missing public page: {document.relative_to(root)}')
            continue
        for target in re.findall(r'\]\(([^)]+)\)', document.read_text()):
            target = target.split(' "', 1)[0].strip('<>')
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            path = (document.parent / unquote(url.path)).resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                errors.append(f'{document.relative_to(root)}: broken local link {target}')
            elif path.relative_to(root).as_posix() not in tracked:
                errors.append(f'{document.relative_to(root)}: local link target is not tracked: {target}')
    return errors


if __name__ == '__main__':
    failures = check()
    if failures:
        raise SystemExit('\n'.join(failures))
    print('Public documentation links and tracked document boundary OK')
