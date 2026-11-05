"""Container-side setup only. No database, bootstrap, crawler, LLM, or HTTP API."""
import contextlib
import hashlib
import importlib.metadata
import inspect
import json
import logging
import math
import os
import re
import signal
import sys
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fixture_contract import validate_inventory, validate_points


class WorkerError(Exception):
    pass


class NoDatabase:
    def __getattr__(self, _name):
        raise WorkerError('DATABASE_USE_FORBIDDEN')


def events_for_service(inventory):
    from app.core.domain import Event, EventSource
    events = {}
    for item in inventory['events'].values():
        events[item['id']] = Event(
            url=f"urn:event-map:e2e:{inventory['namespace']}:{item['id']}",
            name=item['title'], name_en=item['title'], description_en=item['description'],
            address=item['address'], latitude=float(item['latitude']), longitude=float(item['longitude']),
            city=inventory['location']['city'], city_slug=inventory['location']['city'].strip().lower(),
            event_date=datetime.fromisoformat(item['start']).replace(tzinfo=ZoneInfo(inventory['timezone'])),
            source=EventSource.MANUAL,
        )
    return events


def points_for_service(service, events):
    from app.core.usecase.chunking_service import build_chunk_point_id
    points = []
    for event_id, event in events.items():
        title = service._build_bilingual_title(event)
        description = service._build_bilingual_description(event)
        if service.chunking_service.should_chunk_event(title, description):
            chunks = service.chunking_service.chunk_event(event_id, title, description, '', event.url)
            if not chunks or any(not chunk.content.strip() for chunk in chunks):
                raise WorkerError('CHUNK_PLAN_INVALID')
            points.extend({'pointId': build_chunk_point_id(event_id, c.chunk_index),
                           'eventId': event_id, 'chunkIndex': c.chunk_index} for c in chunks)
        else:
            points.append({'pointId': event_id, 'eventId': event_id, 'chunkIndex': None})
    return points


class OwnedStore:
    """Only explicit, deterministic IDs with this setup's ownership marker may change."""
    def __init__(self, client, collection, inventory, points, digest):
        self.client, self.collection = client, collection
        self.allowed = {p['pointId']: p for p in points}
        self.names = {e['id']: e['title'] for e in inventory['events'].values()}
        self.marker = {'namespace': inventory['namespace'], 'datasetSha256': inventory['datasetSha256'],
                       'inventorySha256': digest}
        self.written = []

    def inspect_owned(self, ids):
        found = self.client.retrieve(collection_name=self.collection, ids=ids, with_payload=True, with_vectors=False)
        for record in found:
            point = self.allowed.get(str(record.id))
            payload = record.payload or {}
            if (not point or payload.get('event_id') != point['eventId']
                    or payload.get('e2e_setup') != self.marker
                    or payload.get('name') != self.names[point['eventId']]):
                raise WorkerError('POINT_OWNERSHIP_CONFLICT')
        return {str(record.id) for record in found}

    def upsert(self, point_id, vector, payload):
        point = self.allowed.get(point_id)
        if (not point or payload.get('event_id') != point['eventId']
                or payload.get('name') != self.names[point['eventId']]
                or not vector or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in vector)):
            raise WorkerError('POINT_WRITE_OUTSIDE_PLAN')
        self.inspect_owned([point_id])
        from qdrant_client.models import PointStruct
        marked = dict(payload, e2e_setup=self.marker)
        self.client.upsert(collection_name=self.collection,
                           points=[PointStruct(id=point_id, vector=vector, payload=marked)], wait=True)
        self.written.append(point_id)

    def cleanup(self):
        ids = list(self.allowed)
        present = self.inspect_owned(ids)  # Validate the whole set before the first delete.
        for point_id in ids:
            if point_id in present:
                self.inspect_owned([point_id])
                self.client.delete(collection_name=self.collection, points_selector=[point_id], wait=True)
        if self.client.retrieve(collection_name=self.collection, ids=ids, with_payload=False, with_vectors=False):
            raise WorkerError('CLEANUP_UNCONFIRMED')
        return ids


def projection_target(settings):
    if settings.qdrant_url:
        target = urlsplit(settings.qdrant_url)
        if (target.scheme != 'http' or target.hostname not in {'qdrant', 'localhost', '127.0.0.1'}
                or target.port != 6333 or target.username or target.password or target.query or target.fragment
                or target.path not in {'', '/'}):
            raise WorkerError('QDRANT_TARGET_NOT_LOCAL')
        host = target.hostname
    else:
        if settings.qdrant_host not in {'qdrant', 'localhost', '127.0.0.1'} or settings.qdrant_port != 6333:
            raise WorkerError('QDRANT_TARGET_NOT_LOCAL')
        host = settings.qdrant_host
    collection = settings.qdrant_collection_events
    if not isinstance(collection, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', collection):
        raise WorkerError('QDRANT_COLLECTION_INVALID')
    return {'qdrantTarget': f'http://{host}:6333', 'collection': collection}


def connect_existing_collection():
    # Normal service configuration stays inside its container. Never serialize settings.
    from app.config.settings import Settings
    from qdrant_client import QdrantClient
    settings = Settings()
    projection_target(settings)
    if settings.qdrant_url:
        client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=15)
    else:
        client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port,
                              api_key=settings.qdrant_api_key, timeout=15)
    # Never create/recreate collections or payload indexes as a fixture side effect.
    info = client.get_collection(settings.qdrant_collection_events)
    if isinstance(info.config.params.vectors, dict):
        raise WorkerError('NAMED_VECTORS_UNSUPPORTED')
    if info.config.params.vectors.size != settings.qdrant_vector_size:
        raise WorkerError('VECTOR_DIMENSION_MISMATCH')
    return client, settings


