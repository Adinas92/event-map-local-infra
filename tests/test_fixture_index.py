"""Synthetic setup contracts only; never product, service, or model evidence."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import fixture_index as host
import fixture_index_worker as worker
from fixture_contract import dataset_hash, lock_keys, point_id


def inventory():
    now = datetime.now(timezone.utc)
    date = (now + timedelta(days=10)).date().isoformat()
    ns = 'e2e-0123456789ab'
    loc = {'city': 'Warsaw', 'address': 'Synthetic own fixture street', 'latitude': '52.2', 'longitude': '21.0'}
    events = {}
    for role, ident, category, access in [('primary', '11111111-1111-4111-8111-111111111111', 'MUSIC', 'FREE'),
                                          ('secondary', '22222222-2222-4222-8222-222222222222', 'THEATER', 'PAID')]:
        events[role] = {'id': ident, 'title': ns + ' ' + role, 'description': ns + ' own description',
                        'start': date + 'T18:00', 'end': date + 'T20:00',
                        **{k: loc[k] for k in ('address', 'latitude', 'longitude')},
                        'category': category, 'access': access}
    result = {'schemaVersion': 1, 'baseURL': 'http://localhost:4200/', 'namespace': ns,
              'datasetSha256': '', 'timezone': 'Europe/Warsaw', 'location': loc, 'events': events,
              'validUntil': (now + timedelta(days=2)).isoformat(timespec='seconds').replace('+00:00', 'Z')}
    result['datasetSha256'] = dataset_hash(result)
    return result


def points(data, chunks=False):
    result = []
    for event in data['events'].values():
        for index in ([0, 1, 2] if chunks else [None]):
            result.append({'pointId': point_id(event['id'], index), 'eventId': event['id'], 'chunkIndex': index})
    return result


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = inventory()
        self.input = self.root / 'input.json'
        self.input.write_text(json.dumps(self.data)); self.input.chmod(0o600)
        self.args = types.SimpleNamespace(action='index', input=str(self.input), owner='fixture-operator',
                base_url='http://localhost:4200', state_dir=str(self.root / 'state'),
                lock_dir=str(self.root / 'locks'), engine='docker', project='event-map-local', worker_timeout=180)
        self.plan = points(self.data, chunks=True)
        self.calls = []
        self.problem = None
        self.addCleanup(patch.stopall)
        self.metadata = {'aiContainerId': 'a'*64, 'aiImageId': 'sha256:'+'b'*64,
            'qdrantContainerId': 'd'*64, 'qdrantImageId': 'sha256:'+'e'*64,
            'engine': 'docker', 'composeProject': 'event-map-local'}
        self.target = {'qdrantTarget': 'http://qdrant:6333', 'collection': 'events'}
        patch.object(host, 'runtime', side_effect=lambda *_: ('a'*64, self.metadata.copy())).start()
        patch.object(host, 'invoke', side_effect=self.invoke).start()
        patch.object(sys, 'stdout', new_callable=io.StringIO).start()

    def invoke(self, engine, container, request, seconds):
        self.calls.append(request['action'])
        if request['action'] == 'target':
            return {'complete': True, 'status': 'TARGET_VALIDATED', 'projectionTarget': self.target.copy()}
        if request['action'] == 'plan':
            return {'complete': True, 'status': 'PLANNED', 'points': self.plan,
                    'runtime': {'indexImplementationSha256': 'c'*64}, 'projectionTarget': self.target.copy()}
        ledger = next(p for p in (self.root/'state/datasets').glob('*.json') if not p.name.endswith('-attestation.json'))
        # Every exact ID, including all chunks, is durable before the write request.
        self.assertEqual(host.read_private(ledger)['points'], self.plan)
        if self.problem == 'uncertain':
            raise host.SetupError('WORKER_EVIDENCE_UNAVAILABLE')
        if self.problem == 'partial':
            return {'complete': True, 'status': 'BLOCKED', 'reason': 'INDEX_WORKER_FAILED'}
        if self.problem == 'interrupt':
            raise InterruptedError(signal.SIGTERM)
        if self.problem == 'wrong-status':
            return {'complete': True, 'status': 'CLEANED', 'points': self.plan, 'runtime': {}}
        return {'complete': True, 'status': 'CLEANED' if request['action'] == 'cleanup' else 'INDEXED_NOT_UI_VERIFIED',
                'points': self.plan, 'runtime': {'embeddingModel': 'real-adapter-in-production-not-this-test'},
                'projectionTarget': self.target.copy()}

    def attestations(self):
        return list((self.root/'state/datasets').glob('*-attestation.json'))

    def test_index_and_idempotent_repeat_preserve_exact_chunks(self):
        self.assertEqual(host.perform(self.args), 0)
        self.assertEqual(host.perform(self.args), 0)
        attestation = host.read_private(self.attestations()[0])
        self.assertEqual(attestation['eventIds'], [e['id'] for e in self.data['events'].values()])
        self.assertEqual(attestation['datasetSha256'], self.data['datasetSha256'])
        self.assertFalse(any(k in attestation for k in ('password', 'actors', 'uiVerifiedAt')))
        self.assertEqual(self.attestations()[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((self.root/'locks').iterdir()), [])
        self.assertEqual(self.calls, ['plan', 'index', 'plan', 'index'])

    def test_cleanup_requires_ledger_and_removes_only_its_attestation(self):
        self.args.action = 'cleanup'
        with self.assertRaisesRegex(host.SetupError, 'POINT_INVENTORY_REQUIRED'):
            host.perform(self.args)
        self.assertEqual(self.calls, [])
        self.args.action = 'index'; host.perform(self.args)
        own = self.attestations()[0]
        foreign = own.parent/'other-dataset-attestation.json'; foreign.write_text('{}'); foreign.chmod(0o600)
        self.args.action = 'cleanup'
        self.assertEqual(host.perform(self.args), 0)
        self.assertFalse(own.exists()); self.assertTrue(foreign.exists())
        self.assertEqual(self.calls[-1], 'cleanup')

    def test_partial_failure_has_no_attestation_and_keeps_exact_plan(self):
        self.problem = 'partial'
        with self.assertRaisesRegex(host.SetupError, 'INDEX_WORKER_FAILED'):
            host.perform(self.args)
        self.assertEqual(self.attestations(), [])
        ledger = next((self.root/'state/datasets').glob('*.json'))
        self.assertEqual(host.read_private(ledger)['points'], self.plan)
        self.assertEqual(host.read_private(ledger)['phase'], 'BLOCKED')
        self.assertEqual(list((self.root/'locks').iterdir()), [])

    def test_rejected_new_chunk_plan_preserves_original_ledger_byte_for_byte(self):
        host.perform(self.args)
        ledger = next(p for p in (self.root/'state/datasets').glob('*.json') if not p.name.endswith('-attestation.json'))
        original = ledger.read_bytes()
        self.plan = points(self.data, chunks=False)
        self.calls.clear()
        with self.assertRaisesRegex(host.SetupError, 'POINT_PLAN_CHANGED_REQUIRES_OLD_CLEANUP'):
            host.perform(self.args)
        self.assertEqual(ledger.read_bytes(), original)
        self.assertEqual(self.calls, ['plan'])
        self.assertEqual(self.attestations(), [])

    def test_unknown_remote_outcome_retains_all_three_locks(self):
        self.problem = 'uncertain'
        with self.assertRaisesRegex(host.SetupError, 'WORKER_EVIDENCE_UNAVAILABLE'):
            host.perform(self.args)
        self.assertEqual({p.name for p in (self.root/'locks').iterdir()}, set(lock_keys(self.data)))
        self.assertEqual(self.attestations(), [])

    def test_interruption_retains_locks(self):
        self.problem = 'interrupt'
        with self.assertRaises(InterruptedError): host.perform(self.args)
        self.assertEqual(len(list((self.root/'locks').iterdir())), 3)

    def test_conflicting_owner_or_dataset_never_runs_worker(self):
        host.perform(self.args); self.calls.clear()
        self.args.owner = 'other-operator'
        with self.assertRaisesRegex(host.SetupError, 'DATASET_STATE_CONFLICT'): host.perform(self.args)
        self.assertEqual(self.calls, [])

    def test_shared_e2e_namespace_lock_blocks_before_runtime(self):
        locks = self.root/'locks'; locks.mkdir(mode=0o700)
        ns_key = __import__('hashlib').sha256(('http://127.0.0.1:4200\n'+self.data['namespace']).encode()).hexdigest()
        (locks/ns_key).mkdir(mode=0o700)
        with self.assertRaisesRegex(host.SetupError, 'FIXTURE_LOCKED'): host.perform(self.args)
        self.assertEqual(self.calls, [])
        self.assertTrue((locks/ns_key).exists())

    def test_plan_corruption_or_wrong_status_cannot_attest(self):
        self.problem = 'wrong-status'
        with self.assertRaises(host.SetupError): host.perform(self.args)
        self.assertEqual(self.attestations(), [])

    def test_unsafe_file_mode_and_symlink_rejected(self):
        self.input.chmod(0o644)
        with self.assertRaisesRegex(host.SetupError, 'PRIVATE_FILE_INVALID'): host.perform(self.args)
        self.input.chmod(0o600)
        link = self.root/'link.json'; link.symlink_to(self.input)
        self.args.input = str(link)
        with self.assertRaisesRegex(host.SetupError, 'PRIVATE_FILE_INVALID'): host.perform(self.args)
        self.assertEqual(self.calls, [])

    def ledger(self):
        return next(p for p in (self.root/'state/datasets').glob('*.json') if not p.name.endswith('-attestation.json'))

    def test_projection_drift_blocks_index_and_cleanup_without_replacing_ledger(self):
        host.perform(self.args)
        original = self.ledger().read_bytes()
        for action in ('index', 'cleanup'):
            self.args.action = action
            for field in ('composeProject', 'qdrantContainerId', 'aiImageId'):
                with self.subTest(action=action, field=field):
                    old = self.metadata[field]; self.metadata[field] = 'changed'
                    self.calls.clear()
                    with self.assertRaisesRegex(host.SetupError, 'PROJECTION_TARGET_CHANGED'):
                        host.perform(self.args)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.ledger().read_bytes(), original)
                    self.metadata[field] = old
            self.target['collection'] = 'other'
            self.calls.clear()
            with self.assertRaisesRegex(host.SetupError, 'PROJECTION_TARGET_CHANGED'):
                host.perform(self.args)
            self.assertEqual(self.calls, ['plan' if action == 'index' else 'target'])
            self.assertEqual(self.ledger().read_bytes(), original)
            self.target['collection'] = 'events'

    def test_cleaned_dataset_can_be_renewed_with_archived_old_ledger(self):
        host.perform(self.args)
        self.args.action = 'cleanup'; host.perform(self.args)
        cleaned = self.ledger().read_bytes()
        self.data['events']['primary']['description'] += ' renewed'
        self.data['datasetSha256'] = dataset_hash(self.data)
        self.input.write_text(json.dumps(self.data))
        self.plan = points(self.data, chunks=False)
        self.args.action = 'index'; self.calls.clear()
        self.assertEqual(host.perform(self.args), 0)
        self.assertEqual(self.calls, ['plan', 'index'])
        archives = list((self.root/'state/history').glob('*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), cleaned)
        self.assertEqual(host.read_private(self.ledger())['points'], self.plan)
        self.assertEqual(host.read_private(self.attestations()[0])['datasetSha256'], self.data['datasetSha256'])

    def test_uncleaned_changed_inventory_cannot_replace_old_ledger(self):
        host.perform(self.args); original = self.ledger().read_bytes()
        self.data['events']['primary']['description'] += ' changed'
        self.data['datasetSha256'] = dataset_hash(self.data)
        self.input.write_text(json.dumps(self.data)); self.calls.clear()
        with self.assertRaisesRegex(host.SetupError, 'DATASET_STATE_CONFLICT'): host.perform(self.args)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger().read_bytes(), original)

    def test_file_and_parent_sync_finish_before_mutation_and_failure_blocks_write(self):
        events = []
        real_fsync, real_replace, real_invoke = host.os.fsync, host.os.replace, self.invoke
        def fsync(fd):
            import stat
            events.append('dir-sync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-sync')
            real_fsync(fd)
        def replace(source, destination):
            events.append('ledger-replace' if destination.parent.name == 'datasets' else 'other-replace')
            real_replace(source, destination)
        def invoke(*args):
            if args[2]['action'] == 'index':
                self.assertEqual(events[-3:], ['file-sync', 'ledger-replace', 'dir-sync'])
                events.append('mutation')
            return real_invoke(*args)
        with patch.object(host.os, 'fsync', side_effect=fsync), patch.object(host.os, 'replace', side_effect=replace), \
             patch.object(host, 'invoke', side_effect=invoke):
            host.perform(self.args)
        self.assertIn('mutation', events)
        self.args.state_dir = str(self.root / 'other-state'); self.calls.clear()
        real_sync = host.sync_directory
        def fail_after_replace(path):
            if path.name == 'datasets' and any(path.glob('*.json')):
                raise OSError('synthetic durability failure')
            real_sync(path)
        with patch.object(host, 'sync_directory', side_effect=fail_after_replace):
            with self.assertRaises(OSError): host.perform(self.args)
        self.assertEqual(self.calls, ['plan'])

    def test_new_state_directories_sync_their_parent_entries(self):
        new = self.root/'new-state'/'datasets'
        calls = []
        with patch.object(host, 'sync_directory', side_effect=lambda path: calls.append(path)):
            host.private_dir(new)
        self.assertIn(self.root, calls)
        self.assertIn(new.parent, calls)
        self.assertIn(new, calls)

    def test_worker_source_is_valid_python(self):
        compile(host.worker_source(), 'setup-worker', 'exec')


class FakeClient:
    def __init__(self): self.records, self.writes, self.deletes = {}, [], []
    def retrieve(self, collection_name, ids, **kwargs):
        return [types.SimpleNamespace(id=p, payload=self.records[p]) for p in ids if p in self.records]
    def upsert(self, collection_name, points, wait):
        assert wait is True
        for p in points:
            self.records[p.id] = p.payload; self.writes.append(p.id)
    def delete(self, collection_name, points_selector, wait):
        assert wait is True
        for p in points_selector:
            self.records.pop(p, None); self.deletes.append(p)


class WorkerStoreTests(unittest.TestCase):
    def setUp(self):
        self.data = inventory(); self.plan = points(self.data, chunks=True)
        self.client = FakeClient()
        self.store = worker.OwnedStore(self.client, 'events', self.data, self.plan, 'd'*64)
        model = types.ModuleType('qdrant_client.models')
        model.PointStruct = lambda **kw: types.SimpleNamespace(**kw)
        self.patch = patch.dict(sys.modules, {'qdrant_client.models': model})
        self.patch.start(); self.addCleanup(self.patch.stop)

    def write_all(self):
        names = {e['id']: e['title'] for e in self.data['events'].values()}
        for p in self.plan:
            self.store.upsert(p['pointId'], [0.1, 0.2], {'event_id': p['eventId'], 'name': names[p['eventId']]})

    def test_actual_point_ids_recorded_and_idempotent_cleanup(self):
        self.write_all()
        self.assertEqual(set(self.client.writes), {p['pointId'] for p in self.plan})
        self.assertEqual(set(self.store.cleanup()), set(self.client.writes))
        self.assertEqual(self.client.records, {})
        self.store.cleanup()
        self.assertEqual(len(self.client.deletes), len(self.plan))

    def test_unmarked_foreign_point_blocks_all_cleanup_before_delete(self):
        self.write_all()
        self.client.records[self.plan[-1]['pointId']] = {'event_id': self.plan[-1]['eventId'], 'name': 'foreign'}
        with self.assertRaisesRegex(worker.WorkerError, 'POINT_OWNERSHIP_CONFLICT'): self.store.cleanup()
        self.assertEqual(self.client.deletes, [])

    def test_foreign_marker_or_outside_plan_cannot_be_overwritten(self):
        p = self.plan[0]
        self.client.records[p['pointId']] = {'event_id': p['eventId'], 'name': self.data['events']['primary']['title'],
                                           'e2e_setup': {'namespace': 'foreign'}}
        with self.assertRaisesRegex(worker.WorkerError, 'POINT_OWNERSHIP_CONFLICT'):
            self.store.upsert(p['pointId'], [0.1], {'event_id': p['eventId'], 'name': self.data['events']['primary']['title']})
        with self.assertRaisesRegex(worker.WorkerError, 'POINT_WRITE_OUTSIDE_PLAN'):
            self.store.upsert('33333333-3333-4333-8333-333333333333', [0.1], {})
        self.assertEqual(self.client.writes, [])

    def test_nan_vector_cannot_be_written(self):
        p = self.plan[0]
        with self.assertRaises(worker.WorkerError):
            self.store.upsert(p['pointId'], [float('nan')], {'event_id': p['eventId'], 'name': self.data['events']['primary']['title']})
        self.assertEqual(self.client.writes, [])

    def test_no_database_guard(self):
        with self.assertRaisesRegex(worker.WorkerError, 'DATABASE_USE_FORBIDDEN'):
            worker.NoDatabase().create_event


class RuntimeWiringTests(unittest.TestCase):
    def settings(self, url='http://qdrant:6333'):
        return types.SimpleNamespace(qdrant_url=url, qdrant_host='qdrant', qdrant_port=6333,
            qdrant_api_key=None, qdrant_collection_events='events', qdrant_vector_size=2,
            embeddings_model='actual-runtime-model-reference')

    def test_runtime_binds_selected_compose_containers_without_reading_configuration(self):
        calls = []
        def tool(args):
            calls.append(args)
            if 'ps' in args:
                self.assertEqual(os.environ['CONTAINER_ENGINE'], 'podman')
                self.assertIn('own-project', args)
                return ('a' if args[-1] == 'event-ai' else 'b') * 64
            self.assertEqual(args[0:2], ['podman', 'inspect'])
            expected = '{{.Id}} {{.Image}} {{.State.Status}}'
            if args[-1] == 'a'*64:
                expected += ' {{.State.Health.Status}}'
            self.assertEqual(args[3], expected)
            return args[-1] + ' sha256:' + 'c'*64 + ' running' + (' healthy' if args[-1] == 'a'*64 else '')
        with patch.dict(os.environ, {'CONTAINER_ENGINE': 'original'}), patch.object(host, 'tool', side_effect=tool):
            container, metadata = host.runtime('podman', 'own-project')
            self.assertEqual(os.environ['CONTAINER_ENGINE'], 'original')
        self.assertEqual(container, 'a'*64)
        self.assertEqual(metadata['qdrantContainerId'], 'b'*64)
        self.assertEqual(metadata['aiImageId'], 'sha256:'+'c'*64)
        self.assertEqual(metadata['composeProject'], 'own-project')
        self.assertEqual(metadata['engine'], 'podman')
        self.assertEqual(len(calls), 4)

    def test_connection_only_reads_existing_local_collection(self):
        settings = self.settings()
        client = types.SimpleNamespace(get_collection=lambda name: types.SimpleNamespace(
            config=types.SimpleNamespace(params=types.SimpleNamespace(vectors=types.SimpleNamespace(size=2)))))
        module = types.ModuleType('app.config.settings'); module.Settings = lambda: settings
        qdrant = types.ModuleType('qdrant_client')
        from unittest.mock import Mock
        qdrant.QdrantClient = Mock(return_value=client)
        with patch.dict(sys.modules, {'app.config.settings': module, 'qdrant_client': qdrant}):
            result, configured = worker.connect_existing_collection()
        self.assertIs(result, client); self.assertIs(configured, settings)
        self.assertEqual(qdrant.QdrantClient.call_args.kwargs['url'], 'http://qdrant:6333')
        self.assertEqual(qdrant.QdrantClient.call_args.kwargs['timeout'], 15)

    def test_remote_or_credentialed_qdrant_targets_fail_before_client(self):
        for url in ['https://qdrant:6333', 'http://remote:6333', 'http://qdrant:6333/path',
                    'http://user:canary@qdrant:6333', 'http://qdrant:6333/?api_key=canary']:
            module = types.ModuleType('app.config.settings'); module.Settings = lambda: self.settings(url)
            qdrant = types.ModuleType('qdrant_client')
            from unittest.mock import Mock
            qdrant.QdrantClient = Mock()
            with patch.dict(sys.modules, {'app.config.settings': module, 'qdrant_client': qdrant}):
                with self.assertRaisesRegex(worker.WorkerError, 'QDRANT_TARGET_NOT_LOCAL'):
                    worker.connect_existing_collection()
            qdrant.QdrantClient.assert_not_called()

    def test_execute_plan_has_no_embeddings_or_writes_and_index_uses_runtime_model(self):
        data, client = inventory(), FakeClient()
        plan = points(data)
        events = {e['id']: types.SimpleNamespace(name=e['title']) for e in data['events'].values()}
        configured_models = []
        class Service:
            def __init__(self, database, vector_store, embeddings):
                self.database, self.vector_store, self.embeddings = database, vector_store, embeddings
            def index_event(self, event_id, event):
                self.vector_store.upsert(event_id, self.embeddings.embed(event.name), {'event_id': event_id, 'name': event.name})
        class Embeddings:
            def __init__(self, model_name):
                configured_models.append(model_name); self.model_name = model_name; self.device = 'cpu'
            def get_dimension(self): return 2
            def embed(self, value): return [0.25, 0.75]
        models = types.ModuleType('qdrant_client.models'); models.PointStruct = lambda **kw: types.SimpleNamespace(**kw)
        services = types.ModuleType('app.core.usecase.event_service'); services.EventService = Service
        embeddings = types.ModuleType('app.adapters.embeddings'); embeddings.SentenceTransformerAdapter = Embeddings
        import hashlib
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        request = {'action': 'plan', 'inventory': data, 'inventorySha256': digest}
        with patch.dict(sys.modules, {'app.core.usecase.event_service': services, 'app.adapters.embeddings': embeddings, 'qdrant_client.models': models}), \
             patch.object(worker, 'connect_existing_collection', return_value=(client, self.settings())), \
             patch.object(worker, 'events_for_service', return_value=events), \
             patch.object(worker, 'points_for_service', return_value=plan), \
             patch.object(worker.importlib.metadata, 'version', return_value='synthetic-unit-test'):
            prepared = worker.execute(request)
            self.assertEqual(prepared['status'], 'PLANNED')
            self.assertEqual(configured_models, []); self.assertEqual(client.writes, [])
            request.update(action='index', points=plan, indexImplementationSha256=prepared['runtime']['indexImplementationSha256'],
                           projectionTarget=prepared['projectionTarget'])
            result = worker.execute(request)
            self.assertEqual(result['status'], 'INDEXED_NOT_UI_VERIFIED')
            self.assertEqual(configured_models, ['actual-runtime-model-reference'])
            self.assertEqual(set(client.writes), {e['id'] for e in data['events'].values()})
            self.assertEqual(result['runtime']['embeddingDimension'], 2)
            self.assertNotIn('uiVerifiedAt', result)
            for action in ('index', 'cleanup'):
                request.update(action=action, projectionTarget={'qdrantTarget': 'http://qdrant:6333', 'collection': 'other'})
                before = (list(client.writes), list(client.deletes))
                with self.assertRaisesRegex(worker.WorkerError, 'PROJECTION_TARGET_CHANGED'): worker.execute(request)
                self.assertEqual((client.writes, client.deletes), before)
            request.update(action='index', projectionTarget=prepared['projectionTarget'], indexImplementationSha256='changed')
            with self.assertRaisesRegex(worker.WorkerError, 'RUNTIME_PLAN_CHANGED'): worker.execute(request)


if __name__ == '__main__': unittest.main()
