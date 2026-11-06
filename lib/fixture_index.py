"""Local fixture projection orchestration with private point-level cleanup records."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone

from fixture_contract import (ContractError, lock_keys, validate_inventory, validate_owner, validate_points)

ROOT = Path(__file__).resolve().parents[1]


class SetupError(Exception):
    pass


def read_private(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        with os.fdopen(os.open(path, flags), 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_size > 1_000_000):
                raise SetupError('PRIVATE_FILE_INVALID')
            return json.load(stream)
    except (OSError, ValueError):
        raise SetupError('PRIVATE_FILE_INVALID') from None


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_dir(path):
    path = Path(path)
    missing, current = [], path
    while not current.exists():
        if current.is_symlink():
            raise SetupError('PRIVATE_DIRECTORY_INVALID')
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory)
        sync_directory(directory.parent)
    if path.is_symlink():
        raise SetupError('PRIVATE_DIRECTORY_INVALID')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise SetupError('PRIVATE_DIRECTORY_INVALID')
    sync_directory(path)
    sync_directory(path.parent)
    return path


def save(path, value):
    path = Path(path)
    if path.is_symlink():
        raise SetupError('PRIVATE_FILE_INVALID')
    temp = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class Locks:
    def __init__(self, directory, keys):
        self.directory, self.keys = private_dir(directory), keys
        self.token, self.held, self.uncertain = uuid.uuid4().hex, [], False

    def __enter__(self):
        try:
            for key in self.keys:
                path = self.directory / key
                path.mkdir(mode=0o700)
                self.held.append(path)
                save(path / 'owner.json', {'token': self.token})
        except (OSError, SetupError):
            self.release()
            raise SetupError('FIXTURE_LOCKED') from None
        return self

    def release(self):
        for path in reversed(self.held):
            try:
                if read_private(path / 'owner.json').get('token') == self.token:
                    (path / 'owner.json').unlink()
                    path.rmdir()
            except (OSError, SetupError):
                pass
        self.held = []

    def __exit__(self, *_args):
        if not self.uncertain:
            self.release()


def tool(args, *, payload=None, timeout=30):
    try:
        result = subprocess.run(args, input=payload, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError('ENGINE_OR_WORKER_UNAVAILABLE') from None
    if result.returncode:
        raise SetupError('ENGINE_OR_WORKER_NONZERO')
    return result.stdout.strip()


def worker_source():
    contract = (ROOT / 'lib/fixture_contract.py').read_text()
    code = (ROOT / 'lib/fixture_index_worker.py').read_text()
    return ('import types,sys\n_contract=types.ModuleType("fixture_contract")\n'
            'sys.modules["fixture_contract"]=_contract\n'
            f'exec(compile({contract!r},"fixture_contract","exec"),_contract.__dict__)\n' + code)


def invoke(engine, container, request, seconds):
    request = dict(request, maxSeconds=seconds)
    raw = tool([engine, 'exec', '-i', container, 'python', '-c', worker_source()],
               payload=json.dumps(request, ensure_ascii=False, separators=(',', ':')), timeout=seconds + 30)
    try:
        response = json.loads(raw)
    except ValueError:
        raise SetupError('WORKER_EVIDENCE_UNAVAILABLE') from None
    if not isinstance(response, dict) or response.get('complete') is not True:
        raise SetupError('WORKER_EVIDENCE_UNAVAILABLE')
    return response


def runtime(engine, project):
    env_engine = os.environ.get('CONTAINER_ENGINE')
    os.environ['CONTAINER_ENGINE'] = engine
    containers, images = {}, {}
    try:
        for service in ('event-ai', 'qdrant'):
            container = tool([str(ROOT / 'bin/compose'), '-p', project, '-f', str(ROOT / 'compose.yaml'),
                              'ps', '-q', service])
            if not re.fullmatch(r'[a-f0-9]{12,64}', container):
                raise SetupError('LOCAL_PROJECTION_CONTAINER_UNAVAILABLE')
            # Qdrant has no healthcheck. Avoid looking up an optional State.Health key;
            # Docker may reject that template, while Podman's State may be a struct.
            template = '{{.Id}} {{.Image}} {{.State.Status}}'
            if service == 'event-ai':
                template += ' {{.State.Health.Status}}'
            facts = tool([engine, 'inspect', '--format', template, container]).split()
            if (len(facts) not in {3, 4} or not re.fullmatch(r'[a-f0-9]{64}', facts[0])
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', facts[1]) or facts[2] != 'running'
                    or (service == 'event-ai' and facts[3:] != ['healthy'])):
                raise SetupError('LOCAL_PROJECTION_NOT_READY')
            containers[service], images[service] = facts[:2]
    finally:
        if env_engine is None:
            os.environ.pop('CONTAINER_ENGINE', None)
        else:
            os.environ['CONTAINER_ENGINE'] = env_engine
    return containers['event-ai'], {'aiContainerId': containers['event-ai'], 'aiImageId': images['event-ai'],
        'qdrantContainerId': containers['qdrant'], 'qdrantImageId': images['qdrant'],
        'composeProject': project, 'engine': engine}


def validate_target(value):
    if (not isinstance(value, dict) or set(value) != {'qdrantTarget', 'collection'}
            or value['qdrantTarget'] not in {'http://qdrant:6333', 'http://localhost:6333', 'http://127.0.0.1:6333'}
            or not isinstance(value['collection'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', value['collection'])):
        raise SetupError('PROJECTION_TARGET_INVALID')
    return value


def perform(args):
    os.umask(0o077)
    inventory = validate_inventory(read_private(args.input), args.base_url, allow_expired=args.action == 'cleanup')
    owner = validate_owner(args.owner)
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    directory = private_dir(args.state_dir or Path(args.input).absolute().parent / 'index-state')
    data_dir = private_dir(directory / 'datasets')
    evidence_root = private_dir(directory / 'fixture-index')
    key = hashlib.sha256(('http://127.0.0.1:4200\n' + inventory['namespace']).encode()).hexdigest()
    state_path = data_dir / (key + '.json')
    report_dir = private_dir(evidence_root / uuid.uuid4().hex)
    evidence = report_dir / 'evidence.json'
    attestation_path = data_dir / (key + '-attestation.json')
    state = {'schemaVersion': 1, 'inventorySha256': digest, 'owner': owner,
             'baseURL': inventory['baseURL'], 'namespace': inventory['namespace'],
             'datasetSha256': inventory['datasetSha256'], 'phase': 'not-started'}
    with Locks(args.lock_dir, lock_keys(inventory)) as locks:
        old = read_private(state_path) if state_path.exists() else None
        renewing = isinstance(old, dict) and old.get('phase') == 'CLEANED' and args.action == 'index'
        if old is not None and (not isinstance(old, dict) or old.get('owner') != owner
                                or (not renewing and old.get('inventorySha256') != digest)):
            raise SetupError('DATASET_STATE_CONFLICT')
        if args.action == 'cleanup' and not old:
            raise SetupError('POINT_INVENTORY_REQUIRED')
        # An earlier attestation must not survive a new/failed index or cleanup attempt.
        if attestation_path.exists():
            if attestation_path.is_symlink():
                raise SetupError('PRIVATE_FILE_INVALID')
            attestation_path.unlink()
            sync_directory(attestation_path.parent)
        container, metadata = runtime(args.engine, args.project)
        if old and old.get('projection', {}).get('runtime') != metadata:
            raise SetupError('PROJECTION_TARGET_CHANGED')
        request = {'inventory': inventory, 'inventorySha256': digest}
        points = None
        ledger_started = False
        try:
            if args.action == 'cleanup':
                target = invoke(args.engine, container, dict(request, action='target'), args.worker_timeout)
                if target.get('status') != 'TARGET_VALIDATED':
                    raise SetupError(target.get('reason', 'PROJECTION_TARGET_UNAVAILABLE'))
                projection_target = validate_target(target.get('projectionTarget'))
                points = old.get('points')
                validate_points(points, inventory)
                request.update(action='cleanup', points=points)
            else:
                plan = invoke(args.engine, container, dict(request, action='plan'), args.worker_timeout)
                if plan.get('status') != 'PLANNED':
                    raise SetupError(plan.get('reason', 'INDEX_PLAN_UNAVAILABLE'))
                points = plan.get('points')
                validate_points(points, inventory)
                projection_target = validate_target(plan.get('projectionTarget'))
                if old and not renewing and old.get('points') != points:
                    raise SetupError('POINT_PLAN_CHANGED_REQUIRES_OLD_CLEANUP')
                request.update(action='index', points=points,
                               indexImplementationSha256=plan['runtime']['indexImplementationSha256'])
            projection = {'runtime': metadata, 'target': projection_target}
            if old and old.get('projection') != projection:
                raise SetupError('PROJECTION_TARGET_CHANGED')
            request['projectionTarget'] = projection_target
            if renewing:
                history = private_dir(directory / 'history')
                save(history / (key + '-' + uuid.uuid4().hex + '.json'), old)
            state.update(points=points, phase='planned', runtime=metadata, projection=projection)
            save(state_path, state)  # Durable exact point/chunk inventory precedes all writes.
            ledger_started = True
            locks.uncertain = True
            result = invoke(args.engine, container, request, args.worker_timeout)
            locks.uncertain = False  # A complete remote response proves the worker ended.
            if result.get('status') != ('INDEXED_NOT_UI_VERIFIED' if args.action == 'index' else 'CLEANED'):
                raise SetupError(result.get('reason', 'INDEX_WORKER_FAILED'))
            if result.get('points') != points or result.get('projectionTarget') != projection_target:
                raise SetupError('POINT_EVIDENCE_MISMATCH')
            state.update(phase=result['status'], runtime=dict(metadata, **result.get('runtime', {})))
            save(state_path, state)
            save(evidence, dict(state, scope='local-fixture-projection-setup-only',
                                timestamp=datetime.now(timezone.utc).isoformat()))
            if args.action == 'index':
                attestation = {k: inventory[k] for k in ('baseURL', 'namespace', 'datasetSha256', 'validUntil')}
                attestation.update(eventIds=[e['id'] for e in inventory['events'].values()], owner=owner,
                                   evidenceRef=f'fixture-index/{report_dir.name}/evidence.json')
                save(attestation_path, attestation)
                print(f'INDEXED_NOT_UI_VERIFIED; attestation: {attestation_path}; evidence: {evidence}')
            else:
                print(f'CLEANED_OWN_POINTS_ONLY; evidence: {evidence}')
            return 0
        except BaseException:
            # Keep the original point ledger after partial failure. An uncertain exec keeps
            # namespace and per-ID locks until the operator proves no worker is active.
            state.update(phase='UNCERTAIN' if locks.uncertain else 'BLOCKED')
            if points and ledger_started:
                state['points'] = points
                save(state_path, state)
            save(evidence, dict(state, scope='local-fixture-projection-setup-only'))
            raise


def positive(value):
    number = int(value)
    if not 1 <= number <= 900:
        raise argparse.ArgumentTypeError('must be between 1 and 900 seconds')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('index', 'cleanup'))
    parser.add_argument('--input', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--base-url', choices=('http://localhost:4200', 'http://localhost:4200/',
                                             'http://127.0.0.1:4200', 'http://127.0.0.1:4200/'), required=True)
    parser.add_argument('--engine', choices=('docker', 'podman'), required=True)
    parser.add_argument('--project', default='event-map-local')
    parser.add_argument('--state-dir')
    parser.add_argument('--lock-dir', default=os.environ.get('E2E_LOCK_DIR') or str(Path(tempfile.gettempdir()) / 'event-map-e2e-locks'))
    parser.add_argument('--worker-timeout', type=positive, default=180)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', args.project):
        parser.error('invalid local project')
    def interrupted(signum, _frame):
        raise InterruptedError(signum)
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, interrupted)
    try:
        return perform(args)
    except InterruptedError as error:
        print('BLOCKED: INTERRUPTED; inspect private point ledger and retained locks')
        return 128 + error.args[0]
    except (SetupError, ContractError) as error:
        reason = str(error)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', reason):
            reason = 'INDEX_SETUP_UNAVAILABLE'
        print(f'BLOCKED: {reason}')
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('BLOCKED: INDEX_SETUP_UNAVAILABLE')
        return 2
