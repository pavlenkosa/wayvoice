import unittest
from pathlib import Path
from unittest import mock

from wayvoice import deps
from wayvoice.i18n import _EN, _RU

ROOT = Path(__file__).resolve().parents[1]

# A name that cannot exist in PATH, used to drive the "missing" branches.
IMPOSSIBLE = "wayvoice-definitely-missing-binary"


def _fake_which(present: set[str]):
    """Return a shutil.which stand-in that only knows ``present``."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _which_returning(mapping):
    def _which(name):
        return mapping.get(name)
    return _which


class RegistryTests(unittest.TestCase):
    def test_registry_is_not_empty(self):
        self.assertTrue(deps.dependencies())

    def test_ids_are_unique_and_non_empty(self):
        ids = [dep.id for dep in deps.dependencies()]
        for dep_id in ids:
            self.assertTrue(dep_id.strip(), "empty dependency id")
        self.assertEqual(len(ids), len(set(ids)), "duplicate dependency ids")

    def test_expected_dependencies_present(self):
        ids = [dep.id for dep in deps.dependencies()]
        for expected in ("pipewire", "wl-clipboard", "notify", "ydotool"):
            self.assertIn(expected, ids)

    def test_ydotool_requires_both_binaries(self):
        dep = deps.get("ydotool")
        self.assertIsNotNone(dep)
        self.assertEqual(dep.binaries, ("ydotool", "ydotoold"))

    def test_every_entry_has_binaries_and_purpose_key(self):
        for dep in deps.dependencies():
            self.assertTrue(dep.binaries, f"{dep.id} has no binaries")
            self.assertTrue(dep.purpose_key, f"{dep.id} has no purpose_key")
            self.assertIsInstance(dep.required, bool)

    def test_purpose_key_is_translated_in_both_dictionaries(self):
        for dep in deps.dependencies():
            self.assertIn(dep.purpose_key, _RU, f"{dep.id}: no Russian purpose text")
            self.assertIn(dep.purpose_key, _EN, f"{dep.id}: no English purpose text")

    def test_missing_message_is_translated_in_both_dictionaries(self):
        self.assertIn("deps.missing", _RU)
        self.assertIn("deps.missing", _EN)

    def test_blocking_dependencies_come_first(self):
        required_flags = [dep.required for dep in deps.dependencies()]
        # Once an optional entry appears, no blocking one may follow it.
        seen_optional = False
        for flag in required_flags:
            if not flag:
                seen_optional = True
            elif seen_optional:
                self.fail("a required dependency follows an optional one")

    def test_registry_order_is_stable(self):
        self.assertEqual([d.id for d in deps.dependencies()], [d.id for d in deps.DEPENDENCIES])

    def test_package_names_are_plain_strings(self):
        for dep in deps.dependencies():
            for manager, name in dep.packages.items():
                self.assertIsInstance(name, str)
                self.assertTrue(name.strip())
                self.assertNotIn(" ", name, f"{dep.id}/{manager}: package name looks wrong")


class StatusTests(unittest.TestCase):
    def test_all_binaries_present(self):
        dep = deps.Dependency(
            id="fake", label="fake", binaries=("a-tool", "b-tool"),
            required=True, purpose_key="deps.pipewire", packages={},
        )
        with mock.patch("shutil.which", _fake_which({"a-tool", "b-tool"})):
            state = deps.status_of(dep)
        self.assertTrue(state["ok"])
        self.assertTrue(state["found"])
        self.assertEqual(state["missing"], ())
        self.assertEqual(state["binary_path"], "/usr/bin/a-tool")

    def test_all_binaries_missing(self):
        dep = deps.Dependency(
            id="fake", label="fake", binaries=(IMPOSSIBLE,),
            required=True, purpose_key="deps.pipewire", packages={},
        )
        with mock.patch("shutil.which", _fake_which(set())):
            state = deps.status_of(dep)
        self.assertFalse(state["ok"])
        self.assertFalse(state["found"])
        self.assertEqual(state["missing"], (IMPOSSIBLE,))
        self.assertIsNone(state["binary_path"])

    def test_partially_missing_is_not_ok(self):
        dep = deps.Dependency(
            id="fake", label="fake", binaries=("ydotool", "ydotoold"),
            required=True, purpose_key="deps.ydotool", packages={},
        )
        with mock.patch("shutil.which", _fake_which({"ydotool"})):
            state = deps.status_of(dep)
        self.assertTrue(state["found"], "at least one binary is present")
        self.assertFalse(state["ok"], "a half-installed dependency is not usable")
        self.assertEqual(state["missing"], ("ydotoold",))

    def test_status_all_order_matches_registry(self):
        rows = deps.status_all()
        self.assertEqual([row["id"] for row in rows], [dep.id for dep in deps.dependencies()])

    def test_status_all_keys(self):
        expected = {"id", "label", "purpose_key", "required", "found", "missing", "binary_path"}
        for row in deps.status_all():
            self.assertEqual(set(row), expected)

    def test_status_all_reports_missing_binary(self):
        with mock.patch("shutil.which", _fake_which(set())):
            rows = deps.status_all()
            required_missing = deps.missing_required()
        self.assertTrue(all(not row["found"] for row in rows))
        self.assertTrue(all(row["missing"] for row in rows))
        blocking = [row["id"] for row in deps.status_all() if row["required"]]
        self.assertEqual([row["id"] for row in required_missing], blocking)

    def test_missing_helpers_split_required_and_optional(self):
        # Only pw-record exists: the required pipewire entry stays missing while
        # the optional notify entry is satisfied.
        with mock.patch("shutil.which", _which_returning({"pw-record": "/usr/bin/pw-record"})):
            required_ids = [row["id"] for row in deps.missing_required()]
            optional_ids = [row["id"] for row in deps.missing_optional()]
        self.assertEqual(required_ids, ["wl-clipboard"])
        self.assertEqual(optional_ids, ["notify", "ydotool"])

    def test_missing_helpers_empty_when_everything_present(self):
        present = {name for dep in deps.dependencies() for name in dep.binaries}
        with mock.patch("shutil.which", _fake_which(present)):
            self.assertEqual(deps.missing_required(), [])
            self.assertEqual(deps.missing_optional(), [])


class DescribeMissingTests(unittest.TestCase):
    def test_describe_missing_lists_binaries_and_purpose(self):
        with mock.patch("shutil.which", _fake_which(set())):
            text = deps.describe_missing("pipewire", "en")
        self.assertIn("pw-record", text)
        self.assertIn(_EN["deps.pipewire"], text)

    def test_describe_missing_mentions_every_missing_binary(self):
        with mock.patch("shutil.which", _fake_which({"ydotool"})):
            text = deps.describe_missing("ydotool", "en")
        self.assertIn("ydotoold", text)
        self.assertNotIn("ydotool ", text.split("ydotoold")[0])

    def test_describe_missing_accepts_a_dependency_object(self):
        dep = deps.get("notify")
        with mock.patch("shutil.which", _fake_which(set())):
            self.assertEqual(deps.describe_missing(dep, "en"), deps.describe_missing("notify", "en"))

    def test_describe_missing_empty_when_present(self):
        present = {name for dep in deps.dependencies() for name in dep.binaries}
        with mock.patch("shutil.which", _fake_which(present)):
            self.assertEqual(deps.describe_missing("pipewire", "en"), "")

    def test_describe_missing_does_not_name_a_package(self):
        # The whole point of the module: the text depends on what is absent,
        # never on a distribution specific package name.
        with mock.patch("shutil.which", _fake_which(set())):
            text = deps.describe_missing("pipewire", "en")
        for name in ("pipewire-bin", "pipewire-utils", "apt", "dnf"):
            self.assertNotIn(name, text)

    def test_unknown_id_returns_empty(self):
        self.assertEqual(deps.describe_missing("wayvoice-nonexistent-id", "en"), "")


if __name__ == "__main__":
    unittest.main()
