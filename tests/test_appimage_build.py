"""AppImage identity, portable contents and safe artifact publication."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('appimage_build', Path(__file__).resolve().parents[1] / 'scripts/build-appimage.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        (self.output / 'BUILD_INFO.json').write_text(json.dumps({'version':'1.2.3','commit':'a'*40,'architecture':'x86_64'}))
        self.runtime = self.output / 'usr/lib/wayvoice/runtime'
        self.runtime.mkdir(parents=True)
        (self.runtime / '.engine-v1-ready').touch()
        for name in ('AppRun','usr/bin/python3.12','usr/bin/wayvoice','usr/bin/wayvoice-settings','usr/bin/wl-copy','usr/bin/pw-record','usr/lib/wayvoice/parakeet/bin/nemo-speech'):
            path=self.output/name; path.parent.mkdir(parents=True,exist_ok=True); path.touch(); path.chmod(0o755)

        for name in ('usr/share/common-licenses/LGPL-2.1', 'usr/share/doc/wayvoice/third-party/type2-runtime/LICENSE'):
            path = self.output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('license')

    def test_missing_license_rejected(self):
        (self.output / 'usr/share/common-licenses/LGPL-2.1').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing license'):
            build.validate_tree(self.output, '1.2.3')

    def test_valid_tree(self):
        build.validate_tree(self.output,'1.2.3','a'*40)

    def test_wrong_version_commit_or_architecture_rejected(self):
        for key,value in [('version','9.9.9'),('commit','b'*40),('architecture','aarch64')]:
            info={'version':'1.2.3','commit':'a'*40,'architecture':'x86_64'}
            info[key]=value
            (self.output/'BUILD_INFO.json').write_text(json.dumps(info))
            with self.assertRaises(ValueError): build.validate_tree(self.output,'1.2.3','a'*40)

    def test_missing_runtime_or_launcher_rejected(self):
        (self.runtime/'.engine-v1-ready').unlink()
        with self.assertRaisesRegex(ValueError,'runtime is not ready'): build.validate_tree(self.output,'1.2.3')
        (self.runtime/'.engine-v1-ready').touch()
        os.chmod(self.output/'usr/bin/wl-copy',0o644)
        with self.assertRaisesRegex(ValueError,'Missing executable'): build.validate_tree(self.output,'1.2.3')

    def test_private_files_and_external_links_rejected(self):
        private=self.output/'AGENTS.md'; private.touch()
        with self.assertRaisesRegex(ValueError,'Private'): build.validate_tree(self.output,'1.2.3')
        private.unlink()
        (self.output/'host-python').symlink_to('/usr/bin/python3')
        with self.assertRaisesRegex(ValueError,'External symlink'): build.validate_tree(self.output,'1.2.3')


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
        bundle = dist / 'wayvoice-1.2.3-x86_64.AppImage'
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
        destination = self.root / 'release.AppImage'
        digest = build.publish_bundle(bundle, destination)
        self.assertEqual(digest, hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual((self.root / 'release.AppImage.sha256').read_text(), f'{digest}  release.AppImage\n')
        self.assertEqual(list(self.root.glob('.appimage-*')), [])

    def test_copy_failure_never_exposes_partial_bundle(self):
        from unittest import mock
        source = self.root / 'source'
        source.write_bytes(b'full')
        destination = self.root / 'release.AppImage'
        def fail_copy(origin, target):
            target.write(b'partial')
            raise OSError('disk full')
        with mock.patch.object(build.shutil, 'copyfileobj', side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, 'disk full'):
                build.publish_bundle(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.appimage-*')), [])

    def test_existing_checksum_preserved_on_publication_race(self):
        source = self.root / 'source'
        source.write_bytes(b'full')
        destination = self.root / 'release.AppImage'
        checksum = self.root / 'release.AppImage.sha256'
        checksum.write_text('previous')
        with self.assertRaises(FileExistsError):
            build.publish_bundle(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(checksum.read_text(), 'previous')

    def test_lock_covers_native_dependency_versions_with_sha256(self):
        import re
        root = Path(__file__).resolve().parents[1]
        native = (root / 'app/src/wayvoice/runtime-requirements.txt').read_text()
        lock = (root / 'packaging/appimage/runtime-requirements.txt').read_text()
        def pins(text):
            return {name.lower().replace('_', '-'): version for name, version in re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)', text, re.M)}
        self.assertEqual(pins(native), pins(lock))
        for line in lock.splitlines():
            if line and not line.startswith('#'):
                self.assertRegex(line, r'^\S+==\S+ --hash=sha256:[a-f0-9]{64}$')
