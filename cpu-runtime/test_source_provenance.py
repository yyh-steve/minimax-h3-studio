"""Small Git/metadata regression tests; no package installation, torch, or network."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import source_provenance as provenance


class FakeDistribution:
    version = "0.0.test"

    def __init__(self, installed, source):
        self.installed = installed
        self.source = source
        self.files = [Path("diffusers/__init__.py")]

    def locate_file(self, relative):
        return self.installed / relative

    def read_text(self, name):
        if name == "direct_url.json":
            return json.dumps({"url": self.source.as_uri(), "dir_info": {}})
        return None


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-provenance-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source_file = self.source / "src/diffusers/__init__.py"
        self.source_file.parent.mkdir(parents=True)
        self.source_file.write_text('VALUE = "original"\n')
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run([
            "git", "-c", "user.name=Local Test", "-c", "user.email=test@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-C", str(self.source), "commit", "--quiet", "-m", "fixture",
        ], check=True)
        self.sha = provenance.git(self.source, "rev-parse", "HEAD")
        self.installed = self.root / "installed"
        self.installed_file = self.installed / "diffusers/__init__.py"
        self.installed_file.parent.mkdir(parents=True)
        self.installed_file.write_bytes(self.source_file.read_bytes())
        self.dist = FakeDistribution(self.installed, self.source)
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def record(self):
        entry = provenance.record_distribution("diffusers", self.sha, self.source, self.dist)
        self.manifest.write_text(json.dumps({"schema": 1, "packages": {"diffusers": entry}}))
        return entry

    def verify(self):
        with patch.dict(provenance.PINS, {"diffusers": self.sha}):
            return provenance.verified_install("diffusers", self.dist, self.manifest)

    def test_record_and_verify_exact_installed_source(self):
        entry = self.record()
        result = self.verify()
        self.assertEqual(result["commit"], self.sha)
        self.assertEqual(result["commit_source"], "verified_source_install_manifest")
        self.assertEqual(result["verified_python_files"], 1)
        self.assertEqual(entry["tree"], provenance.git(self.source, "rev-parse", "HEAD^{tree}"))

    def test_installed_code_change_is_rejected(self):
        self.record()
        self.installed_file.write_text('VALUE = "changed"\n')
        with self.assertRaisesRegex(RuntimeError, "hash changed"):
            self.verify()

    def test_code_different_from_source_cannot_be_recorded(self):
        self.installed_file.write_text("DIFFERENT = True\n")
        with self.assertRaisesRegex(RuntimeError, "differs from verified source"):
            self.record()

    def test_dirty_tracked_source_is_rejected(self):
        self.source_file.write_text("CHANGED = True\n")
        with self.assertRaisesRegex(RuntimeError, "tracked source changed"):
            self.record()

    def test_wrong_commit_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "source HEAD"):
            provenance.record_distribution("diffusers", "0" * 40, self.source, self.dist)

    def test_manifest_version_change_is_rejected(self):
        self.record()
        self.dist.version = "other"
        with self.assertRaisesRegex(RuntimeError, "commit/version mismatch"):
            self.verify()

    def test_editable_install_is_rejected(self):
        self.dist.read_text = lambda name: json.dumps({"url": self.source.as_uri(), "dir_info": {"editable": True}})
        with self.assertRaisesRegex(RuntimeError, "Editable"):
            self.record()

    def test_missing_manifest_does_not_invent_commit(self):
        with self.assertRaises(FileNotFoundError):
            self.verify()


if __name__ == "__main__":
    unittest.main()
