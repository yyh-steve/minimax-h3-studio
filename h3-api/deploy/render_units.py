#!/usr/bin/env python3
"""Render reviewable Ubuntu systemd files; never install, start or enable services."""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys


CONFIG_PATH = "/etc/h3-api/h3-api.env"
SECRET_PATH = "/etc/h3-api/h3-api-secret.env"
SERVICE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z", re.ASCII)


def absolute_path(value, name):
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError(f"{name} must be an absolute POSIX path") from None
    if not isinstance(text, str) or not text or any(
            ord(char) < 32 or ord(char) == 127 or char in "\u0085\u2028\u2029" for char in text):
        raise ValueError(f"{name} must not be empty or contain control/newline characters")
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be absolute and contain no '..' components")
    # These are paths on the future Ubuntu host: do not resolve or inspect them
    # against the local machine's filesystem, home directory or credentials.
    return str(path)


def quoted(value):
    """A literal double-quoted item for systemd's C/shell-like string syntax."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def exec_path(path):
    # ':' is a systemd ExecStart prefix disabling $ expansion, not a shell.
    # '%' expansion is separate and needs %% even with that prefix.
    return quoted(":" + path.replace("%", "%%"))


def service_unit(python, service_user, *, worker):
    command = "worker" if worker else "api --host 127.0.0.1 --port 30010"
    description = "Local H3 serial generation worker" if worker else "Local H3 API on loopback only"
    stop_seconds = 240 if worker else 60
    return f"""# Generated configuration only; installation and start are separate manual actions.
[Unit]
Description={description}
After=network.target

[Service]
Type=simple
User={service_user}
# The installed API package and all runtime paths are absolute; cwd is not an import/data path.
WorkingDirectory=/
EnvironmentFile={CONFIG_PATH}
EnvironmentFile={SECRET_PATH}
UMask=0077
ExecStart={exec_path(python)} -m h3_api {command}
Restart=no
KillSignal=SIGTERM
KillMode=control-group
TimeoutStopSec={stop_seconds}s
SendSIGKILL=yes

# Intentionally no boot-install section and no dependency that starts the other service.
"""


def installation_instructions(paths, service_user):
    quote = shlex.quote
    prefix, output = PurePosixPath(paths["prefix"]), PurePosixPath(paths["output_dir"])
    api_python = str(prefix / ".venv-api/bin/python")
    return f"""MANUAL INSTALLATION NOTES — these instructions have NOT been executed

Scope
-----
Target: Ubuntu with systemd. The generator can run on a Mac and writes files only.
It does not install dependencies, create users, read credentials, start cloud
resources, request permissions, open network access or register boot startup.

Review the rendered files before executing any selected command below.
Use an administrator shell on the intended host, never on the preparation Mac.
Both services are manual-start and Restart=no. There is no automatic rerun of
interrupted work. Starting the worker can consume already queued tasks: inspect
the queue and confirm the intended job/time limits before manually starting it.

1. Check the existing environment
---------------------------------
The configured service user is {service_user!r}. The default root is provided for
an existing trial host, not as a universal production recommendation. Render with
--service-user to select an ALREADY EXISTING account; this generator does not
create it or change permissions on the model environment.

The service user must be able to traverse/read the API installation, runner,
model Python, weights and the source-provenance paths used by that Python. It
must be able to write the entire configured data directory, including an existing
SQLite database and existing task files. Review ownership before reusing data.
Systemd's system manager reads the root-owned private EnvironmentFile files.

Keep the existing model virtual environment and weights. Synchronize the current
runner helpers and both audited model manifests separately; the API package does
not include or download weights. The runner needs --duration-seconds and
--prompt-file. Ensure ffmpeg and ffprobe are installed on the service PATH.
WorkingDirectory=/ is intentional: API dependencies are installed in its own
venv and all data paths are absolute; the model backend sets its own runner cwd.
No ProtectHome/PrivateTmp isolation is added that could hide existing model paths.

2. Install the API into a separate virtual environment
------------------------------------------------------
Copy the h3-api package and requirements.lock into the configured prefix first.
These commands must not target the model Python environment:

python3.12 -m venv {quote(str(prefix / '.venv-api'))}
{quote(api_python)} -m pip install -r {quote(str(prefix / 'requirements.lock'))}
{quote(api_python)} -m pip install --no-deps {quote(str(prefix))}
{quote(api_python)} -m pip check

3. Review and install the non-secret configuration
--------------------------------------------------
The following copy changes a configuration file only. Back up any existing
configuration first. The .env syntax is for systemd, not for shell sourcing;
paths containing $ or backticks remain literal and must not be evaluated.

install -d -m 0755 /etc/h3-api
install -m 0600 {quote(str(output / 'h3-api.env.example'))} {CONFIG_PATH}
install -d -m 0700 -o {quote(service_user)} {quote(paths['data_dir'])}

4. Separately create the private API key file
--------------------------------------------
No key was generated or read by render_units.py. Both services require the same
private key file. To create a NEW key locally on the target host without printing
it, an administrator may deliberately run the following standard-library code.
O_EXCL refuses to replace an existing key. Do not regenerate keys on service
startup; changing a key invalidates existing signed download links.

