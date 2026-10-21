"""Content and filesystem tests; no systemd, network, credentials or model needed."""
import importlib.util
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "render_units.py"
OUTPUTS = {"h3-api.service", "h3-worker.service", "h3-api.env.example", "INSTALL.txt"}


class DeployTests(unittest.TestCase):
    def renderer(self):
        self.assertTrue(SCRIPT.is_file(), "The deployment renderer must exist before deployment files can be generated")
        spec = importlib.util.spec_from_file_location("h3_deploy_renderer", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def settings(output_dir="/tmp/rendered-h3-units"):
        return dict(prefix="/opt/h3-api", runner_dir="/data/h3-cpu",
                    model_dir="/data/h3-cpu/model", model_python="/data/h3-cpu/.venv/bin/python",
                    data_dir="/var/lib/h3-api", output_dir=output_dir)

    def render(self, **overrides):
        values = self.settings()
        values.update(overrides)
        return self.renderer().render_files(**values)

    def cli(self, values, env=None):
        self.renderer()
        args = [sys.executable, str(SCRIPT)]
        for name, value in values.items():
            args.extend(["--" + name.replace("_", "-"), str(value)])
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=10)

    def test_units_are_loopback_only_and_never_activated_at_boot(self):
        files = self.render()
        self.assertEqual(set(files), OUTPUTS)
        api, worker = files["h3-api.service"], files["h3-worker.service"]
        self.assertIn('-m h3_api api --host 127.0.0.1 --port 30010', api)
        self.assertIn('-m h3_api worker', worker)
        self.assertNotIn('0.0.0.0', api)
        for unit in (api, worker):
            self.assertIn('User=root\n', unit)
            self.assertIn('WorkingDirectory=/\n', unit)
            self.assertIn('UMask=0077\n', unit)
            self.assertIn('Restart=no\n', unit)
            self.assertNotIn('[Install]', unit)
            self.assertNotIn('WantedBy=', unit)
            self.assertNotIn('Wants=h3-', unit)
            self.assertNotIn('Requires=h3-', unit)
            self.assertNotIn('ExecStartPre=', unit)
            self.assertNotIn('ProtectHome=', unit)
            self.assertNotIn('PrivateTmp=', unit)
        self.assertIn('KillMode=control-group\n', worker)
        stop = next(line.split('=', 1)[1] for line in worker.splitlines() if line.startswith('TimeoutStopSec='))
        self.assertGreaterEqual(int(stop.removesuffix('s')), 180)

    def test_api_venv_and_existing_model_environment_are_distinct(self):
        files = self.render()
        for name in ('h3-api.service', 'h3-worker.service'):
            self.assertIn('"\u003a/opt/h3-api/.venv-api/bin/python"', files[name])
            self.assertNotIn('/data/h3-cpu/.venv/bin/python', files[name])
            self.assertIn('EnvironmentFile=/etc/h3-api/h3-api.env\n', files[name])
            self.assertIn('EnvironmentFile=/etc/h3-api/h3-api-secret.env\n', files[name])
        config = files['h3-api.env.example']
        self.assertIn('H3_MODEL_PYTHON="/data/h3-cpu/.venv/bin/python"', config)
        self.assertIn('H3_MODEL_DIR="/data/h3-cpu/model"', config)
        self.assertIn('H3_PUBLIC_BASE_URL="http://127.0.0.1:30010"', config)
        self.assertFalse(any(line.startswith('H3_API_KEY=') for line in config.splitlines()))

    def test_space_quotes_backslashes_and_expansion_markers_stay_literal(self):
        prefix = '/opt/H3 space/\"quoted\"/cash$HOME%N/back\\slash'
        model = "/data/model's $HOME%N/weights"
        files = self.render(prefix=prefix, model_dir=model, service_user='h3_worker-1')
        self.assertIn('User=h3_worker-1\n', files['h3-api.service'])
        line = next(line for line in files['h3-api.service'].splitlines() if line.startswith('ExecStart='))
        argv = shlex.split(line.split('=', 1)[1])
        # systemd's ':' command prefix disables $ expansion; doubled % means a literal %.
        self.assertEqual(argv[0], ':' + prefix.replace('%', '%%') + '/.venv-api/bin/python')
        self.assertEqual(argv[1:], ['-m', 'h3_api', 'api', '--host', '127.0.0.1', '--port', '30010'])
        env_line = next(line for line in files['h3-api.env.example'].splitlines() if line.startswith('H3_MODEL_DIR='))
        self.assertEqual(shlex.split(env_line.split('=', 1)[1]), [model])
        self.assertIn(shlex.quote(prefix + '/requirements.lock'), files['INSTALL.txt'])
        self.assertIn(shlex.quote(prefix + '/.venv-api/bin/python'), files['INSTALL.txt'])

    def test_all_paths_reject_relative_traversal_and_line_injection(self):
        for field in self.settings():
            for bad in ('relative/path', '', '/srv/../other', '/srv/x\nExecStart=/bin/false',
                        '/srv/x\rUser=other', '/srv/x\tbad', '/srv/x\x00bad', '/srv/x\u2028bad'):
                with self.subTest(field=field, bad=repr(bad)), self.assertRaises(ValueError):
                    self.render(**{field: bad})

    def test_service_user_rejects_injection_and_non_account_names(self):
        for user in ('', 'root\nExecStart=/bin/false', 'root;id', 'root group', '../root', '-root', '123', 'x' * 33):
            with self.subTest(user=repr(user)), self.assertRaises(ValueError):
                self.render(service_user=user)

    def test_instructions_are_reviewable_and_do_not_install_into_model_venv(self):
        guide = self.render()['INSTALL.txt']
        self.assertIn('requirements.lock', guide)
        self.assertIn('-m pip install --no-deps', guide)
        self.assertNotIn('/data/h3-cpu/.venv/bin/python -m pip', guide)
        self.assertNotIn('systemctl enable', guide)
        self.assertNotIn('--now', guide)
        self.assertIn('O_EXCL', guide)
        self.assertIn('no automatic rerun', guide)
        self.assertIn('cloud billing', guide)
        self.assertIn('service user', guide)

    def test_cli_only_writes_private_outputs_and_does_not_invoke_deployment_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_bin = root / 'fake-bin'
            fake_bin.mkdir()
            marker = root / 'unexpected-command'
            for name in ('systemctl', 'systemd-analyze', 'pip', 'ssh', 'curl', 'useradd'):
                executable = fake_bin / name
                executable.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker)) + '\n')
                executable.chmod(0o755)
            output = root / 'rendered'
            fixture_key = 'synthetic-key-that-must-not-be-read-0001'
            env = {'PATH': str(fake_bin), 'PYTHONIOENCODING': 'utf-8', 'H3_API_KEY': fixture_key}
            run = self.cli(self.settings(str(output)), env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertFalse(marker.exists(), 'Rendering must not run installation/start commands')
            self.assertEqual({path.name for path in output.iterdir()}, OUTPUTS)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for path in output.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertNotIn(fixture_key, path.read_text())
            self.assertNotIn(fixture_key, run.stdout + run.stderr)

    def test_existing_output_is_not_overwritten_or_partially_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'rendered'
            output.mkdir()
            original = output / 'h3-api.env.example'
            original.write_text('keep the existing configuration\n')
            run = self.cli(self.settings(str(output)))
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(original.read_text(), 'keep the existing configuration\n')
            self.assertEqual({path.name for path in output.iterdir()}, {'h3-api.env.example'})

    def test_invalid_cli_arguments_fail_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'rendered'
            values = self.settings(str(output))
            values['prefix'] = '/opt/api\nUser=other'
            run = self.cli(values)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
