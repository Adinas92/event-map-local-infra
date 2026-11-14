import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class OciSecretImportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "fixture"
        self.source.write_bytes(b"synthetic first line\nsynthetic second line\n")
        self.secrets = self.root / "secrets"

    def import_file(self, service, name):
        return subprocess.run(
            [str(REPO / "bin/secrets"), "set-file", service, name, str(self.source)],
            env={**os.environ, "EVENT_MAP_SECRETS_DIR": str(self.secrets)},
            capture_output=True,
        )

    def test_import_preserves_bytes_and_permissions_without_printing_value(self):
        for name in ("oci_config", "oci_api_key.pem"):
            with self.subTest(name=name):
                result = self.import_file("next-to-you", name)
                self.assertEqual(result.returncode, 0, result.stderr)
                target = self.secrets / "next-to-you" / name
                self.assertEqual(target.read_bytes(), self.source.read_bytes())
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertNotIn(b"synthetic", result.stdout + result.stderr)

    def test_import_rejects_empty_input_and_other_secrets(self):
        self.assertNotEqual(self.import_file("next-to-you", "APP_AUTH_TOKEN_SECRET").returncode, 0)
        self.assertNotEqual(self.import_file("event-ai", "oci_config").returncode, 0)
        self.source.write_bytes(b"")
        self.assertNotEqual(self.import_file("next-to-you", "oci_config").returncode, 0)
        self.assertFalse(self.secrets.exists())

    def test_import_rejects_directory_as_destination(self):
        target = self.secrets / "next-to-you" / "oci_config"
        target.mkdir(parents=True)
        result = self.import_file("next-to-you", "oci_config")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])
