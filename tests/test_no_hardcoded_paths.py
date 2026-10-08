import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Literals that would tie the project to a single installation prefix.
# Note: scripts/build-deb.sh is intentionally not scanned — it *builds* the
# /usr/lib/wayvoice layout and must name it literally.
FORBIDDEN = (
    "/usr/lib/wayvoice",
    "/usr/bin/wayvoice",
    "/usr/bin/ydotool",
    "/usr/bin/python3",
)

GLOBS = (
    "app/src/wayvoice/*.py",
    # UI-ARCH-001: the ui package (pages/controllers/widgets/dialogs) too.
    "app/src/wayvoice/ui/**/*.py",
    "systemd/*.service",
    "data/*.desktop",
)


def scanned_files() -> list[Path]:
    """Return every file the prefix guard applies to.

    Sources, units and desktop entries by glob, plus the executable
    extension-less wrappers in scripts/ (*.sh helpers are excluded).
    """
    files: list[Path] = []
    for pattern in GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    scripts = ROOT / "scripts"
    files.extend(p for p in sorted(scripts.iterdir()) if p.is_file() and not p.suffix)
    return files


class NoHardcodedPathsTests(unittest.TestCase):
    def test_no_hardcoded_install_prefix(self):
        offenders = []
        for path in scanned_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                for needle in FORBIDDEN:
                    if needle in line:
                        offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "hardcoded installation prefix:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
