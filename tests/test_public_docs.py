"""Public documentation must work from the tracked source snapshot."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_docs', ROOT / 'scripts/check-public-docs.py')
public_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_docs)


class PublicDocumentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        for name in ('README.md', 'CONTRIBUTING.md', 'SUPPORT.md', 'SECURITY.md', *public_docs.PUBLIC_DOCS):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Public documentation.\n')
        self.track('.')

    def track(self, name):
        subprocess.run(['git', '-C', str(self.root), 'add', name], check=True)

    def test_link_to_ignored_internal_document_is_rejected(self):
        (self.root / '.gitignore').write_text('/docs/ACCEPTANCE_CURRENT.md\n')
        (self.root / 'docs/ACCEPTANCE_CURRENT.md').write_text('Private checkpoint.\n')
        (self.root / 'README.md').write_text('[Checkpoint](docs/ACCEPTANCE_CURRENT.md)\n')
        self.assertEqual(public_docs.check(self.root), [
            'README.md: local link target is not tracked: docs/ACCEPTANCE_CURRENT.md'])

    def test_link_to_untracked_asset_is_rejected(self):
        (self.root / 'preview.png').write_bytes(b'local preview')
        (self.root / 'README.md').write_text('![Preview](preview.png)\n')
        self.assertEqual(public_docs.check(self.root), [
            'README.md: local link target is not tracked: preview.png'])

    def test_tracked_relative_targets_with_fragment_and_escaped_path_are_valid(self):
        asset = self.root / 'data/screen shot.png'
        asset.parent.mkdir()
        asset.write_bytes(b'public preview')
        self.track('data/screen shot.png')
        (self.root / 'README.md').write_text(
            '[Languages](docs/engines-and-models.md#supported-languages)\n'
            '![Preview](data/screen%20shot.png)\n')
        (self.root / 'docs/installation.md').write_text('[Home](../README.md)\n')
        self.assertEqual(public_docs.check(self.root), [])

    def test_missing_target_remains_a_broken_link(self):
        (self.root / 'README.md').write_text('[Missing](absent.md)\n')
        self.assertEqual(public_docs.check(self.root), ['README.md: broken local link absent.md'])

    def test_tracked_internal_document_is_rejected(self):
        (self.root / 'docs/ACCEPTANCE_CURRENT.md').write_text('Private checkpoint.\n')
        self.track('docs/ACCEPTANCE_CURRENT.md')
        self.assertIn('Internal document tracked: docs/ACCEPTANCE_CURRENT.md', public_docs.check(self.root))
