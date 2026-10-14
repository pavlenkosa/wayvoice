"""Artifact inspection must reject mismatched or private release contents."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('flatpak_build', Path(__file__).resolve().parents[1] / 'scripts/build-flatpak.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        (self.output / 'metadata').write_text('[Application]\nname=io.github.stepan.WayVoice\nruntime=org.gnome.Platform/x86_64/50\nsdk=org.gnome.Sdk/x86_64/50\ncommand=wayvoice-settings\n')
        self.files = self.output / 'files'
        init = self.files / 'lib/wayvoice/app/src/wayvoice/__init__.py'
        init.parent.mkdir(parents=True)
        init.write_text('__version__ = "1.2.3"\n')
        self.runtime = self.files / 'lib/wayvoice/runtime'
        self.runtime.mkdir()
        (self.runtime / '.engine-v1-ready').touch()
        (self.files / 'bin').mkdir()
        for name in ('wayvoice', 'wayvoice-settings', 'wayvoice-daemon', 'wl-copy', 'wl-paste'):
            path = self.files / 'bin' / name
            path.touch()
            path.chmod(0o755)

        parakeet = self.files / 'lib/wayvoice/parakeet/bin/nemo-speech'
        parakeet.parent.mkdir(parents=True)
        parakeet.touch()
        parakeet.chmod(0o755)

    def test_missing_parakeet_runtime_rejected(self):
        (self.files / 'lib/wayvoice/parakeet/bin/nemo-speech').unlink()
        with self.assertRaisesRegex(ValueError, 'Parakeet'):
            build.validate_tree(self.output, '1.2.3')

    def test_valid_tree(self):
        build.validate_tree(self.output, '1.2.3')

    def test_wrong_version_rejected(self):
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            build.validate_tree(self.output, '9.9.9')

    def test_metadata_mismatch_rejected(self):
        path = self.output / 'metadata'
        for old, new in [('x86_64', 'aarch64'), ('wayvoice-settings', 'wrong'), ('/50', '/49')]:
            original = path.read_text()
            path.write_text(original.replace(old, new))
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, 'Unexpected Flatpak'):
                build.validate_tree(self.output, '1.2.3')
            path.write_text(original)

    def test_private_files_rejected(self):
        for name in ('AGENTS.md', '.env', 'secret.key', 'ACCEPTANCE_CURRENT.md'):
            path = self.files / name
            path.touch()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Private/build file'):
                build.validate_tree(self.output, '1.2.3')
            path.unlink()

    def test_missing_runtime_or_launcher_rejected(self):
        (self.runtime / '.engine-v1-ready').unlink()
        with self.assertRaisesRegex(ValueError, 'runtime is not ready'):
            build.validate_tree(self.output, '1.2.3')
        (self.runtime / '.engine-v1-ready').touch()
        os.chmod(self.files / 'bin/wl-copy', 0o644)
        with self.assertRaisesRegex(ValueError, 'Missing executable'):
            build.validate_tree(self.output, '1.2.3')


class BuildSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_dirty_tree_refused_before_any_build(self):
        from unittest import mock
        import subprocess
        with mock.patch.object(build, 'run', return_value=subprocess.CompletedProcess([], 0, ' M tracked\n')) as run:
            with self.assertRaisesRegex(SystemExit, 'clean tracked'):
                build.main(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / 'build').exists())

    def test_existing_bundle_refused_before_any_build(self):
        from unittest import mock
        import subprocess
        dist = self.root / 'dist'
        dist.mkdir()
        bundle = dist / 'wayvoice-1.2.3-x86_64.flatpak'
        bundle.write_bytes(b'previous build')
        with mock.patch.object(build, 'run', side_effect=[subprocess.CompletedProcess([], 0, ''), subprocess.CompletedProcess([], 0, '1.2.3\n')]) as run:
            with self.assertRaisesRegex(SystemExit, 'Refusing to replace'):
                build.main(self.root)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(bundle.read_bytes(), b'previous build')
        self.assertFalse((self.root / 'build').exists())

    def test_publication_writes_matching_hash(self):
        import hashlib
        bundle = self.root / 'source'
        bundle.write_bytes(b'complete bundle')
        destination = self.root / 'release.flatpak'
        digest = build.publish_bundle(bundle, destination)
        self.assertEqual(digest, hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual((self.root / 'release.flatpak.sha256').read_text(), f'{digest}  release.flatpak\n')
        self.assertEqual(list(self.root.glob('.flatpak-*')), [])

    def test_copy_failure_never_exposes_partial_bundle(self):
        from unittest import mock
        source = self.root / 'source'
        source.write_bytes(b'full')
        destination = self.root / 'release.flatpak'
        def fail_copy(origin, target):
            target.write(b'partial')
            raise OSError('disk full')
        with mock.patch.object(build.shutil, 'copyfileobj', side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, 'disk full'):
                build.publish_bundle(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.flatpak-*')), [])

    def test_existing_checksum_preserved_on_publication_race(self):
        source = self.root / 'source'
        source.write_bytes(b'full')
        destination = self.root / 'release.flatpak'
        checksum = self.root / 'release.flatpak.sha256'
        checksum.write_text('previous')
        with self.assertRaises(FileExistsError):
            build.publish_bundle(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(checksum.read_text(), 'previous')

    def test_lock_covers_native_dependency_versions_with_sha256(self):
        import re
        root = Path(__file__).resolve().parents[1]
        native = (root / 'app/src/wayvoice/runtime-requirements.txt').read_text()
        lock = (root / 'packaging/flatpak/runtime-requirements.txt').read_text()
        def pins(text):
            return {name.lower().replace('_', '-'): version for name, version in re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)', text, re.M)}
        self.assertEqual(pins(native), pins(lock))
        for line in lock.splitlines():
            if line and not line.startswith('#'):
                self.assertRegex(line, r'^\S+==\S+ --hash=sha256:[a-f0-9]{64}$')
