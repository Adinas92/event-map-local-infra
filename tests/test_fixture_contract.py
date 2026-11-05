"""Synthetic contract tests; no runtime, files, or operator inventory required."""

import copy
import hashlib
import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid5

from lib.fixture_contract import (
    ContractError, dataset_hash, lock_keys, point_id, validate_inventory,
    validate_owner, validate_points,
)


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
BASE_URL = "http://localhost:4200/"
IDS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")


def inventory():
    namespace = "e2e-0123456789abcdef"
    location = {"city": "Warsaw", "address": "plac Defilad 1, Warsaw", "latitude": "52.2319", "longitude": "21.0067"}

    def event(index, category, access):
        return {"id": IDS[index], "title": namespace + (" primary" if index == 0 else " contrast"),
                "description": "Synthetic zażółć 😀 " + namespace,
                "start": "2030-01-10T18:00", "end": "2030-01-10T20:00",
                "address": location["address"], "latitude": location["latitude"],
                "longitude": location["longitude"], "category": category, "access": access}

    value = {"schemaVersion": 1, "baseURL": BASE_URL, "namespace": namespace,
             "datasetSha256": "", "timezone": "Europe/Warsaw", "location": location,
             "events": {"primary": event(0, "MUSIC", "FREE"), "secondary": event(1, "THEATER", "PAID")},
             "validUntil": "2030-01-05T12:00:00.000Z"}
    value["datasetSha256"] = dataset_hash(value)
    return value


def plan(event_ids=IDS, chunks=(None,)):
    return [{"pointId": point_id(event_id, chunk), "eventId": event_id, "chunkIndex": chunk}
            for event_id in event_ids for chunk in chunks]


