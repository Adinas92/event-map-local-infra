#!/usr/bin/env python3
"""Offline secret scan of Git-tracked text; output contains locations, never values.

Canonical source: event-map-docs/scripts/security_scan.py. Standalone copies are
kept identical so CI in each repository needs no private cross-repository token.
Requires detect-secrets==1.5.0. No provider verification or archive traversal.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

DENIED = re.compile(
    r'(?i)(?:^|/)(?:\.env(?:\..*)?|.*(?:dump|backup).*|credentials\.json|secrets\.json|'
    r'.*\.(?:pem|key|p12|pfx|tfstate)(?:\..*)?|tfplan|.*\.tfplan|'
    r'(?:playwright-)?auth-state\.json|storage-state\.json)$'
)
EXAMPLES = {'.env.example', '.env.template', '.env.sample'}
BINARY = {'.png', '.jpg', '.jpeg', '.webp', '.ico', '.pdf', '.zip', '.gz', '.tgz',
          '.tar', '.jar', '.class', '.woff', '.woff2', '.ttf', '.db', '.sqlite', '.sqlite3'}
EXPORT = re.compile(r'(?i)(dump|backup)')


def excluded(path: str) -> bool:
    return bool(DENIED.search(path)) and Path(path).name not in EXAMPLES


def run_git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError('Git failed; raw diagnostic suppressed')
    return result.stdout


def safe_findings(raw: bytes, mapping: dict[str, dict], root: Path = Path.cwd()) -> list[dict]:
    baseline = json.loads(raw)
    if not isinstance(baseline.get('results'), dict):
        raise ValueError('Invalid scanner result')
    return [{**mapping[str((root / path).resolve())], 'line': finding['line_number'],
             'rule': finding['type']}
            for path, findings in baseline['results'].items() for finding in findings]


def scan(repo: Path, *, executable: str = 'detect-secrets', history: bool = False,
         history_range: str | None = None) -> int:
    repo = repo.resolve()
    blocked, omitted, mapping = [], [], {}
    with tempfile.TemporaryDirectory(prefix='event-map-scan-') as directory:
        snapshot = Path(directory)

        def stage(path: str, data: bytes, blob: str | None = None) -> None:
            if Path(path).is_absolute() or '..' in Path(path).parts:
                raise ValueError('Unsafe Git path')
            if b'\0' in data:
                omitted.append({'path': path, 'reason': 'binary-content'})
                return
            target = snapshot / (blob or 'working-tree') / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            mapping[str(target.resolve())] = {'path': path, 'blob': blob}

        if history:
            if history_range and not re.fullmatch(r'[0-9a-f]{40,64}\.\.HEAD', history_range):
                raise ValueError('History range must use exact base SHA..HEAD')
            rows = run_git(repo, 'rev-list', '--objects', history_range or '--all').splitlines()
            objects = {}
            for row in rows:
                sha, separator, raw_path = row.partition(b' ')
                if not separator:
                    continue
                path = raw_path.decode('utf-8')
                # Committed credentials are audit targets. Exports are excluded before blob read.
                if EXPORT.search(path) or Path(path).suffix.lower() in BINARY:
                    omitted.append({'path': path, 'reason': 'export-binary-or-archive'})
                    continue
                objects.setdefault(sha.decode(), path)
            for sha, path in objects.items():
                if run_git(repo, 'cat-file', '-t', sha).strip() == b'blob':
                    stage(path, run_git(repo, 'cat-file', 'blob', sha), sha)
        else:
            # Enumerate the index only; never recursively walk the user's checkout.
            for entry in run_git(repo, 'ls-files', '--stage', '-z').split(b'\0'):
                if not entry:
                    continue
                metadata, raw_path = entry.split(b'\t', 1)
                path = raw_path.decode('utf-8')
                mode = metadata.split()[0]
                if excluded(path):
                    blocked.append({'path': path, 'rule': 'private-file-in-git'})
                    continue
                if mode not in (b'100644', b'100755'):
                    omitted.append({'path': path, 'reason': 'non-regular-git-entry'})
                    continue
                if Path(path).suffix.lower() in BINARY:
                    omitted.append({'path': path, 'reason': 'binary-or-archive'})
                    continue
                relative = Path(path)
                source = repo / relative
                if (any((repo / part).is_symlink() for part in [relative, *relative.parents]
                        if part != Path('.')) or not source.resolve().is_relative_to(repo)):
                    raise RuntimeError('Tracked path traverses a symlink; scan stopped')
                if source.exists():
                    stage(path, source.read_bytes())

        # detect-secrets ignores paths outside its working root; execute inside the snapshot.
        result = subprocess.run([executable, 'scan', '--all-files', '--no-verify', '.'],
                                cwd=snapshot, capture_output=True)
        if result.returncode:
            raise RuntimeError('Scanner failed; raw diagnostic suppressed')
        findings = safe_findings(result.stdout, mapping, snapshot)
    print(json.dumps({'mode': 'history' if history else 'tracked-working-tree',
                      'files_scanned': len(mapping), 'blocked_paths': blocked,
                      'omitted': omitted, 'findings': findings}, ensure_ascii=True, indent=2))
    return 1 if blocked or findings else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--scanner', default='detect-secrets')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--history-range', help='Exact base SHA..HEAD; implies --history')
    args = parser.parse_args()
    try:
        return scan(args.repo, executable=args.scanner,
                    history=args.history or bool(args.history_range), history_range=args.history_range)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        print('Security scan ERROR: scanner/Git unavailable or invalid result; no PASS.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
