"""Installed documentation is self-contained and never copies internal notes."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_readme', ROOT / 'scripts/package-readme.py')
package_readme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_readme)


class PackagedDocumentationTests(unittest.TestCase):
    def test_real_public_documentation_links_and_images_resolve_in_package(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            package_readme.copy_public_docs(ROOT, destination)
            self.assertEqual(
                {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()},
                set(package_readme.PUBLIC_FILES))
            for document in destination.rglob('*.md'):
                text = document.read_text()
                targets = re.findall(r'\]\(([^)]+)\)', text)
                targets += re.findall(r'\bsrc="([^"]+)"', text)
                for target in targets:
                    url = urlsplit(target)
                    if url.scheme or url.netloc or not url.path:
                        continue
                    with self.subTest(document=document.relative_to(destination), target=target):
                        resolved = (document.parent / unquote(url.path)).resolve()
                        self.assertTrue(resolved.is_relative_to(destination))
                        self.assertTrue(resolved.is_file())

    def test_only_allowlisted_files_are_copied_from_source_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            destination = Path(directory) / 'package'
            for name in package_readme.PUBLIC_FILES:
                source = root / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('Public fixture')
            (root / 'docs/ACCEPTANCE_CURRENT.md').write_text('Private checkpoint')
            (root / 'docs/private.pem').write_text('Private data')
            package_readme.copy_public_docs(root, destination)
            self.assertFalse((destination / 'docs/ACCEPTANCE_CURRENT.md').exists())
            self.assertFalse((destination / 'docs/private.pem').exists())
            self.assertTrue((destination / 'README.md').is_file())

    def test_symlinked_input_is_rejected_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            root.mkdir()
            destination = Path(directory) / 'package'
            secret = Path(directory) / 'secret'
            secret.write_text('Private data')
            (root / 'README.md').symlink_to(secret)
            with self.assertRaisesRegex(ValueError, 'symlinked'):
                package_readme.copy_public_docs(root, destination)
            self.assertFalse(destination.exists())