class InventoryTests(unittest.TestCase):
    def assert_invalid(self, value, code="FIXTURE_INVALID", **kwargs):
        with self.assertRaises(ContractError) as caught:
            validate_inventory(value, BASE_URL, now=NOW, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_returns_same_mapping_and_preserves_every_value_and_key_order(self):
        value = inventory()
        before = copy.deepcopy(value)
        key_order = list(value["events"]["primary"])
        self.assertIs(value, validate_inventory(value, BASE_URL, now=NOW))
        self.assertEqual(before, value)
        self.assertEqual(key_order, list(value["events"]["primary"]))
        self.assertEqual("Warsaw", value["location"]["city"])

    def test_base_url_trailing_slash_is_optional_but_output_is_unchanged(self):
        for exported in (BASE_URL, BASE_URL.rstrip("/")):
            for cli in (BASE_URL, BASE_URL.rstrip("/")):
                with self.subTest(exported=exported, cli=cli):
                    value = inventory()
                    value["baseURL"] = exported
                    self.assertIs(value, validate_inventory(value, cli, now=NOW))
                    self.assertEqual(exported, value["baseURL"])

    def test_base_url_aliases_must_match_exactly_except_final_slash(self):
        value = inventory()
        value["baseURL"] = "http://127.0.0.1:4200/"
        self.assert_invalid(value)
        self.assertIs(value, validate_inventory(value, value["baseURL"], now=NOW))

    def test_rejects_noncanonical_or_remote_targets(self):
        for url in ("http://localhost:4200//", "http://LOCALHOST:4200/", "http://localhost:4200/?x=1",
                    "http://user:secret@localhost:4200/", "http://localhost:4200/#x", "https://localhost:4200/",
                    "http://localhost:80/", "http://example.test:4200/", "http://[::1]:4200/", None):
            with self.subTest(url=url):
                value = inventory()
                value["baseURL"] = url
                self.assert_invalid(value)

    def test_exact_fields_at_every_level(self):
        paths = ((), ("location",), ("events",), ("events", "primary"), ("events", "secondary"))
        for path in paths:
            for operation in ("extra", "missing"):
                with self.subTest(path=path, operation=operation):
                    value = inventory()
                    target = value
                    for key in path:
                        target = target[key]
                    if operation == "extra":
                        target["actors"] = {"password": "must-never-appear-in-error"}
                    else:
                        del target[next(iter(target))]
                    self.assert_invalid(value)

    def test_rejects_wrong_shapes_and_schema(self):
        for value in (None, [], "inventory", 1, True):
            self.assert_invalid(value)
        for key, wrong in (("schemaVersion", True), ("schemaVersion", 1.0), ("schemaVersion", 2),
                           ("events", []), ("location", None), ("timezone", "UTC")):
            with self.subTest(key=key, wrong=wrong):
                value = inventory()
                value[key] = wrong
                self.assert_invalid(value)

    def test_namespace_and_hash_formats_and_digest_mismatch(self):
        for bad in ("e2e-abc", "e2e-" + "f" * 33, "e2e-ABCDEF012345", "x\ne2e-0123456789ab", None):
            value = inventory()
            value["namespace"] = bad
            self.assert_invalid(value)
        for bad in (None, "a" * 63, "A" * 64, "g" * 64):
            value = inventory()
            value["datasetSha256"] = bad
            self.assert_invalid(value)
        value = inventory()
        value["events"]["primary"]["description"] += " changed"
        self.assert_invalid(value, "FIXTURE_DATASET_MISMATCH")

    def test_ids_must_be_distinct_canonical_lowercase_rfc_versions_one_to_five(self):
        for bad in (IDS[1], IDS[0].upper().replace("11111111", "ABCDEFAB"), "{" + IDS[0] + "}",
                    IDS[0].replace("4111", "6111"), IDS[0].replace("8111", "7111"), None):
            value = inventory()
            value["events"]["primary"]["id"] = bad
            self.assert_invalid(value)
        for version in range(1, 6):
            value = inventory()
            value["events"]["primary"]["id"] = IDS[0].replace("4111", str(version) + "111")
            value["datasetSha256"] = dataset_hash(value)
            validate_inventory(value, BASE_URL, now=NOW)

    def test_text_bounds_and_namespace_binding(self):
        for key, bad in (("title", "wrong title"), ("title", "x" * 301), ("title", None),
                         ("description", "unrelated"), ("description", "x" * 16001),
                         ("description", "e2e-0123456789abcdef\ud800")):
            value = inventory()
            value["events"]["primary"][key] = bad
            self.assert_invalid(value)
        for key, bad in (("city", ""), ("city", " " * 5), ("city", "x" * 301),
                         ("address", "x" * 1001), ("address", None)):
            value = inventory()
            value["location"][key] = bad
            self.assert_invalid(value)

    def test_coordinate_strings_are_finite_bounded_and_preserved_exactly(self):
        for key, bad in (("latitude", "90.01"), ("latitude", "-90.01"), ("longitude", "180.01"),
                         ("latitude", 52.2), ("latitude", "NaN"), ("latitude", "Infinity"),
                         ("latitude", "1e1"), ("latitude", " 52.2"), ("latitude", "+52.2"),
                         ("latitude", "9" * 33), ("longitude", "21,0")):
            value = inventory()
            value["location"][key] = bad
            self.assert_invalid(value)
        value = inventory()
        for target in (value["location"], *value["events"].values()):
            target["latitude"], target["longitude"] = "-90.000", "180.0"
        value["datasetSha256"] = dataset_hash(value)
        validate_inventory(value, BASE_URL, now=NOW)
        self.assertEqual("-90.000", value["location"]["latitude"])

    def test_location_and_role_properties_must_match(self):
        for key, bad in (("address", "elsewhere"), ("latitude", "52.23190"), ("longitude", "21.00670"),
                         ("category", "THEATER"), ("access", "PAID")):
            value = inventory()
            value["events"]["primary"][key] = bad
            self.assert_invalid(value)
        value = inventory()
        value["events"]["secondary"]["title"] = value["events"]["primary"]["title"]
        self.assert_invalid(value)

    def test_local_dates_are_strict_real_dates_and_end_follows_start(self):
        for bad in ("2030-02-30T18:00", "2030-01-10T24:00", "2030-1-10T18:00", "2030-01-10T18:00Z",
                    "2030-01-10T18:00:00", "2030-03-31T02:30", None):
            value = inventory()
            value["events"]["primary"]["start"] = bad
            self.assert_invalid(value, allow_expired=True)
        for end in ("2030-01-10T18:00", "2030-01-10T17:59"):
            value = inventory()
            value["events"]["primary"]["end"] = end
            self.assert_invalid(value)

    def test_expiry_is_strict_and_cleanup_only_bypasses_time_window(self):
        value = inventory()
        value["validUntil"] = "2030-01-01T12:00:00Z"
        self.assert_invalid(value, "FIXTURE_EXPIRED")
        self.assertIs(value, validate_inventory(value, BASE_URL, now=NOW, allow_expired=True))
        value["events"]["primary"]["start"] = "2029-12-30T18:00"
        value["events"]["primary"]["end"] = "2029-12-30T20:00"
        value["datasetSha256"] = dataset_hash(value)
        validate_inventory(value, BASE_URL, now=NOW, allow_expired=True)
        value["events"]["primary"]["end"] = "bad-date"
        self.assert_invalid(value, allow_expired=True)

    def test_expiry_date_always_requires_a_real_utc_timestamp(self):
        for bad in ("2030-02-30T12:00:00Z", "2030-01-05T12:00Z", "2030-01-05T12:00:00+00:00",
                    "2030-01-05T12:00:00.1Z", "2030-01-05", None):
            value = inventory()
            value["validUntil"] = bad
            self.assert_invalid(value, allow_expired=True)

    def test_event_day_is_after_valid_until_in_warsaw_including_midnight(self):
        value = inventory()
        value["validUntil"] = "2030-01-09T23:30:00Z"  # January 10 in Warsaw.
        self.assert_invalid(value, "FIXTURE_EVENT_WINDOW_EXPIRED")
        validate_inventory(value, BASE_URL, now=NOW, allow_expired=True)
        value["validUntil"] = "2030-01-09T22:30:00Z"
        validate_inventory(value, BASE_URL, now=NOW)

    def test_rejects_naive_clock_and_nonboolean_cleanup_flag(self):
        for now in (datetime(2030, 1, 1), "2030-01-01"):
            with self.assertRaises(ContractError):
                validate_inventory(inventory(), BASE_URL, now=now)
        self.assert_invalid(inventory(), allow_expired=1)


class HashAndLockTests(unittest.TestCase):
    def test_hash_is_utf8_compact_json_projection_not_sorted_or_ascii_escaped(self):
        value = {"namespace": "e2e-0123456789ab", "events": {"secondary": {"title": "Zażółć 😀", "id": "b"}, "primary": {"id": "a"}}}
        expected_json = '{"namespace":"e2e-0123456789ab","events":{"secondary":{"title":"Zażółć 😀","id":"b"},"primary":{"id":"a"}}}'
        self.assertEqual(hashlib.sha256(expected_json.encode("utf-8")).hexdigest(), dataset_hash(value))
        original = dataset_hash(value)
        value["unknownUnhashedField"] = {"actors": "synthetic"}
        self.assertEqual(original, dataset_hash(value))
        value["events"]["secondary"] = {"id": "b", "title": "Zażółć 😀"}
        self.assertNotEqual(original, dataset_hash(value))

    def test_hash_failure_is_safe(self):
        for value in (None, {}, {"namespace": "test", "events": float("nan")}, {"namespace": "test", "events": "\ud800"}):
            with self.assertRaises(ContractError) as caught:
                dataset_hash(value)
            self.assertEqual("FIXTURE_INVALID", str(caught.exception))

    def test_lock_keys_match_e2e_namespace_and_exact_events_across_loopback_aliases(self):
        value = inventory()
        origin = "http://127.0.0.1:4200"
        expected = sorted(hashlib.sha256((origin + "\n" + suffix).encode()).hexdigest()
                          for suffix in (value["namespace"], "event:" + IDS[0], "event:" + IDS[1]))
        for url in (BASE_URL, BASE_URL.rstrip("/"), origin, origin + "/"):
            value["baseURL"] = url
            self.assertEqual(expected, lock_keys(value))
        changed_namespace = copy.deepcopy(value)
        changed_namespace["namespace"] = "e2e-fedcba9876543210"
        self.assertEqual(2, len(set(expected) & set(lock_keys(changed_namespace))))
        changed_event = copy.deepcopy(value)
        changed_event["events"]["primary"]["id"] = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(2, len(set(expected) & set(lock_keys(changed_event))))

    def test_lock_keys_reject_invalid_target_missing_or_duplicate_ids(self):
        for mutation in (lambda v: v.update(baseURL="http://remote:4200"),
                         lambda v: v["events"]["primary"].update(id=IDS[1]),
                         lambda v: v["events"].pop("primary")):
            value = inventory()
            mutation(value)
            with self.assertRaises(ContractError):
                lock_keys(value)


class PointAndOwnerTests(unittest.TestCase):
    def test_event_and_chunk_ids_follow_uuid_formula_with_inclusive_limits(self):
        self.assertEqual(IDS[0], point_id(IDS[0], None))
        for index in (0, 1, 255):
            self.assertEqual(str(uuid5(UUID(IDS[0]), "chunk:" + str(index))), point_id(IDS[0], index))
        self.assertNotEqual(point_id(IDS[0], 0), point_id(IDS[1], 0))

    def test_invalid_chunk_types_bounds_and_event_ids_are_safe(self):
        for index in (-1, 256, True, False, 0.0, "0", [], {}):
            with self.assertRaises(ContractError) as caught:
                point_id(IDS[0], index)
            self.assertEqual("FIXTURE_POINTS_INVALID", str(caught.exception))
        with self.assertRaises(ContractError):
            point_id("private-input-not-uuid", None)

    def test_accepts_event_points_chunk_points_and_maximum_plan(self):
        for points in (plan(), plan(chunks=(0, 1)), plan(chunks=range(256)), plan(chunks=(None, 0, 255))):
            self.assertIsNone(validate_points(points, inventory()))
        points = plan()
        points.reverse()
        validate_points(points, inventory())

    def test_rejects_missing_events_duplicates_unknown_ids_and_wrong_ids(self):
        invalid = [None, {}, [], plan(event_ids=(IDS[0],)), plan() * 2, plan() * 257]
        wrong_id = plan()
        wrong_id[0]["pointId"] = IDS[1]
        invalid.append(wrong_id)
        unknown = plan()
        unknown[0]["eventId"] = "33333333-3333-4333-8333-333333333333"
        unknown[0]["pointId"] = unknown[0]["eventId"]
        invalid.append(unknown)
        for points in invalid:
            with self.subTest(points=repr(points)[:100]):
                with self.assertRaises(ContractError) as caught:
                    validate_points(points, inventory())
                self.assertEqual("FIXTURE_POINTS_INVALID", str(caught.exception))

    def test_point_records_have_exact_fields_and_strict_chunk_values(self):
        for mutation in (lambda p: p[0].update(extra="no"), lambda p: p[0].pop("chunkIndex"),
                         lambda p: p[0].update(chunkIndex=True), lambda p: p[0].update(pointId=[]),
                         lambda p: p.__setitem__(0, [])):
            points = plan()
            mutation(points)
            with self.assertRaises(ContractError):
                validate_points(points, inventory())

    def test_owner_safe_ref_bounds(self):
        for owner in ("local-operator", "Owner.name/path_1", "a", "a" * 160):
            self.assertEqual(owner, validate_owner(owner))
        for owner in (None, "", "a" * 161, " leading", "/leading", "a\n", "a@b", "a:secret", "Zażółć"):
            with self.assertRaises(ContractError) as caught:
                validate_owner(owner)
            self.assertEqual("FIXTURE_OWNER_INVALID", str(caught.exception))

    def test_contract_error_never_exposes_unsafe_code(self):
        for unsafe in ("private secret", "a\npassword", None, "A" * 81):
            error = ContractError(unsafe)
            self.assertEqual("FIXTURE_INVALID", error.code)
            self.assertEqual(error.code, str(error))


if __name__ == "__main__":
    unittest.main()
