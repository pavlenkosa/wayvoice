"""The Flatpak manifest checker asserts the structure the Flatpak needs.

The release gate used to check only that ``io.github.stepan.WayVoice.json`` was
JSON with the right id and runtime. A manifest that lost its wayvoice module,
its pinned wl-clipboard commit or a permission the application cannot work
without still parsed and still passed. ``scripts/check-flatpak-manifest.py``
makes those assertions; these tests make sure the checker itself catches the
regressions it exists for, by mutating a copy of the real manifest.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check-flatpak-manifest.py"
MANIFEST = REPO / "io.github.stepan.WayVoice.json"


def run_checker(manifest_text: str) -> subprocess.CompletedProcess:
    """Run the checker against manifest_text as the repository manifest.

    The checker resolves the manifest path relative to itself, so a copy of the
    tree is the honest way to feed it a mutated manifest without touching the
    real file. The copy is minimal: the script and the manifest.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "check-flatpak-manifest.py").write_bytes(
            CHECKER.read_bytes())
        (root / "io.github.stepan.WayVoice.json").write_text(
            manifest_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(root / "scripts" / "check-flatpak-manifest.py")],
            capture_output=True, text=True,
        )


def mutate(**changes) -> str:
    """The real manifest with one structural change applied."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if "drop_module" in changes:
        data["modules"] = [
            m for m in data["modules"] if m.get("name") != changes["drop_module"]
        ]
    if "drop_finish_arg" in changes:
        data["finish-args"].remove(changes["drop_finish_arg"])
    if "set" in changes:
        data.update(changes["set"])
    if "unpin_commit" in changes:
        for module in data["modules"]:
            if module.get("name") == "wl-clipboard":
                del module["sources"][0]["commit"]
    return json.dumps(data, indent=2)


class ManifestCheckerTests(unittest.TestCase):
    def test_the_real_manifest_passes(self):
        result = run_checker(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0,
                         f"the shipped manifest failed its own checker: {result.stderr}")

    def test_a_lost_wayvoice_module_is_caught(self):
        result = run_checker(mutate(drop_module="wayvoice"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("wayvoice module", result.stderr)

    def test_an_unpinned_wl_clipboard_is_caught(self):
        result = run_checker(mutate(unpin_commit=True))
        self.assertEqual(result.returncode, 1)
        self.assertIn("pinned", result.stderr)

    def test_a_lost_permission_is_caught(self):
        result = run_checker(
            mutate(drop_finish_arg="--filesystem=xdg-run/pipewire-0"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing finish-args", result.stderr)

    def test_a_wrong_launch_command_is_caught(self):
        result = run_checker(mutate(set={"command": "wayvoice-daemon"}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("command", result.stderr)

    def test_invalid_json_is_caught(self):
        result = run_checker("{not json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("JSON", result.stderr)


if __name__ == "__main__":
    unittest.main()
