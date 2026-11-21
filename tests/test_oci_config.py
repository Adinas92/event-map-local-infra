"""Security and activation contract for pinned OCI configuration (synthetic only)."""
import base64
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oci_config", REPO / "bin/oci_config.py")
oci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oci)
MARKER = "synthetic-sensitive-marker-7db32"


class OciConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "private"
        self.doc_path = self.base / "deployment.json"
        self.doc = self.make_doc()
        self.save()
        self.calls = []

    def make_doc(self, service="event-ai", environment="local", version=1):
        name = {"event-ai": "OPENAI_API_KEY", "next-to-you": "APP_AUTH_TOKEN_SECRET",
                "next-to-you-ui": "CARTO_BASEMAPS_API_KEY"}[service]
        return {"schema_version": 1, "environment": environment, "service": service,
                "catalog_sha256": hashlib.sha256(oci.CATALOG.read_bytes()).hexdigest(),
                "region": "eu-amsterdam-1", "bundles": [{"class": "public" if service.endswith("-ui") else "secret",
                "secret_id": "ocid1.vaultsecret.oc1.eu-amsterdam-1.synthetic", "version_number": version,
                "names": [name], "required": [name]}]}

    def save(self):
        self.doc_path.write_text(json.dumps(self.doc))

    def response(self, command, **kwargs):
        self.calls.append((command, kwargs))
        bundle = self.doc["bundles"][len(self.calls) % len(self.doc["bundles"]) - 1]
        payload = {"environment": self.doc["environment"], "service": self.doc["service"],
                   "class": bundle["class"], "values": {x: MARKER for x in bundle["names"]}}
        data = {"secret-id": bundle["secret_id"], "version-number": bundle["version_number"],
                "secret-bundle-content": {"content-type": "BASE64", "content": base64.b64encode(json.dumps(payload).encode()).decode()}}
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": data}).encode(), b"")

    def sync(self, runner=None):
        return oci.sync(self.doc_path, self.root, self.doc["environment"], self.doc["service"],
                        "synthetic-profile", "security_token", runner or self.response)

    def modified_response(self, change):
        def run(command, **kwargs):
            response = self.response(command, **kwargs)
            data = json.loads(response.stdout)
            change(data)
            response.stdout = json.dumps(data).encode()
            return response
        return run

    def change_payload(self, change):
        def update(data):
            content = data["data"]["secret-bundle-content"]
            payload = json.loads(base64.b64decode(content["content"]))
            change(payload)
            content["content"] = base64.b64encode(json.dumps(payload).encode()).decode()
        return self.modified_response(update)

    def test_pinned_fetch_and_private_atomic_release(self):
        release = self.sync()
        self.assertEqual((release / "files/OPENAI_API_KEY").read_text(), MARKER)
        self.assertEqual((self.root / "local/event-ai/current").resolve(), release)
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((release / "files/OPENAI_API_KEY").stat().st_mode), 0o600)
        command, kwargs = self.calls[0]
        self.assertIn("--version-number", command)
        self.assertNotIn("--stage", command)
        self.assertNotIn(MARKER, " ".join(command))
        self.assertEqual(kwargs["env"]["OCI_CLI_RC_FILE"], os.devnull)
        self.assertNotIn(MARKER, (release / "release.json").read_text())
        self.assertEqual(list(release.parent.glob(".pending-*")), [])

    def test_rotation_and_explicit_rollback_keep_immutable_generations(self):
        old = self.sync()
        self.doc["bundles"][0]["version_number"] = 2
        self.save()
        new = self.sync()
        self.assertNotEqual(old, new)
        self.assertEqual(json.loads((old / "release.json").read_text())["descriptor"]["bundles"][0]["version_number"], 1)
        self.doc["bundles"][0]["version_number"] = 1
        self.save()
        rollback = self.sync()
        self.assertEqual((self.root / "local/event-ai/current").resolve(), rollback)
        self.assertTrue(old.is_dir() and new.is_dir())

    def test_managed_network_proxy_survives_without_oci_overrides(self):
        transport = {"HTTPS_PROXY": "http://synthetic-proxy.invalid:8080",
                     "https_proxy": "http://synthetic-proxy.invalid:8080",
                     "NO_PROXY": "localhost"}
        with patch.dict(os.environ, {**transport, "OCI_CLI_ENDPOINT": "https://untrusted.invalid",
                                     "OCI_CLI_DEBUG": "true", "OPENAI_API_KEY": MARKER}):
            self.sync()
        child = self.calls[0][1]["env"]
        for name, value in transport.items():
            self.assertEqual(child[name], value)
        for name in ["OCI_CLI_ENDPOINT", "OCI_CLI_DEBUG", "OPENAI_API_KEY"]:
            self.assertNotIn(name, child)

    def test_bad_payloads_never_activate_over_existing_generation(self):
        old = self.sync()
        mutations = [lambda p: p.update(environment="dev"), lambda p: p.update(service="next-to-you"),
                     lambda p: p.update(**{"class": "parameter"}), lambda p: p.update(values={}),
                     lambda p: p["values"].update(UNDECLARED=MARKER),
                     lambda p: p["values"].update(OPENAI_API_KEY=""),
                     lambda p: p["values"].update(OPENAI_API_KEY=123),
                     lambda p: p["values"].update(OPENAI_API_KEY="x\x00y")]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(oci.ConfigError):
                self.sync(self.change_payload(change))
            self.assertEqual((self.root / "local/event-ai/current").resolve(), old)
            self.assertEqual(len(list(old.parent.iterdir())), 1)

    def test_provider_failure_version_mismatch_and_malformed_json_fail_closed(self):
        for runner in [lambda *a, **k: subprocess.CompletedProcess(a, 1, MARKER.encode(), MARKER.encode()),
                       lambda *a, **k: subprocess.CompletedProcess(a, 0, MARKER.encode(), b""),
                       self.modified_response(lambda d: d["data"].update({"version-number": 99})),
                       self.modified_response(lambda d: d["data"].update({"secret-id": "other"}))]:
            with self.assertRaises(oci.ConfigError):
                self.sync(runner)
        self.assertFalse(self.root.exists())

    def test_catalog_uses_pydantic_aliases_and_pins_catalog_revision(self):
        bundle = self.doc["bundles"][0]
        bundle.update({"class": "parameter", "names": ["AI_SEARCH_ENABLED"], "required": ["AI_SEARCH_ENABLED"]})
        self.save()
        release = self.sync(self.change_payload(lambda p: p.update(values={"AI_SEARCH_ENABLED": "false"})))
        self.assertEqual((release / "files/AI_SEARCH_ENABLED").read_text(), "false")
        for wrong in ["ENABLED", "CANDIDATE_POOL_LIMIT", "PROVIDER"]:
            bundle.update(names=[wrong], required=[wrong])
            self.save()
            with self.assertRaisesRegex(oci.ConfigError, "CATALOG_BOUNDARY"):
                self.sync()
        self.doc["catalog_sha256"] = "outdated"
        self.save()
        with self.assertRaisesRegex(oci.ConfigError, "CATALOG_VERSION"):
            self.sync()

    def test_completed_example_descriptors_match_catalog_classification(self):
        for example in sorted((REPO / "config/oci").glob("*/*.example.json")):
            with self.subTest(example=example.name, environment=example.parent.name):
                doc = json.loads(example.read_text())
                doc["catalog_sha256"] = hashlib.sha256(oci.CATALOG.read_bytes()).hexdigest()
                self.doc_path.write_text(json.dumps(doc))
                oci.descriptor(self.doc_path, doc["environment"], doc["service"])

    def test_legacy_ide_aliases_cannot_be_silently_activated(self):
        self.doc = self.make_doc("next-to-you")
        for name in ["APP_AUTH_TOKENSECRET", "NTY_SECURITY_DEV_MODE_ENABLED", "NTY_TO_EVENT_AI_CLIENT_ID"]:
            with self.subTest(name=name):
                self.doc["bundles"][0].update({"class": "secret" if "SECRET" in name else "parameter",
                                              "names": [name], "required": [name]})
                self.save()
                with self.assertRaisesRegex(oci.ConfigError, "CATALOG_BOUNDARY"):
                    self.sync()
        self.assertFalse(self.root.exists())

    def test_output_inside_another_git_checkout_is_rejected(self):
        foreign = self.base / "other-repo"
        foreign.mkdir()
        (foreign / ".git").mkdir()
        self.root = foreign / "private-config"
        with self.assertRaisesRegex(oci.ConfigError, "OUTPUT_IN_REPOSITORY"):
            self.sync()
        self.assertFalse(self.root.exists())

    def test_duplicate_fields_rejected(self):
        with self.assertRaisesRegex(oci.ConfigError, "DUPLICATE_FIELD"):
            oci.decode_json(b'{"values":{},"values":{}}', 100)

    def test_size_limit_and_illegal_names(self):
        with self.assertRaisesRegex(oci.ConfigError, "SIZE_LIMIT"):
            self.sync(self.change_payload(lambda p: p["values"].update(OPENAI_API_KEY="a" * 25_001)))
        for name in ["../../outside", "oci_config/child", "UNDECLARED"]:
            self.doc["bundles"][0]["names"] = [name]
            self.doc["bundles"][0]["required"] = [name]
            self.save()
            with self.assertRaisesRegex(oci.ConfigError, "CATALOG_BOUNDARY"):
                self.sync()
        self.assertFalse(self.root.exists())

    def test_ui_exports_only_allowlisted_public_javascript(self):
        self.doc = self.make_doc("next-to-you-ui")
        self.save()
        release = self.sync()
        text = (release / "runtime-config.js").read_text()
        self.assertIn("cartoBasemapsApiKey", text)
        self.assertEqual(stat.S_IMODE((release / "runtime-config.js").stat().st_mode), 0o644)
        self.doc["bundles"][0].update(names=["OPENAI_API_KEY"], required=["OPENAI_API_KEY"])
        self.save()
        with self.assertRaisesRegex(oci.ConfigError, "CATALOG_BOUNDARY"):
            self.sync()
        self.doc["bundles"][0]["class"] = "secret"
        self.save()
        with self.assertRaisesRegex(oci.ConfigError, "PUBLIC_BOUNDARY"):
            self.sync()

    def test_json_string_in_ui_is_serialized_without_executable_interpolation(self):
        self.doc = self.make_doc("next-to-you-ui")
        self.save()
        value = "');throw new Error('synthetic'); //\n\""
        release = self.sync(self.change_payload(lambda p: p["values"].update(CARTO_BASEMAPS_API_KEY=value)))
        js = (release / "runtime-config.js").read_text()
        self.assertEqual(json.loads(js.removeprefix("window.eventMapConfig = ").removesuffix(";\n")),
                         {"cartoBasemapsApiKey": value})

    def test_filesystem_failure_cleans_staging_and_preserves_active_generation(self):
        old = self.sync()
        with patch.object(oci, "write_private", side_effect=OSError(MARKER)):
            with self.assertRaises(OSError):
                self.sync()
        self.assertEqual((self.root / "local/event-ai/current").resolve(), old)
        self.assertEqual(list(old.parent.glob(".pending-*")), [])

    def test_symlink_and_insecure_output_roots_are_rejected(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(oci.ConfigError, "PRIVATE_DIRECTORY_REQUIRED"):
            self.sync()
        self.root.rmdir()
        self.root.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(oci.ConfigError, "UNSAFE_PATH"):
            self.sync()

    def test_concurrent_publication_does_not_override_lock(self):
        old = self.sync()
        with oci.release_lock(old.parent.parent):
            with self.assertRaisesRegex(oci.ConfigError, "SYNC_BUSY"):
                self.sync()
        self.assertEqual((self.root / "local/event-ai/current").resolve(), old)

    def test_real_cli_boundary_never_prints_sensitive_provider_error(self):
        fake = self.base / "oci"
        fake.write_text("#!/bin/sh\nprintf '%s' '" + MARKER + "' >&2\nexit 1\n")
        fake.chmod(0o700)
        result = subprocess.run([str(REPO / "bin/secrets"), "oci", "sync", "--deployment", str(self.doc_path),
                                 "--root", str(self.root), "--environment", "local", "--service", "event-ai",
                                 "--profile", "synthetic"], capture_output=True,
                                env={**os.environ, "PATH": str(self.base) + os.pathsep + os.environ["PATH"]})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"BLOCKED: OCI_READ_FAILED\n")
        self.assertNotIn(MARKER.encode(), result.stdout + result.stderr)
        self.assertFalse(self.root.exists())

    def test_compose_requires_resolved_matching_generations(self):
        releases = []
        for service in sorted(oci.SERVICES):
            self.doc = self.make_doc(service)
            self.save()
            releases.append(self.sync())
        calls = []
        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        oci.compose(releases, "local", ["config", "--quiet"], runner)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(MARKER, str(calls))
        with self.assertRaises(oci.ConfigError):
            oci.compose(releases, "dev", ["up"], runner)
        with self.assertRaisesRegex(oci.ConfigError, "PIN_GENERATION_PATH"):
            oci.compose([releases[0].parent.parent / "current"] + releases[1:], "local", ["up"], runner)
        (releases[0] / "files" / self.make_doc(sorted(oci.SERVICES)[0])["bundles"][0]["names"][0]).unlink()
        with self.assertRaisesRegex(oci.ConfigError, "INCOMPLETE_RELEASE"):
            oci.compose(releases, "local", ["up"], runner)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI unavailable")
    def test_actual_compose_merge_removes_base_environment_and_secret_values(self):
        env = {**os.environ, "OCI_AI_RELEASE": str(self.base / "ai"), "OCI_BE_RELEASE": str(self.base / "be"),
               "OCI_UI_RELEASE": str(self.base / "ui"), "EVENT_MAP_SECRETS_DIR": str(self.base / "legacy")}
        result = subprocess.run(["docker", "compose", "-f", str(REPO / "compose.yaml"), "-f", str(REPO / "compose.oci.yaml"),
                                 "config", "--format", "json"], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config["services"]["event-ai"]["environment"],
                         {"EVENT_MAP_SECRETS_DIR": "/run/secrets", "CRAWL4_AI_BASE_DIRECTORY": "/tmp"})
        backend = config["services"]["next-to-you"]["environment"]
        self.assertNotIn("DB_PASSWORD", backend)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", backend)
        self.assertEqual(backend["SPRING_CONFIG_IMPORT"], "configtree:/run/secrets/")
        self.assertFalse(config["services"]["next-to-you-ui"].get("environment"))
        self.assertNotIn(MARKER.encode(), result.stdout + result.stderr)
        for service in ["event-ai", "next-to-you"]:
            mount = next(v for v in config["services"][service]["volumes"] if v["target"] == "/run/secrets")
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])