def execute(request):
    action = request['action']
    if action not in {'target', 'plan', 'index', 'cleanup'}:
        raise WorkerError('ACTION_INVALID')
    inv = validate_inventory(request['inventory'], request['inventory']['baseURL'], allow_expired=action in {'cleanup', 'target'})
    digest = hashlib.sha256(json.dumps(inv, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if digest != request['inventorySha256']:
        raise WorkerError('INVENTORY_CHANGED')
    client, settings = connect_existing_collection()
    target = projection_target(settings)
    if action == 'target':
        return {'complete': True, 'status': 'TARGET_VALIDATED', 'projectionTarget': target}
    if action in {'index', 'cleanup'} and request.get('projectionTarget') != target:
        raise WorkerError('PROJECTION_TARGET_CHANGED')
    from app.core.usecase.event_service import EventService
    implementation = hashlib.sha256(inspect.getsource(EventService.index_event).encode()).hexdigest()
    metadata = {'python': sys.version.split()[0], 'indexImplementationSha256': implementation,
                'qdrantClient': importlib.metadata.version('qdrant-client'),
                'embeddingModel': 'not-loaded', 'modelRevision': 'unknown (runtime cache not attested)'}
    if action == 'cleanup':
        points = request['points']
        validate_points(points, inv)
        store = OwnedStore(client, settings.qdrant_collection_events, inv, points, digest)
        store.cleanup()
        return {'complete': True, 'status': 'CLEANED', 'points': points, 'runtime': metadata, 'projectionTarget': target}
    events = events_for_service(inv)
    service = EventService(NoDatabase(), None, None)
    points = points_for_service(service, events)
    validate_points(points, inv)
    store = OwnedStore(client, settings.qdrant_collection_events, inv, points, digest)
    store.inspect_owned([p['pointId'] for p in points])
    if action == 'plan':
        return {'complete': True, 'status': 'PLANNED', 'points': points, 'runtime': metadata, 'projectionTarget': target}
    if points != request['points'] or implementation != request['indexImplementationSha256']:
        raise WorkerError('RUNTIME_PLAN_CHANGED')
    from app.adapters.embeddings import SentenceTransformerAdapter
    embeddings = SentenceTransformerAdapter(model_name=settings.embeddings_model)
    if embeddings.get_dimension() != settings.qdrant_vector_size:
        raise WorkerError('EMBEDDING_DIMENSION_MISMATCH')
    service.embeddings, service.vector_store = embeddings, store
    for event_id, event in events.items():
        service.index_event(event_id, event)
    if set(store.written) != set(store.allowed) or len(store.written) != len(store.allowed):
        raise WorkerError('INDEX_PLAN_INCOMPLETE')
    if store.inspect_owned(list(store.allowed)) != set(store.allowed):
        raise WorkerError('INDEX_CONFIRMATION_MISSING')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', embeddings.model_name):
        raise WorkerError('EMBEDDING_MODEL_METADATA_INVALID')
    metadata.update(embeddingModel=embeddings.model_name, embeddingDevice=embeddings.device,
                    embeddingDimension=embeddings.get_dimension(),
                    sentenceTransformers=importlib.metadata.version('sentence-transformers'))
    return {'complete': True, 'status': 'INDEXED_NOT_UI_VERIFIED', 'points': points, 'runtime': metadata, 'projectionTarget': target}


def main():
    # A kernel alarm terminates even a blocked native model call. The host retains locks
    # whenever it cannot prove that the remote worker has finished.
    logging.disable(logging.CRITICAL)
    request = json.loads(sys.stdin.read(1_000_001))
    seconds = request.get('maxSeconds')
    if type(seconds) is not int or not 1 <= seconds <= 900:
        raise WorkerError('WORKER_TIMEOUT_INVALID')
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)
    output = sys.stdout
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            result = execute(request)
        except Exception as error:
            safe = str(error) if isinstance(error, WorkerError) else getattr(error, 'code', 'INDEX_WORKER_FAILED')
            if not isinstance(safe, str) or not safe.replace('_', '').isupper():
                safe = 'INDEX_WORKER_FAILED'
            result = {'complete': True, 'status': 'BLOCKED', 'reason': safe}
    signal.alarm(0)
    output.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    output.flush()


if __name__ == '__main__':
    main()