python3.12 - <<'PY'
import os
import secrets
path = {SECRET_PATH!r}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as output:
    output.write('H3_API_KEY=' + secrets.token_urlsafe(32) + '\\n')
PY

The key must be ASCII, contain at least 24 characters, and contain no whitespace.
If a key already exists, preserve and provision it privately through the chosen
operations procedure. Never commit or print the secret file. This API is a shared
single identity, not a multi-tenant authorization system.

5. Install and verify the unit definitions, without starting them
----------------------------------------------------------------
Review/back up any existing units before copying. Nothing here registers boot
startup; neither generated unit has an [Install] section.

install -m 0644 {quote(str(output / 'h3-api.service'))} /etc/systemd/system/h3-api.service
install -m 0644 {quote(str(output / 'h3-worker.service'))} /etc/systemd/system/h3-worker.service
systemd-analyze verify /etc/systemd/system/h3-api.service /etc/systemd/system/h3-worker.service
systemctl daemon-reload

6. Optional manual operation, after checks and queue review
-----------------------------------------------------------
Only execute these separate commands when an actual run is intended:

systemctl start h3-api.service
systemctl start h3-worker.service
systemctl status h3-api.service h3-worker.service
journalctl -u h3-api.service -u h3-worker.service

To stop local services, deliberately stop the worker before the API:

systemctl stop h3-worker.service
systemctl stop h3-api.service

The API binds only 127.0.0.1:30010. No firewall, public proxy or public listener is
configured. /healthz does not check model readiness. Each state directory permits
one worker. Worker shutdown targets the entire control group and allows up to
240 seconds for teardown, exceeding the backend's TERM/kill grace and bounded
media checks; interrupted tasks are failed on recovery, never resumed from a
denoising checkpoint or automatically regenerated.

Job timeout, service stop and OS shutdown do NOT stop cloud billing. Cloud
resource shutdown/charging controls remain a separate operator responsibility.
Task retention and logical deletion do not clean up stored media or all historic
database rows; arrange backups and storage maintenance separately.
"""


def render_files(*, prefix, runner_dir, model_dir, model_python, data_dir, output_dir, service_user="root"):
    paths = {name: absolute_path(value, name) for name, value in {
        "prefix": prefix, "runner_dir": runner_dir, "model_dir": model_dir,
        "model_python": model_python, "data_dir": data_dir, "output_dir": output_dir,
    }.items()}
    if not isinstance(service_user, str) or not SERVICE_USER.fullmatch(service_user):
        raise ValueError("service_user must be a lowercase account name: [a-z_][a-z0-9_-]{0,31}")
    python = str(PurePosixPath(paths["prefix"]) / ".venv-api/bin/python")
    config = """# Non-secret systemd EnvironmentFile example; do not source this file in a shell.
# No API key is present. Separately provision /etc/h3-api/h3-api-secret.env.
# Keep installed configuration files private (root-owned mode 0600).
"""
    for variable, name in (("H3_DATA_DIR", "data_dir"), ("H3_RUNNER_DIR", "runner_dir"),
                           ("H3_MODEL_DIR", "model_dir"), ("H3_MODEL_PYTHON", "model_python")):
        config += variable + "=" + quoted(paths[name]) + "\n"
    config += """H3_PUBLIC_BASE_URL="http://127.0.0.1:30010"
# Review CPU affinity and the intended job budget before manually starting work.
H3_THREADS=80
H3_JOB_TIMEOUT=64800
H3_QUEUE_LIMIT=8
"""
    return {
        "h3-api.service": service_unit(python, service_user, worker=False),
        "h3-worker.service": service_unit(python, service_user, worker=True),
        "h3-api.env.example": config,
        "INSTALL.txt": installation_instructions(paths, service_user),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, help="Absolute API installation directory; its venv is .venv-api")
    parser.add_argument("--runner-dir", required=True, help="Absolute directory containing the installed runner helpers")
    parser.add_argument("--model-dir", required=True, help="Absolute existing model directory")
    parser.add_argument("--model-python", required=True, help="Absolute Python executable in the existing model venv")
    parser.add_argument("--data-dir", required=True, help="Absolute persistent API state directory")
    parser.add_argument("--service-user", default="root", help="Existing Ubuntu account; default root for the trial host")
    parser.add_argument("--output-dir", required=True, help="Absolute local output directory; existing output files are refused")
    args = parser.parse_args()
    files = render_files(**vars(args))
    output = Path(absolute_path(args.output_dir, "output_dir"))
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError("output_dir must be a directory, not a symlink or file")
    if any((output / name).exists() or (output / name).is_symlink() for name in files):
        raise FileExistsError("Refusing to overwrite an existing deployment output; choose a new output directory")
    previous_umask = os.umask(0o077)
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, content in files.items():
            with (output / name).open("x", encoding="utf-8", newline="\n") as destination:
                destination.write(content)
    finally:
        os.umask(previous_umask)
    print(f"Rendered {len(files)} files to {output}. No installation or service/cloud action was executed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
