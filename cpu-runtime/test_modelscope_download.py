"""Offline regression tests for audited base/ref selection and completion records."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import download_from_modelscope as downloader


HERE = Path(__file__).resolve().parent
BASE = HERE / "modelscope-h3-manifest.json"
REF = HERE / "modelscope-h3-ref-manifest.json"


def completed(path):
    data = json.loads(path.read_text())
    data.update(all_files_sha256_verified=True, download_seconds=1.0)
    return data


class ManifestTests(unittest.TestCase):
    def merge(self, old, new):
        self.assertTrue(callable(getattr(downloader, "merge_completed_manifest", None)),
                        "Downloader must preserve installed selections when appending ref")
        return downloader.merge_completed_manifest(old, new)

    def validate(self, data, selection=None):
        self.assertTrue(callable(getattr(downloader, "validate_completed_manifest", None)),
                        "Completed manifests need independent audited-selection validation")
        return downloader.validate_completed_manifest(data, required_selection=selection)

    def test_pinned_ref_selection_contains_only_16_supplement_files(self):
        try:
            ref = downloader.load_manifest(REF)
        except RuntimeError as error:
            self.fail(f"The audited Ref2VA supplement must be accepted: {error}")
        self.assertEqual(len(ref["files"]), 16)
        self.assertEqual(ref["total_bytes"], 66280569250)
        self.assertTrue(all(x["path"].startswith("transformer_ref/") for x in ref["files"]))
        self.assertEqual(len(downloader.load_manifest(BASE)["files"]), 63)

    def test_modified_manifest_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / "changed.json"
            for original in (BASE, REF):
                changed.write_bytes(original.read_bytes() + b" ")
                with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                    downloader.load_manifest(changed)

    def test_base_completion_still_works_without_ref_companion_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / BASE.name).write_bytes(BASE.read_bytes())
            with mock.patch.object(downloader, "__file__", str(directory / "download_from_modelscope.py")):
                try:
                    selections = self.validate(completed(BASE), "base")
                except FileNotFoundError as error:
                    self.fail(f"Base-only deployments must not require the ref companion: {error}")
            self.assertEqual(selections, {"base"})

    def test_append_retains_all_base_entries_and_is_idempotent(self):
        base, ref = completed(BASE), completed(REF)
        before = copy.deepcopy(base)
        merged = self.merge(base, ref)
        self.assertEqual(base, before, "Merging must not mutate the supplied base record")
        self.assertEqual(len(merged["files"]), 79)
        self.assertEqual(merged["total_bytes"], base["total_bytes"] + ref["total_bytes"])
        indexed = {x["path"]: x for x in merged["files"]}
        for item in base["files"]:
            self.assertEqual(indexed[item["path"]], item)
        repeated = self.merge(merged, ref)
        self.assertEqual(repeated["files"], merged["files"])
        self.assertEqual(repeated["total_bytes"], merged["total_bytes"])
        self.assertEqual(self.validate(repeated, "ref"), {"base", "ref"})

    def test_base_rerun_preserves_previously_completed_ref(self):
        merged = self.merge(completed(BASE), completed(REF))
        rerun = self.merge(merged, completed(BASE))
        self.assertEqual(rerun["files"], merged["files"])
        self.assertEqual(self.validate(rerun, "ref"), {"base", "ref"})

    def test_ref_supplement_requires_a_complete_base_record(self):
        with self.assertRaisesRegex(RuntimeError, "base"):
            self.merge(None, completed(REF))
        incomplete = completed(BASE)
        incomplete["files"].pop()
        incomplete["total_bytes"] = sum(x["size"] for x in incomplete["files"])
        with self.assertRaises(RuntimeError):
            self.merge(incomplete, completed(REF))

    def test_ref_validation_does_not_accept_base_only_or_missing_ref_shard(self):
        with self.assertRaisesRegex(RuntimeError, "ref"):
            self.validate(completed(BASE), "ref")
        partial = completed(REF)
        partial["files"].pop()
        partial["total_bytes"] = sum(x["size"] for x in partial["files"])
        with self.assertRaises(RuntimeError):
            self.validate(partial, "ref")

    def test_rejects_tampered_installed_records_before_merge(self):
        mutations = {
            "traversal": lambda data: data["files"][0].update(path="../escaped.json"),
            "noncanonical_path": lambda data: data["files"][0].update(path="./LICENSE"),
            "duplicate": lambda data: data["files"].append(copy.deepcopy(data["files"][0])),
            "wrong_hash": lambda data: data["files"][0].update(sha256="0" * 64),
            "wrong_size": lambda data: data["files"][0].update(size=1),
            "boolean_size": lambda data: data["files"][0].update(size=True),
            "wrong_revision": lambda data: data.update(revision="main"),
            "wrong_source": lambda data: data.update(source_model="other/MiniMax-H3"),
            "unverified": lambda data: data.update(all_files_sha256_verified=False),
            "wrong_total": lambda data: data.update(total_bytes=1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                data = completed(BASE)
                mutate(data)
                with self.assertRaises(RuntimeError):
                    self.merge(data, completed(REF))

    def test_ref_execution_visits_only_ref_files_and_persists_merged_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / ".h3-cpu-manifest.json"
            output.write_text(json.dumps(completed(BASE)))
            visited = []
            argv = ["download_from_modelscope.py", "--manifest", str(REF),
                    "--model-dir", str(directory), "--execute-download"]
            with mock.patch("sys.argv", argv), \
                    mock.patch.object(downloader.platform, "system", return_value="Linux"), \
                    mock.patch.object(downloader.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(downloader.shutil, "disk_usage",
                                      return_value=mock.Mock(free=500 * downloader.GIB)), \
                    mock.patch.object(downloader, "download_one",
                                      side_effect=lambda item, *args: visited.append(item["path"])), \
                    redirect_stdout(io.StringIO()):
                try:
                    downloader.main()
                except RuntimeError as error:
                    self.fail(f"Supplement execution must accept the audited manifest: {error}")
            self.assertEqual(set(visited), {x["path"] for x in completed(REF)["files"]})
            self.assertEqual(len(visited), 16)
            self.assertEqual(len(json.loads(output.read_text())["files"]), 79)


if __name__ == "__main__":
    unittest.main()
