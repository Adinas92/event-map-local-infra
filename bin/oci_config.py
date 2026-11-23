#!/usr/bin/env python3
"""Fetch pinned OCI configuration without exposing values to the caller."""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "config/oci-config-catalog.json"
SERVICES = {"event-ai", "next-to-you", "next-to-you-ui"}
MAX_BUNDLE = 25_000
MAX_RESPONSE = 100_000


class ConfigError(Exception):
    """Only a static code is allowed to escape the configuration boundary."""


def reject(code):
    raise ConfigError(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("DUPLICATE_FIELD")
        result[key] = value
    return result


def decode_json(raw, limit):
    if len(raw) > limit:
        reject("SIZE_LIMIT")
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        reject("INVALID_JSON")


def load_json(path, limit=100_000):
    with open(path, "rb") as stream:
        return decode_json(stream.read(limit + 1), limit)


def exact_fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        reject("SCHEMA")


def load_catalog():
    raw = CATALOG.read_bytes()
    services = {}
    for entry in decode_json(raw, 100_000)["entries"]:
        services.setdefault(entry["service"], {})[entry["name"]] = entry
    return hashlib.sha256(raw).hexdigest(), services


def descriptor(path, environment, service, catalog=None):
    doc = load_json(path)
    exact_fields(doc, ["schema_version", "environment", "service", "region", "bundles", "catalog_sha256"])
    if (type(doc["schema_version"]) is not int or doc["schema_version"] != 1
            or environment not in {"local", "dev"} or service not in SERVICES
            or doc["environment"] != environment or doc["service"] != service
            or not isinstance(doc["region"], str)
            or not re.fullmatch(r"[a-z]{2}-[a-z0-9-]+-[1-9][0-9]*", doc["region"])):
        reject("DESCRIPTOR_IDENTITY")
    revision, services = load_catalog() if catalog is None else catalog
    if doc["catalog_sha256"] != revision:
        reject("CATALOG_VERSION")
    allowed = services.get(service, {})
    if not isinstance(doc["bundles"], list) or not 1 <= len(doc["bundles"]) <= 2:
        reject("BUNDLE_COUNT")
    seen, classes, sources = set(), set(), set()
    for bundle in doc["bundles"]:
        exact_fields(bundle, ["class", "secret_id", "version_number", "names", "required"])
        kind = bundle["class"]
        if not isinstance(kind, str) or kind not in {"secret", "parameter", "public"} or kind in classes:
            reject("BUNDLE_CLASS")
        if (service == "next-to-you-ui") != (kind == "public"):
            reject("PUBLIC_BOUNDARY")
        classes.add(kind)
        sid = bundle["secret_id"]
        if (not isinstance(sid, str) or not re.fullmatch(r"ocid1\.vaultsecret\.oc1\.[a-z0-9-]+\.[a-z0-9]+", sid)
                or sid in sources or type(bundle["version_number"]) is not int or bundle["version_number"] < 1):
            reject("PINNED_REFERENCE")
        sources.add(sid)
        names, required = bundle["names"], bundle["required"]
        if (not isinstance(names, list) or not names or not all(isinstance(x, str) for x in names)
                or len(set(names)) != len(names) or set(names) & seen
                or not isinstance(required, list) or not all(isinstance(x, str) for x in required)
                or len(set(required)) != len(required) or not set(required) <= set(names)):
            reject("NAME_SET")
        for name in names:
            if name not in allowed or allowed[name]["class"] != kind:
                reject("CATALOG_BOUNDARY")
        seen.update(names)
    return doc


def fetch_bundle(doc, bundle, profile, auth, runner=subprocess.run):
    # Provider stdout/stderr remain private to this process, never diagnostics.
    command = ["oci", "--profile", profile, "--auth", auth, "--region", doc["region"],
               "--output", "json", "--connection-timeout", "5", "--read-timeout", "10",
               "--max-retries", "0", "secrets", "secret-bundle", "get",
               "--secret-id", bundle["secret_id"], "--version-number", str(bundle["version_number"])]
    # Preserve the host's network transport, including managed egress proxies.
    # OCI endpoint/debug overrides and unrelated application values stay excluded.
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                              "http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    environment["OCI_CLI_RC_FILE"] = os.devnull
    try:
        response = runner(command, capture_output=True, timeout=25, env=environment)
    except (OSError, subprocess.SubprocessError):
        reject("OCI_UNAVAILABLE")
    if response.returncode:
        reject("OCI_READ_FAILED")
    response = decode_json(response.stdout, MAX_RESPONSE)
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        reject("OCI_RESPONSE")
    data = response["data"]
    if data.get("secret-id") != bundle["secret_id"] or data.get("version-number") != bundle["version_number"]:
        reject("OCI_VERSION")
    content = data.get("secret-bundle-content")
    if not isinstance(content, dict) or content.get("content-type") != "BASE64" or not isinstance(content.get("content"), str):
        reject("OCI_CONTENT")
    try:
        raw = base64.b64decode(content["content"], validate=True)
    except (ValueError, binascii.Error):
        reject("OCI_ENCODING")
    payload = decode_json(raw, MAX_BUNDLE)
    exact_fields(payload, ["environment", "service", "class", "values"])
    if any(payload[key] != expected for key, expected in
           [("environment", doc["environment"]), ("service", doc["service"]), ("class", bundle["class"])]):
        reject("PAYLOAD_IDENTITY")
    values = payload["values"]
    if (not isinstance(values, dict) or not set(bundle["required"]) <= set(values)
            or not set(values) <= set(bundle["names"])):
        reject("VALUE_SET")
    if any(not isinstance(value, str) or not value or "\x00" in value for value in values.values()):
        reject("VALUE_TYPE")
    return values


def private_directory(path):
    # Only dedicated operator-owned roots may hold generated configuration.
    path = Path(path)
    if path.is_symlink():
        reject("UNSAFE_PATH")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        reject("PRIVATE_DIRECTORY_REQUIRED")
    return path


def write_private(path, raw):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def release_lock(directory):
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(directory / ".lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            reject("UNSAFE_LOCK")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reject("SYNC_BUSY")
        yield
    finally:
        os.close(fd)


def sync(path, root, environment, service, profile, auth, runner=subprocess.run):
    catalog = load_catalog()
    doc = descriptor(path, environment, service, catalog)
    _, services = catalog
    entries = services[service]
    # No fallback to an old generation on fetch or validation failure.
    values = {}
    for bundle in doc["bundles"]:
        values.update(fetch_bundle(doc, bundle, profile, auth, runner))
    root = Path(root).absolute()
    if root.is_symlink():
        reject("UNSAFE_PATH")
    root = root.resolve()
    if root == REPO or REPO in root.parents or any((p / ".git").exists() for p in [root, *root.parents]):
        reject("OUTPUT_IN_REPOSITORY")
    private_directory(root)
    destination = private_directory(private_directory(root / environment) / service)
    with release_lock(destination):
        current = destination / "current"
        if current.exists() and not current.is_symlink():
            reject("UNSAFE_CURRENT")
        releases = private_directory(destination / "releases")
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
        release = releases / uuid.uuid4().hex
        pointer = destination / (".current-" + uuid.uuid4().hex)
        try:
            files = private_directory(stage / "files")
            for name, value in values.items():
                write_private(files / entries[name].get("target", name), value.encode("utf-8"))
            if service == "next-to-you-ui":
                public = {"cartoBasemapsApiKey": values.get("CARTO_BASEMAPS_API_KEY", "")}
                write_private(stage / "runtime-config.js", ("window.eventMapConfig = " + json.dumps(public) + ";\n").encode())
                # This file contains only allowlisted public browser configuration.
                os.chmod(stage / "runtime-config.js", 0o644)
            record = {"schema_version": 1, "environment": environment, "service": service,
                      "descriptor": doc, "descriptor_sha256": hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()}
            write_private(stage / "release-descriptor.json", (json.dumps(doc, sort_keys=True) + "\n").encode())
            write_private(stage / "release.json", (json.dumps(record, sort_keys=True) + "\n").encode())
            os.rename(stage, release)
            os.symlink("releases/" + release.name, pointer)
            os.replace(pointer, current)
        except BaseException:
            pointer.unlink(missing_ok=True)
            if stage.exists():
                shutil.rmtree(stage)
            raise
        return release


def compose(releases, environment, arguments, runner=subprocess.run):
    if len(releases) != 3:
        reject("THREE_RELEASES_REQUIRED")
    catalog = load_catalog()
    _, services = catalog
    directories = {}
    for path in releases:
        path = Path(path)
        if path.is_symlink():
            reject("PIN_GENERATION_PATH")
        path = path.resolve()
        info = path.stat()
        if not path.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            reject("PRIVATE_DIRECTORY_REQUIRED")
        record = load_json(path / "release.json")
        service = record.get("service")
        if (record.get("environment") != environment or service not in SERVICES or service in directories
                or record.get("schema_version") != 1 or not (path / "files").is_dir()
                or (path / "files").is_symlink()):
            reject("RELEASE_IDENTITY")
        doc = record.get("descriptor")
        if not isinstance(doc, dict) or doc.get("service") != service or doc.get("environment") != environment:
            reject("RELEASE_IDENTITY")
        digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
        if record.get("descriptor_sha256") != digest:
            reject("RELEASE_DESCRIPTOR")
        # Reuse the same allowlist/schema validation without reading secret files.
        if descriptor(path / "release-descriptor.json", environment, service, catalog) != doc:
            reject("RELEASE_DESCRIPTOR")
        entries = services[service]
        for bundle in doc["bundles"]:
            for name in bundle["required"]:
                value_file = path / "files" / entries[name].get("target", name)
                if (value_file.is_symlink() or not value_file.is_file() or not value_file.stat().st_size
                        or value_file.stat().st_uid != os.getuid() or stat.S_IMODE(value_file.stat().st_mode) != 0o600):
                    reject("INCOMPLETE_RELEASE")
        if service == "next-to-you-ui":
            public_file = path / "runtime-config.js"
            if public_file.is_symlink() or not public_file.is_file():
                reject("PUBLIC_CONFIG_MISSING")
        directories[service] = str(path)
    env = dict(os.environ)
    # These are host paths, not configuration values or OCI credentials.
    env.update({"OCI_AI_RELEASE": directories["event-ai"], "OCI_BE_RELEASE": directories["next-to-you"],
                "OCI_UI_RELEASE": directories["next-to-you-ui"]})
    command = [str(REPO / "bin/compose"), "-f", str(REPO / "compose.yaml"), "-f", str(REPO / "compose.oci.yaml")] + arguments
    try:
        result = runner(command, env=env, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        reject("COMPOSE_UNAVAILABLE")
    if result.returncode:
        reject("COMPOSE_FAILED")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("sync")
    get.add_argument("--deployment", required=True)
    get.add_argument("--root", required=True)
    get.add_argument("--environment", choices=["local", "dev"], required=True)
    get.add_argument("--service", choices=sorted(SERVICES), required=True)
    get.add_argument("--profile", required=True)
    get.add_argument("--auth", choices=["security_token", "api_key", "instance_principal"], default="security_token")
    launch = commands.add_parser("compose")
    launch.add_argument("--environment", choices=["local", "dev"], required=True)
    launch.add_argument("--release", action="append", required=True)
    launch.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            release = sync(args.deployment, args.root, args.environment, args.service, args.profile, args.auth)
            print(f"READY {args.environment} {args.service} generation={release}")
        else:
            command = args.args[1:] if args.args[:1] == ["--"] else args.args
            if not command:
                reject("COMPOSE_COMMAND_REQUIRED")
            compose(args.release, args.environment, command)
            print("READY compose")
        return 0
    except ConfigError as error:
        print("BLOCKED: " + str(error), file=sys.stderr)
    except Exception:
        # OS, encoding, provider and parser exception text can contain values.
        print("BLOCKED: CONFIG_OPERATION_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
