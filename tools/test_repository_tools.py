"""Offline checks for publication guards and source restoration preconditions."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scanner = load("check_secrets")
restore = load("restore_source")


class PublicationGuards(unittest.TestCase):
    def test_recognizes_credentials_without_exempting_test_files(self):
        samples = [b"ghp_" + b"a" * 36,
                   b"eyJ" + b"a" * 12 + b"." + b"b" * 12 + b"." + b"c" * 12,
                   b"-----BEGIN " + b"RSA PRIVATE KEY-----",
                   b"https://someone:" + b"unknown-value@example.com/"]
        for value in samples:
            with self.subTest(value_type=len(value)):
                self.assertTrue(list(scanner.findings("overlay/tests/check.py", value)))

    def test_rejects_private_files_even_when_already_tracked(self):
        for name in ["private_profile_backup/profile.json", "state.json", ".env.local", "debug.log"]:
            self.assertIn("private/runtime file", list(scanner.findings(name, b"{}")))

    def test_limits_exemptions_to_reviewed_examples(self):
        self.assertFalse(list(scanner.findings("docs/example.txt", b"https://username:password@")))
        self.assertTrue(list(scanner.findings("docs/example.txt", b"https://username:" + b"different@")))
        self.assertTrue(list(scanner.findings("overlay/client.py", b'token="' + b"z" * 24 + b'"')))

    def test_scans_index_content_and_never_prints_credential(self):
        secret = b"ghp_" + b"z" * 36
        with patch.object(scanner, "git", side_effect=[b"100644 abc 0\tclient.py\0", secret]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(scanner.main(), 1)
        self.assertNotIn(secret.decode(), output.getvalue())
        self.assertIn("client.py", output.getvalue())


class RestoreGuards(unittest.TestCase):
    def test_existing_destination_is_preserved_without_git(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            destination = Path(temporary)
            marker = destination / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            with patch.object(sys, "argv", ["restore_source.py", str(destination)]), patch.object(restore, "run") as run:
                with self.assertRaisesRegex(SystemExit, "already exists"):
                    restore.main()
                run.assert_not_called()
            self.assertEqual(marker.read_text("utf-8"), "preserve")

    def test_corrupt_patch_is_rejected_before_destination_creation(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            (root / "patches").mkdir()
            (root / "patches/dev16.patch").write_bytes(b"changed")
            (root / "source-manifest.json").write_text(json.dumps({"patch_sha256": "0" * 64}), encoding="utf-8")
            destination = root / "new-source"
            with patch.object(restore, "ROOT", root), patch.object(sys, "argv", ["restore_source.py", str(destination)]), patch.object(restore, "run") as run:
                with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                    restore.main()
                run.assert_not_called()
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
