"""Exercise registry failures without Docker or network access."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RpmContainerTests(unittest.TestCase):
    def run_builder(self, failures=0, run_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = base / 'docker'
            docker.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
base = Path(os.environ['FAKE_DOCKER_DIR'])
with (base / 'calls').open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'pull':
    count = int((base / 'count').read_text()) if (base / 'count').exists() else 0
    (base / 'count').write_text(str(count + 1))
    sys.exit(1 if count < int(os.environ['PULL_FAILURES']) else 0)
sys.exit(int(os.environ['RUN_EXIT']))
''')
            docker.chmod(0o755)
            sleep = base / 'sleep'
            sleep.write_text('#!/bin/sh\nexit 0\n')
            sleep.chmod(0o755)
            env = dict(os.environ, PATH=f'{base}:{os.environ["PATH"]}',
                       FAKE_DOCKER_DIR=str(base), PULL_FAILURES=str(failures),
                       RUN_EXIT=str(run_exit))
            result = subprocess.run(['bash', str(ROOT / 'scripts/build-rpm-container.sh')],
                                    env=env, capture_output=True, text=True, timeout=5)
            calls = [json.loads(line) for line in (base / 'calls').read_text().splitlines()]
            return result, calls

    def test_transient_pull_failures_retry_before_one_build(self):
        result, calls = self.run_builder(failures=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ['pull', 'pull', 'pull', 'run'])
        self.assertTrue(all(call[1] == 'registry.fedoraproject.org/fedora:44'
                            for call in calls[:3]))
        self.assertIn('--pull=never', calls[-1])

    def test_exhausted_pull_fails_without_starting_build(self):
        result, calls = self.run_builder(failures=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[0] for call in calls], ['pull'] * 3)
        self.assertIn('RPM build was not started', result.stderr)

    def test_packaging_failure_is_propagated_without_retry(self):
        result, calls = self.run_builder(run_exit=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual([call[0] for call in calls], ['pull', 'run'])


class RpmArtifactTests(unittest.TestCase):
    def test_rebuild_does_not_publish_stale_rpm_and_removes_owned_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ('app', 'scripts', 'systemd', 'data', 'packaging', 'third_party', 'bin'):
                (base / name).mkdir()
            for name in ('README.md', 'CHANGELOG.md', 'LICENSE'):
                (base / name).write_text('fixture')
            # The source archive now includes the public documentation bundle.
            import runpy
            for name in runpy.run_path(str(ROOT / 'scripts/package-readme.py'))['PUBLIC_FILES']:
                destination = base / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, destination)
            shutil.copyfile(ROOT / 'scripts/package-readme.py', base / 'scripts/package-readme.py')
            shutil.copyfile(ROOT / 'packaging/public-docs.json', base / 'packaging/public-docs.json')
            shutil.copy2(ROOT / 'scripts/build-rpm.sh', base / 'scripts/build-rpm.sh')
            (base / 'scripts/check-version.py').write_text("print('1.2.3')\n")
            stale = base / 'build/rpm/RPMS/x86_64/wayvoice-1.2.3-1.fc43.x86_64.rpm'
            stale.parent.mkdir(parents=True)
            stale.write_text('stale')
            rpm = base / 'bin/rpm'
            rpm.write_text('#!/bin/sh\ncase "$*" in\n*VERSION*) echo 1.2.3;;\n*) echo x86_64;;\nesac\n')
            rpm.chmod(0o755)
            builder = base / 'bin/rpmbuild'
            builder.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
args = sys.argv
top = Path(next(a.split(' ', 1)[1] for a in args if a.startswith('_topdir ')))
out = top / 'RPMS/x86_64/wayvoice-1.2.3-1.fc44.x86_64.rpm'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('fresh')
''')
            builder.chmod(0o755)
            env = dict(os.environ, PATH=f'{base / "bin"}:{os.environ["PATH"]}', SOURCE_DATE_EPOCH='1')
            result = subprocess.run(['bash', str(base / 'scripts/build-rpm.sh')],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([p.name for p in (base / 'dist').glob('*.rpm')],
                             ['wayvoice-1.2.3-1.fc44.x86_64.rpm'])
            self.assertTrue(stale.exists(), 'Unrelated previous output must be preserved')
            self.assertEqual(list((base / 'build').glob('rpm.*')), [])
