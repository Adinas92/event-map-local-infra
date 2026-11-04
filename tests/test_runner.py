"""Adapter contracts only; these tests do not prove product or runtime readiness."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import contextlib
import io
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('adapter', str(Path(__file__).resolve().parents[1] / 'bin/test-ui'))
spec = importlib.util.spec_from_loader(loader.name, loader)
adapter = importlib.util.module_from_spec(spec)
loader.exec_module(adapter)


def successful_report():
    return {'status': 'PASS', 'scope': 'application-ui-smoke', 'profile': 'local',
            'target': 'http://127.0.0.1:4200/', 'suiteSelection': 'entry',
            'requiredScenarioIds': ['UI-ENTRY'], 'execution': {'discovered': ['UI-ENTRY']},
            'counts': {'executed': 1, 'passed': 1}}


class OutcomeTests(unittest.TestCase):
    def test_complete_report_and_zero_code(self):
        self.assertEqual(adapter.assess(successful_report(), 0, 'entry', {'UI-ENTRY'}, 'http://127.0.0.1:4200'), ('PASS', 0))

    def test_fail_and_blocked_preserved(self):
        for status, code in [('FAIL', 1), ('BLOCKED', 2)]:
            self.assertEqual(adapter.assess({'status': status}, code, 'entry', {'UI-ENTRY'}, 'http://127.0.0.1:4200'), (status, code))

    def test_no_false_pass(self):
        cases = [{}, {'profile': 'dev'}, {'scope': 'runner-contract-only'},
                 {'requiredScenarioIds': []}, {'execution': {'discovered': []}},
                 {'target': 'http://unapproved'}, {'counts': {'executed': 0, 'passed': 0}}]
        for key in ('failed', 'skipped', 'retries', 'flaky', 'notRun', 'expectedFailures'):
            cases.append({'counts': {'executed': 1, 'passed': 1, key: 1}})
        for change in cases:
            report = successful_report() if change else {}
            report.update(change)
            with self.subTest(change=change):
                self.assertEqual(adapter.assess(report, 0, 'entry', {'UI-ENTRY'}, 'http://127.0.0.1:4200'), ('BLOCKED', 2))
        self.assertEqual(adapter.assess(successful_report(), 1, 'entry', {'UI-ENTRY'}, 'http://127.0.0.1:4200'), ('BLOCKED', 2))
        self.assertEqual(adapter.assess(successful_report(), 0, 'entry', set(), 'http://127.0.0.1:4200'), ('BLOCKED', 2))


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        (self.source / 'config').mkdir(parents=True)
        (self.source / 'package.json').write_text(json.dumps({'devDependencies': {'@playwright/test': '1.60.0'}}))
        self.calls = []
        self.suite_config = None
        self.test_code = 0
        self.no_report = False
        self.interrupt = False
        self.not_ready = False
        self.args = type('Args', (), dict(engine='docker', project='isolated', suite='entry',
              e2e_repo=str(self.source), stack_mode='existing', fixture=None, base_url='http://127.0.0.1:4200', lock_dir=str(self.root / 'locks'),
              readiness_timeout=1, test_timeout=1, start_timeout=1))()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ).start()
        patch.object(adapter.sys, 'stdout', new_callable=io.StringIO).start()
        patch.object(adapter.platform, 'platform', return_value='test-platform').start()
        patch.object(adapter, 'ROOT', self.root).start()
        patch.object(adapter.shutil, 'which', return_value='/engine').start()
        patch.object(adapter, 'command', side_effect=self.command).start()
        patch.object(adapter.subprocess, 'Popen', side_effect=self.popen).start()
        patch.object(adapter, 'git', side_effect=lambda repo, *a: '' if a[0] == 'status' else 'abc123').start()

    def command(self, args, **kwargs):
        self.calls.append(args)
        output = ''
        if 'version' in args:
            output = '5.1.4\n'
        if 'clone' in args:
            dest = Path(args[-1]); (dest / 'config').mkdir(parents=True)
            if self.suite_config:
                (dest / 'config/suites.json').write_text(json.dumps(self.suite_config))
            (dest / 'config/scenarios.json').write_text(json.dumps({'scenarios': [
                {'id': 'UI-ENTRY', 'required': True, 'profiles': ['local'], 'suites': [self.args.suite]}]}))
        if 'ps' in args:
            output = 'a' * 64
        if 'inspect' in args:
            output = 'running healthy' if not self.not_ready else 'running starting'
        return subprocess.CompletedProcess(args, 0, output)

    def popen(self, invocation, **kwargs):
        self.calls.append(invocation)
        outer = self
        class Child:
            def wait(self, timeout):
                if outer.interrupt:
                    raise InterruptedError(signal.SIGTERM)
                if not outer.no_report:
                    workspace = next(x.split('src=')[1].split(',dst=')[0] for x in invocation if 'dst=/workspace' in x)
                    report_dir = Path(workspace) / 'reports' / 'one'
                    report_dir.mkdir(parents=True)
                    report = successful_report()
                    report['suite'] = {'sha': 'abc123', 'dirty': False}
                    if outer.test_code:
                        report['status'] = 'FAIL'
                    (report_dir / 'report.json').write_text(json.dumps(report))
                return outer.test_code
            def poll(self): return 0
        return Child()

    def result(self):
        files = list((self.root / 'reports').glob('*/orchestration.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].parent.stat().st_mode & 0o777, 0o700)
        return json.loads(files[0].read_text())

    def test_existing_pass_and_scoped_cleanup(self):
        self.assertEqual(adapter.run(self.args), 0)
        self.assertEqual(self.result()['status'], 'PASS')
        self.assertFalse(any('up' in call or 'down' in call or '--volumes' in call or 'secrets' in call for call in self.calls))
        invocation = next(c for c in self.calls if c[:2] == ['docker', 'run'])
        self.assertIn('isolated_event-map', invocation)
        self.assertIn('--user', invocation)
        self.assertFalse(any('/run/secrets' in arg for arg in invocation))
        self.assertIn('rm', self.calls[-1])

    def test_podman_keep_id(self):
        self.args.engine = 'podman'
        self.assertEqual(adapter.run(self.args), 0)
        self.assertIn('--userns=keep-id', next(c for c in self.calls if c[:2] == ['podman', 'run']))

    def test_unavailable_engine_not_run(self):
        with patch.object(adapter.shutil, 'which', return_value=None):
            self.assertEqual(adapter.run(self.args), 2)
        self.assertIn('NOT_RUN', self.result()['reason'])
        self.assertEqual(self.calls, [])

    def test_container_budget_exceeds_full_suite_budget(self):
        self.suite_config = {'entry': {'timeoutMs': 900_000}}
        self.args.test_timeout = None
        self.assertEqual(adapter.run(self.args), 0)
        self.assertEqual(self.result()['testTimeoutSeconds'], 1530)

    def test_podman_uses_its_own_version_template(self):
        self.args.engine = 'podman'
        adapter.run(self.args)
        self.assertIn(['podman', 'info', '--format', '{{.Version.Version}}'], self.calls)

    def test_shared_lock_mount_and_explicit_origin(self):
        self.args.base_url = 'http://localhost:4200'
        adapter.run(self.args)
        invocation = next(c for c in self.calls if c[:2] == ['docker', 'run'])
        self.assertIn('E2E_LOCK_DIR=/run/e2e-locks', invocation)
        self.assertIn(f'type=bind,src={self.root / "locks"},dst=/run/e2e-locks', invocation)
        self.assertIn('http://localhost:4200', invocation)
        self.assertEqual((self.root / 'locks').stat().st_mode & 0o777, 0o700)

    def test_bad_lock_permissions_block_before_start(self):
        locks = self.root / 'locks'; locks.mkdir(); locks.chmod(0o755)
        self.args.stack_mode = 'start'
        self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'LOCK_DIRECTORY_INVALID')
        self.assertFalse(any('up' in c for c in self.calls))

    def test_dirty_checkout_is_not_silently_cloned(self):
        with patch.object(adapter, 'git', return_value=' M tracked-file'):
            self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'E2E_CHECKOUT_DIRTY')
        self.assertFalse(any('clone' in c for c in self.calls))

    def test_runner_timeout_is_blocked_and_cleanup_runs(self):
        class TimeoutChild:
            def wait(self, timeout): raise subprocess.TimeoutExpired('engine', timeout)
            def poll(self): return 0
        with patch.object(adapter.subprocess, 'Popen', return_value=TimeoutChild()):
            self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'RUNNER_TIMEOUT')
        self.assertEqual(self.calls[-1][:3], ['docker', 'rm', '-f'])

    def test_test_failure_is_nonzero(self):
        self.test_code = 1
        self.assertEqual(adapter.run(self.args), 1)
        self.assertEqual(self.result()['status'], 'FAIL')

    def test_missing_report_blocks(self):
        self.no_report = True
        self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'RUNNER_REPORT_UNAVAILABLE')

    def test_signal_keeps_diagnostics_and_removes_only_runner(self):
        self.interrupt = True
        self.assertEqual(adapter.run(self.args), 143)
        self.assertEqual(self.result()['reason'], 'INTERRUPTED')
        self.assertEqual(self.calls[-1][:3], ['docker', 'rm', '-f'])

    def test_readiness_timeout_does_not_start_runner(self):
        self.not_ready = True
        with patch.object(adapter.time, 'monotonic', side_effect=[0, 2]):
            self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'STACK_READINESS_TIMEOUT')
        self.assertFalse(any('run' in c for c in self.calls))

    def test_start_is_explicit_and_never_resets(self):
        self.args.stack_mode = 'start'
        self.assertEqual(adapter.run(self.args), 0)
        starts = [c for c in self.calls if 'up' in c]
        self.assertEqual(len(starts), 1)
        self.assertIn('--build', starts[0])
        self.assertFalse(any('down' in c or '--volumes' in c for c in self.calls))

    def test_fixture_required_for_business_before_stack_start(self):
        self.args.suite = 'business'; self.args.stack_mode = 'start'
        self.assertEqual(adapter.run(self.args), 2)
        self.assertEqual(self.result()['reason'], 'FIXTURE_FILE_REQUIRED')
        self.assertFalse(any('up' in c for c in self.calls))

    def test_fixture_is_readonly_and_no_content_read(self):
        self.args.suite = 'business'
        fixture = self.root / 'fixture.json'; fixture.write_text('secret-canary'); fixture.chmod(0o600)
        self.args.fixture = str(fixture)
        adapter.run(self.args)
        invocation = next(c for c in self.calls if c[:2] == ['docker', 'run'])
        self.assertIn(f'type=bind,src={fixture},dst=/run/e2e-fixture.json,readonly', invocation)
        self.assertNotIn('secret-canary', json.dumps(self.result()))


class WrapperTests(unittest.TestCase):
    def test_compose_selects_exact_engine_and_preserves_status(self):
        script = Path(__file__).resolve().parents[1] / 'bin/compose'
        with tempfile.TemporaryDirectory() as directory:
            for engine in ('docker', 'podman'):
                stub = Path(directory) / engine
                stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 7\n')
                stub.chmod(0o700)
                env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'], CONTAINER_ENGINE=engine)
                result = subprocess.run([str(script), 'ps', '-q', 'service'], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 7)
                self.assertEqual(result.stdout.splitlines(), ['compose', 'ps', '-q', 'service'])
            env['CONTAINER_ENGINE'] = 'automatic-fallback'
            result = subprocess.run([str(script), 'ps'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
