"""Release validation is local and never creates or changes a tag."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'scripts/check-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleasePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        for folder in ('scripts', '.github', 'releases'):
            (self.root / folder).mkdir()
        (self.root / 'scripts/check-version.py').write_text('print("1.2.3")\n')
        (self.root / '.github/release-policy.json').write_text('{"channel":"alpha"}')
        self.notes = self.root / 'releases/1.2.3.md'
        self.notes.write_text('# WayVoice 1.2.3\n\n' + '\n'.join(f'## {h}\nConcrete user information.\n' for h in ('Highlights', 'Compatibility', 'Downloads', 'Known limitations')))
        self.git('add', 'scripts', '.github', 'releases')
        self.git('commit', '-qm', 'fixture')
        self.git('branch', 'main')
        self.git('tag', '-a', 'v1.2.3', '-m', 'Release')
        self.sha = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def validate(self, tag='v1.2.3', sha=None):
        return release.validate(self.root, tag, sha or self.sha, 'main')

    def test_alpha_policy_marks_numeric_version_prerelease(self):
        self.assertEqual(self.validate()['prerelease'], 'true')

    def test_stable_policy_is_explicit(self):
        (self.root / '.github/release-policy.json').write_text('{"channel":"stable"}')
        self.assertEqual(self.validate()['prerelease'], 'false')

    def test_mismatched_commit_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must match'):
            self.validate(sha='0' * 40)

    def test_absent_tag_is_not_created(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.validate('v9.9.9')
        self.assertEqual(self.git('tag'), 'v1.2.3')

    def test_empty_or_placeholder_notes_rejected(self):
        for text in ('', '# TODO\n', '## Highlights\nFine\n'):
            self.notes.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate()

    def test_blank_section_cannot_borrow_the_following_heading(self):
        self.notes.write_text('## Highlights\n\n## Compatibility\nWorks\n## Downloads\nPackages\n## Known limitations\nAlpha\n')
        with self.assertRaisesRegex(ValueError, 'Highlights'):
            self.validate()

    def test_commit_outside_main_rejected(self):
        (self.root / 'extra').touch()
        self.git('add', 'extra')
        self.git('commit', '-qm', 'not integrated')
        self.git('tag', 'v1.2.4')
        with self.assertRaises(subprocess.CalledProcessError):
            self.validate('v1.2.4', self.git('rev-parse', 'HEAD'))

    def test_workflow_is_tag_only_with_separate_approval_and_permissions(self):
        text = (ROOT / '.github/workflows/release.yml').read_text()
        trigger = text.split('permissions:', 1)[0]
        self.assertIn("tags: ['v*']", trigger)
        self.assertNotIn('branches:', trigger)
        self.assertNotIn('workflow_dispatch:', trigger)
        self.assertIn('needs: build', text)
        self.assertIn('environment: release', text)
        self.assertEqual(text.count('contents: write'), 1)
        self.assertNotIn('--clobber', text)
        self.assertIn('--verify-tag --draft', text)
        self.assertIn('RELEASE_ENVIRONMENT_READY', text)
