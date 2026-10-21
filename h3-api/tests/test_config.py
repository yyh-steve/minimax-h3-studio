"""Interpreter-path regression using a real dependency-free virtual environment."""
from contextlib import chdir
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
import venv

from h3_api.config import Settings


@unittest.skipUnless(os.name == "posix", "The target worker runs on POSIX")
class ModelInterpreterSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="h3-config-venv-")
        cls.addClassCleanup(cls.temporary.cleanup)
        # macOS exposes its temporary directory through a /var alias. Normalize
        # the fixture directory only; the interpreter itself must remain a symlink.
        cls.root = Path(cls.temporary.name).resolve()
        cls.model_env = cls.root / "model-venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(cls.model_env)
        cls.model_python = cls.model_env / "bin" / "python"
        if not cls.model_python.is_symlink():
            raise AssertionError("Regression fixture must use a real venv interpreter symlink")
        site = subprocess.run(
            [str(cls.model_python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        cls.module_name = "h3_venv_only_" + uuid.uuid4().hex
        cls.marker = "module-present-only-in-the-model-venv"
        (Path(site.stdout.strip()) / (cls.module_name + ".py")).write_text(
            "MARKER = " + repr(cls.marker) + "\n", encoding="utf-8"
        )

    def settings(self, entry, **overrides):
        values = {"data_dir": self.root / "data", "api_key": "unit-test-config-key-" + "x" * 24,
                  "model_python": entry}
        values.update(overrides)
        return Settings(**values)

    def assert_uses_model_venv(self, settings):
        script = (
            "import importlib,json,sys\n"
            "try:\n"
            " marker=importlib.import_module(sys.argv[1]).MARKER\n"
            "except ModuleNotFoundError:\n"
            " marker=None\n"
            "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'module_marker':marker}))\n"
        )
        child = subprocess.run(
            [str(settings.model_python), "-I", "-B", "-c", script, self.module_name],
            check=True, capture_output=True, text=True, timeout=10,
        )
        observed = json.loads(child.stdout)
        self.assertNotEqual(observed["prefix"], observed["base_prefix"], observed)
        self.assertEqual(Path(observed["prefix"]).resolve(), self.model_env.resolve(), observed)
        self.assertEqual(observed["module_marker"], self.marker, observed)
        self.assertEqual(settings.model_python, self.model_python.absolute())
        self.assertTrue(settings.model_python.is_symlink())

    def test_absolute_interpreter_entry_retains_venv_and_venv_only_module(self):
        self.assert_uses_model_venv(self.settings(self.model_python))

    def test_relative_interpreter_entry_becomes_absolute_without_losing_venv(self):
        with chdir(self.root):
            settings = self.settings(Path("model-venv/bin/python"))
        self.assertTrue(settings.model_python.is_absolute())
        self.assert_uses_model_venv(settings)

    def test_data_runner_and_model_directories_still_resolve_symlinks(self):
        aliases, targets = {}, {}
        for name in ("data_dir", "runner_dir", "model_dir"):
            target = self.root / (name + "-real")
            target.mkdir()
            alias = self.root / (name + "-link")
            alias.symlink_to(target, target_is_directory=True)
            aliases[name], targets[name] = alias, target
        settings = self.settings(self.model_python, **aliases)
        for name, target in targets.items():
            self.assertEqual(getattr(settings, name), target.resolve())


if __name__ == "__main__":
    unittest.main()
