"""Exercise registry failures without Docker or network access."""
import json
import os
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
