#!/usr/bin/env python3
"""Validate the portable build recipe and immutable recognition/tool inputs."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def main():
    helper = ROOT / 'packaging/appimage/build-appdir.py'
    values = {}
    for node in ast.parse(helper.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name): values[target.id] = node.value.value
    for prefix in ('TOOL', 'RUNTIME'):
        url, sha = values[prefix + '_URL'], values[prefix + '_SHA']
        if not url.startswith('https://github.com/AppImage/') or '/continuous/' in url or '/latest/' in url:
            raise ValueError('AppImage tools must use immutable official release URLs')
        if not re.fullmatch(r'[a-f0-9]{64}', sha): raise ValueError('Missing tool SHA256')
    def pins(text):
        return dict(re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)', text, re.M))
    native = (ROOT / 'app/src/wayvoice/runtime-requirements.txt').read_text()
    lock = (ROOT / 'packaging/appimage/runtime-requirements.txt').read_text()
    if pins(native) != pins(lock): raise ValueError('Portable recognition dependency versions differ from native')
    for line in lock.splitlines():
        if line and not line.startswith('#') and not re.fullmatch(r'\S+==\S+ --hash=sha256:[a-f0-9]{64}', line):
            raise ValueError('Recognition dependencies must be pinned with SHA256')
    if not (ROOT / 'packaging/appimage/AppRun').read_text().startswith('#!/bin/sh\n'):
        raise ValueError('Missing AppImage entry point')
    print('AppImage recipe and immutable runtime/tool inputs OK')


if __name__ == '__main__':
    main()
