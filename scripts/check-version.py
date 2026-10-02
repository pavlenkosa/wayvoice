#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_init_version() -> str:
    tree = ast.parse((ROOT / "app/src/wayvoice/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return ast.literal_eval(node.value)
    raise SystemExit("Could not find __version__ in app/src/wayvoice/__init__.py")


def read_pyproject_version() -> str:
    with (ROOT / "app/pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return str(data["project"]["version"])


def read_metainfo_version() -> str:
    root = ET.parse(ROOT / "data/io.github.stepan.WayVoice.metainfo.xml").getroot()
    releases = root.find("releases")
    if releases is None or not list(releases):
        raise SystemExit("No <release> entries in AppStream metainfo")
    return list(releases)[0].attrib["version"]


def main() -> int:
    expected = sys.argv[1].removeprefix("v") if len(sys.argv) > 1 else None
    versions = {
        "app": read_init_version(),
        "pyproject": read_pyproject_version(),
        "metainfo": read_metainfo_version(),
    }

    unique = set(versions.values())
    if len(unique) != 1:
        print("Version mismatch inside repository:", file=sys.stderr)
        for name, value in versions.items():
            print(f"  {name}: {value}", file=sys.stderr)
        return 1

    version = next(iter(unique))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.-]+)?", version):
        print(f"Invalid project version: {version}", file=sys.stderr)
        return 1

    if expected and version != expected:
        print(
            f"Git tag/version mismatch: tag expects {expected}, project is {version}",
            file=sys.stderr,
        )
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
