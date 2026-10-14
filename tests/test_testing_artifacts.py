"""Testing packages must identify their source and reject stale/tampered inputs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('testing_artifacts', Path(__file__).resolve().parents[1] / 'scripts/stage-testing-artifacts.py')
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


class TestingArtifactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'dist'
        self.source.mkdir()
        self.output = self.root / 'testing'
        self.commit = 'a' * 40
        for extension in ('deb', 'rpm', 'flatpak'):
            path = self.source / f'wayvoice-1.2.3.{extension}'
            path.write_bytes(extension.encode())
            path.with_name(path.name + '.sha256').write_text(f'{artifacts.sha256(path)}  {path.name}\n')

    def stage(self):
        artifacts.stage(self.source, self.output, '1.2.3', self.commit, 'https://example.com/run/1')

    def test_renamed_packages_have_valid_checksums_and_provenance(self):
        self.stage()
        info = json.loads((self.output / 'BUILD_INFO.json').read_text())
        self.assertEqual(info['commit'], self.commit)
        self.assertEqual(info['channel'], 'testing')
        self.assertEqual(info['version'], '1.2.3')
        self.assertEqual(info['ref'], 'refs/heads/develop')
        self.assertEqual(len(info['packages']), 3)
        for line in (self.output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split()
            self.assertEqual(artifacts.sha256(self.output / name), digest)
        for package in info['packages']:
            self.assertIn('-testing-' + self.commit[:12], package['file'])
            name = package['file']
            self.assertEqual((self.output / (name + '.sha256')).read_text(), f"{package['sha256']}  {name}\n")

    def test_modified_package_is_rejected_before_publication(self):
        (self.source / 'wayvoice-1.2.3.deb').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_missing_or_duplicate_format_is_rejected(self):
        path = self.source / 'wayvoice-1.2.3.rpm'
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'exactly one rpm'):
            self.stage()
        path.touch()
        (self.source / 'old.rpm').touch()
        with self.assertRaisesRegex(ValueError, 'exactly one rpm'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        marker = self.output / 'user-file'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.stage()
        self.assertEqual(marker.read_text(), 'keep')

    def test_failed_copy_removes_only_its_owned_output(self):
        from unittest import mock
        with mock.patch.object(artifacts.shutil, 'copyfile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.stage()
        self.assertFalse(self.output.exists())
        self.assertTrue(self.source.exists())
