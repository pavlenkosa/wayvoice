"""The Debian package cannot claim ``Architecture: all``.

The package bundles the ydotool helper, which ``build-ydotool.sh`` compiles on the
build host, so the artifact is only runnable on the architecture it was built for.
An ``Architecture: all`` control file with native ELF inside is a lie dpkg will
happily install everywhere and then break on first use - which is what
`dpkg-deb -f dist/wayvoice_0.6.2_all.deb Architecture` reported as ``all`` while
``file`` on the two binaries said x86-64.

These tests read ``scripts/build-deb.sh`` rather than building the package: a real
build needs a compiler and dpkg tooling, and the suite must stay offline and fast.
They pin the substitution points that make the architecture specific; they cannot
catch a live build contradicting the script, which is what the CI "Inspect Debian
package" step and the local release gate assert for real.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build-deb.sh"


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


class ArchitectureMetadataTests(unittest.TestCase):
    def test_the_control_file_is_never_architecture_all(self):
        # Comment lines are stripped first: the script explains *why* the package can
        # never be "Architecture: all", and that prose must not trip the assertion.
        code = "\n".join(
            line for line in script_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotRegex(
            code, r"^Architecture: all\b",
            "the control file claims a package with native ELF is architecture-independent",
        )

    def test_the_architecture_comes_from_the_build_host(self):
        self.assertIn("dpkg --print-architecture", script_text(),
                      "no host-architecture detection")
        self.assertRegex(script_text(), r"Architecture:\s*\$\{DEB_ARCH\}",
                         "the control file must substitute the host architecture")

    def test_the_artifact_name_carries_the_architecture(self):
        text = script_text()
        self.assertRegex(text, r'OUT_NAME="wayvoice_\$\{VERSION\}_\$\{DEB_ARCH\}\.deb"',
                         "the artifact name must distinguish architectures")
        self.assertNotIn("_all.deb", text,
                         "a hardcoded all-architecture artifact name contradicts the control file")

    def test_the_checksum_file_uses_a_relative_path(self):
        # sha256sum used to write the absolute build-host path into the .sha256 file,
        # which made `sha256sum -c` fail everywhere except the original build host.
        match = re.search(r"sha256sum\s+\"\$OUT\"", script_text())
        self.assertIsNone(match, "the .sha256 file must not embed an absolute path")


if __name__ == "__main__":
    unittest.main()
