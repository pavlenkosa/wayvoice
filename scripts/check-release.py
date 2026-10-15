#!/usr/bin/env python3
"""Validate an existing release tag and human-authored notes without publishing."""
import json
import os
from pathlib import Path
import re
import subprocess


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def validate(root, tag, sha, main_ref='origin/main'):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Expected an explicit vMAJOR.MINOR.PATCH release tag')
    commit = git(root, 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}')
    if commit != sha or git(root, 'rev-parse', 'HEAD') != sha:
        raise ValueError('Tag, checkout and workflow commit must match')
    subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', commit, main_ref], check=True)
    version = subprocess.check_output(['python3', str(root / 'scripts/check-version.py'), tag[1:]], text=True).strip()
    notes = root / 'releases' / f'{version}.md'
    text = notes.read_text()
    if re.search(r'\b(?:TODO|TBD|PLACEHOLDER)\b', text, re.I):
        raise ValueError('Release notes contain placeholders')
    for heading in ('Highlights', 'Compatibility', 'Downloads', 'Known limitations'):
        match = re.search(rf'^## {heading}[ \t]*\n(.*?)(?=^## |\Z)', text, re.M | re.S)
        if not match or not match.group(1).strip():
            raise ValueError(f'Missing substantive release notes: {heading}')
    channel = json.loads((root / '.github/release-policy.json').read_text())['channel']
    if channel not in ('alpha', 'beta', 'stable'):
        raise ValueError('Unknown release channel')
    return {'release': 'true', 'version': version, 'tag': tag, 'tag_sha': sha,
            'prerelease': str(channel != 'stable' or '-' in version).lower(),
            'notes': f'releases/{version}.md'}


def main():
    if os.environ.get('GITHUB_REF_TYPE') != 'tag':
        raise SystemExit('Publication is tag-only')
    values = validate(Path(__file__).resolve().parent.parent,
                      os.environ['GITHUB_REF_NAME'], os.environ['GITHUB_SHA'])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.writelines(f'{key}={value}\n' for key, value in values.items())


if __name__ == '__main__':
    main()
