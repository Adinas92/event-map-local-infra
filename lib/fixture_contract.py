"""Pure validation for UI-exported local index inventories.

A matching digest validates the supplied dataset, not DOM provenance or ownership.
No function in this module reads files, contacts services, or mutates an index.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo


class ContractError(Exception):
    """A safe diagnostic code, with no input data in the message."""

    def __init__(self, code: str = "FIXTURE_INVALID") -> None:
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "FIXTURE_INVALID"
        super().__init__(self.code)


_TOP_FIELDS = {"schemaVersion", "baseURL", "namespace", "datasetSha256", "timezone", "location", "events", "validUntil"}
_LOCATION_FIELDS = {"city", "address", "latitude", "longitude"}
_EVENT_FIELDS = {"id", "title", "description", "start", "end", "address", "latitude", "longitude", "category", "access"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_LOCAL_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}")
_UTC_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z")
_WARSAW = ZoneInfo("Europe/Warsaw")


def _require(condition: bool, code: str = "FIXTURE_INVALID") -> None:
    if not condition:
        raise ContractError(code)


def _fields(value: object, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value))


def _base_url(value: object) -> str:
    _require(isinstance(value, str) and value in {
        "http://localhost:4200", "http://localhost:4200/",
        "http://127.0.0.1:4200", "http://127.0.0.1:4200/",
    })
    return value.rstrip("/") + "/"


def _event_id(value: object, code: str = "FIXTURE_INVALID") -> str:
    _require(isinstance(value, str) and _UUID.fullmatch(value) is not None, code)
    return value


def _coordinate(value: object, bound: int) -> bool:
    if not isinstance(value, str) or len(value) > 32 or re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value) is None:
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and abs(number) <= bound
    except InvalidOperation:
        return False


def _local_time(value: object) -> datetime:
    _require(isinstance(value, str) and _LOCAL_DATE.fullmatch(value) is not None)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=_WARSAW)
        # Reject nonexistent wall times in the spring DST gap.
        _require(parsed.astimezone(timezone.utc).astimezone(_WARSAW).replace(tzinfo=None) == parsed.replace(tzinfo=None))
        return parsed
    except ValueError:
        raise ContractError() from None


def _utc_time(value: object) -> datetime:
    _require(isinstance(value, str) and _UTC_DATE.fullmatch(value) is not None)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ContractError() from None


def dataset_hash(inventory: dict) -> str:
    """Hash the JSON.stringify-compatible projection, preserving object key order."""
    try:
        payload = {"namespace": inventory["namespace"], "events": inventory["events"]}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ContractError() from None


def validate_inventory(value: object, base_url: str, allow_expired: bool = False,
                       now: datetime | None = None) -> dict:
    """Validate schema 1 and return the original mapping without normalization.

    Cleanup may bypass expiration/window checks, but all date syntax and remaining
    constraints are still checked. The caller must separately authorize mutation.
    """
    _require(_fields(value, _TOP_FIELDS))
    _require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1)
    _require(_base_url(value["baseURL"]) == _base_url(base_url))
    namespace = value["namespace"]
    _require(isinstance(namespace, str) and re.fullmatch(r"e2e-[a-f0-9]{12,32}", namespace) is not None)
    _require(isinstance(value["datasetSha256"], str) and re.fullmatch(r"[a-f0-9]{64}", value["datasetSha256"]) is not None)
    _require(value["timezone"] == "Europe/Warsaw")
    _require(type(allow_expired) is bool)
    if now is None:
        now = datetime.now(timezone.utc)
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    valid_until = _utc_time(value["validUntil"])
    location = value["location"]
    _require(_fields(location, _LOCATION_FIELDS))
    _require(_text(location["city"], 300) and _text(location["address"], 1000))
    _require(_coordinate(location["latitude"], 90) and _coordinate(location["longitude"], 180))
    events = value["events"]
    _require(_fields(events, {"primary", "secondary"}))
    ids = []
    starts = []
    for role, category, access in (("primary", "MUSIC", "FREE"), ("secondary", "THEATER", "PAID")):
        event = events[role]
        _require(_fields(event, _EVENT_FIELDS))
        ids.append(_event_id(event["id"]))
        _require(_text(event["title"], 300) and event["title"].startswith(namespace + " "))
        _require(_text(event["description"], 16000) and namespace in event["description"])
        start, end = _local_time(event["start"]), _local_time(event["end"])
        _require(end > start)
        starts.append(start)
        _require(all(event[key] == location[key] for key in ("address", "latitude", "longitude")))
        _require(event["category"] == category and event["access"] == access)
    _require(len(set(ids)) == 2 and events["primary"]["title"] != events["secondary"]["title"])
    _require(value["datasetSha256"] == dataset_hash(value), "FIXTURE_DATASET_MISMATCH")
    if not allow_expired:
        _require(valid_until > now, "FIXTURE_EXPIRED")
        today = now.astimezone(_WARSAW).date()
        valid_day = valid_until.astimezone(_WARSAW).date()
        _require(all(start > now and start.date() > today and start.date() > valid_day for start in starts),
                 "FIXTURE_EVENT_WINDOW_EXPIRED")
    return value


def lock_keys(inventory: dict) -> list[str]:
    """Return ordered namespace/event lock names shared across loopback aliases."""
    try:
        _base_url(inventory["baseURL"])
        namespace = inventory["namespace"]
        _require(isinstance(namespace, str) and re.fullmatch(r"e2e-[a-f0-9]{12,32}", namespace) is not None)
        ids = [_event_id(inventory["events"][role]["id"]) for role in ("primary", "secondary")]
        _require(len(set(ids)) == 2)
    except (KeyError, TypeError):
        raise ContractError() from None
    origin = "http://127.0.0.1:4200"
    return sorted(hashlib.sha256((origin + "\n" + suffix).encode("utf-8")).hexdigest()
                  for suffix in [namespace, *("event:" + event_id for event_id in ids)])


def point_id(event_id: str, chunk_index: int | None) -> str:
    """Match EventService's UUID/event and UUID5/chunk point identifiers."""
    event_id = _event_id(event_id, "FIXTURE_POINTS_INVALID")
    if chunk_index is None:
        return event_id
    _require(type(chunk_index) is int and 0 <= chunk_index <= 255, "FIXTURE_POINTS_INVALID")
    return str(uuid5(UUID(event_id), "chunk:" + str(chunk_index)))


def validate_points(points: object, inventory: dict) -> None:
    """Validate a bounded, complete deterministic plan for exactly two events."""
    code = "FIXTURE_POINTS_INVALID"
    _require(isinstance(points, list) and 0 < len(points) <= 512, code)
    try:
        expected = {_event_id(inventory["events"][role]["id"], code) for role in ("primary", "secondary")}
    except (KeyError, TypeError):
        raise ContractError(code) from None
    _require(len(expected) == 2, code)
    seen = set()
    present = set()
    for point in points:
        _require(_fields(point, {"pointId", "eventId", "chunkIndex"}), code)
        event_id = _event_id(point["eventId"], code)
        _require(event_id in expected, code)
        deterministic = point_id(event_id, point["chunkIndex"])
        _require(isinstance(point["pointId"], str) and point["pointId"] == deterministic and deterministic not in seen, code)
        seen.add(deterministic)
        present.add(event_id)
    _require(present == expected, code)


def validate_owner(value: object) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,159}", value) is not None,
             "FIXTURE_OWNER_INVALID")
    return value
