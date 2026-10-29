import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import secrets
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('security_scan', Path(__file__).with_name('security_scan.py'))
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SecurityScanTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('detect-secrets'), 'detect-secrets is required for engine smoke test')
    def test_real_engine_detects_a_synthetic_credential_without_disclosing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            marker = 'sk_' + 'live_' + secrets.token_hex(16)
            (repo / 'config.py').write_text('stripe_api_key = "' + marker + '"\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'config.py'], check=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(scanner.scan(repo), 1)
            self.assertTrue(json.loads(output.getvalue())['findings'])
            self.assertNotIn(marker, output.getvalue())

    @unittest.skipUnless(shutil.which('detect-secrets'), 'detect-secrets is required for engine history test')
    def test_history_detects_removed_committed_credential_and_excludes_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            marker = 'sk_' + 'live_' + secrets.token_hex(16)
            (repo / '.env').write_text('stripe_api_key = "' + marker + '"\n')
            (repo / 'dump.sql').write_text('SYNTHETIC_EXPORT_DO_NOT_SCAN')
            subprocess.run(['git', '-C', str(repo), 'add', '.env', 'dump.sql'], check=True)
            commit = ['git', '-C', str(repo), '-c', 'user.name=Synthetic', '-c',
                      'user.email=synthetic@example.invalid', 'commit', '-qm']
            subprocess.run(commit + ['synthetic fixture'], check=True)
            subprocess.run(['git', '-C', str(repo), 'rm', '-q', '.env'], check=True)
            subprocess.run(commit + ['remove synthetic fixture'], check=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(scanner.scan(repo, history=True), 1)
            result = json.loads(output.getvalue())
            self.assertTrue(any(f['path'] == '.env' and f['blob'] for f in result['findings']))
            self.assertFalse(any(f['path'] == 'dump.sql' for f in result['findings']))
            self.assertTrue(any(f['path'] == 'dump.sql' for f in result['omitted']))
            self.assertNotIn(marker, output.getvalue())

    def test_report_discards_values_and_personal_metadata(self):
        result = scanner.safe_findings(json.dumps({'results': {'/tmp/scan/a.py': [{
            'line_number': 4, 'type': 'test', 'hashed_secret': 'SYNTHETIC_HASH', # pragma: allowlist secret -- synthetic scanner output fixture
            'secret_value': 'SYNTHETIC_PRIVATE_MARKER', # pragma: allowlist secret -- synthetic scanner output fixture
        }]}, 'Author': 'SYNTHETIC_AUTHOR', 'Email': 'person@example.invalid'}).encode(),
            {str(Path('/tmp/scan/a.py').resolve()): {'path': 'a.py', 'blob': 'abc'}})
        self.assertEqual(result, [{'path': 'a.py', 'blob': 'abc', 'line': 4, 'rule': 'test'}])

    def test_private_files_are_blocked_but_examples_are_not(self):
        for path in ['.env', '.env.production', 'db/dump.sql', 'backup.sql.gz',
                     'infra/terraform.tfstate', 'infra/plan.tfplan', '.auth/storage-state.json']:
            self.assertTrue(scanner.excluded(path), path)
        for path in ['.env.example', 'docs/secrets.md', 'config/secrets-manifest.tsv',
                     'src/main.sql', 'tests/test_runtime_secrets.py']:
            self.assertFalse(scanner.excluded(path), path)

    def test_scan_does_not_read_export_untracked_secret_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'safe.txt').write_text('synthetic public fixture')
            (repo / '.env').write_text('SYNTHETIC_UNTRACKED_PRIVATE')
            (repo / 'dump.sql').mkdir()  # Reading this blocked path would fail.
            (repo / 'link').symlink_to(repo / '.env')
            subprocess.run(['git', '-C', str(repo), 'add', 'safe.txt', 'link'], check=True)
            # Add a fake dump index entry without ever reading a database export.
            blob = subprocess.check_output(['git', '-C', str(repo), 'hash-object', '-w', 'safe.txt']).strip().decode()
            subprocess.run(['git', '-C', str(repo), 'update-index', '--add', '--cacheinfo',
                            '100644,' + blob + ',dump.sql'], check=True)
            fake = repo / 'fake-scanner'
            fake.write_text('#!/usr/bin/env python3\nimport pathlib,sys,json\n'
                            'snapshot=pathlib.Path(sys.argv[-1])\n'
                            'assert sorted(p.name for p in snapshot.rglob("*") if p.is_file())==["safe.txt"]\n'
                            'print(json.dumps({"results":{}}))\n')
            fake.chmod(0o755)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(scanner.scan(repo, executable=str(fake)), 1)
            self.assertNotIn('SYNTHETIC_UNTRACKED_PRIVATE', output.getvalue())
            self.assertIn('private-file-in-git', output.getvalue())

    def test_missing_scanner_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            with self.assertRaises(OSError):
                scanner.scan(repo, executable='/does-not-exist')

    def test_ancestor_symlink_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'nested').mkdir()
            (repo / 'nested/safe.txt').write_text('synthetic')
            subprocess.run(['git', '-C', str(repo), 'add', 'nested/safe.txt'], check=True)
            (repo / 'nested/safe.txt').unlink()
            (repo / 'nested').rmdir()
            outside = Path(directory) / 'outside'
            outside.mkdir()
            (outside / 'safe.txt').mkdir()  # A read would fail with IsADirectoryError.
            (repo / 'nested').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                scanner.scan(repo, executable='/does-not-exist')


if __name__ == '__main__':
    unittest.main()
