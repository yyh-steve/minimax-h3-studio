#!/usr/bin/env bash
# Repair GitHub HTTP/2 transport failures without changing either pinned commit.
# No arguments: show plan only. This script never downloads model weights.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_root=/data/h3-cpu/src
python_bin=python
execute=0
diffusers_sha=b800f828f3951c4d206e154411bcbfa693cd322e
transformers_sha=d4804f9d76b8c071b993d813e9e87067b780cbee

usage() {
  cat <<'TEXT'
Usage: bash bootstrap-sources.sh [--execute] [--python PATH] [--source-root PATH]

Default: print the plan; no downloads or installation.
--execute: Linux x86_64 only. Fetch the two pinned source trees over HTTP/1.1,
           install directly from directories, then audit Git HEAD and installed code hashes.
--python: Python from the already-prepared CPU virtual environment.
--source-root: source cache (default /data/h3-cpu/src).

Uses the existing pip index configuration. No model downloads, cloud operations,
global Git configuration changes, or modification of h3_cpu_trial.py.
TEXT
}

while (($#)); do
  case "$1" in
    --execute) execute=1; shift ;;
    --python|--source-root)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      if [[ "$1" == --python ]]; then python_bin=$2; else source_root=$2; fi
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

printf 'Source root: %s\nPython: %s\n' "$source_root" "$python_bin"
printf 'diffusers: %s\ntransformers: %s\n' "$diffusers_sha" "$transformers_sha"
if [[ $execute != 1 ]]; then
  usage
  exit 0
fi

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
  printf 'Refusing execution outside Linux x86_64, including the local Mac.\n' >&2
  exit 1
}
command -v git >/dev/null
command -v "$python_bin" >/dev/null
# Metadata only: do not import torch or run CPU computation here.
"$python_bin" - <<'PY'
import importlib.metadata as m
import sys
if sys.prefix == sys.base_prefix:
    raise SystemExit('Activate the CPU virtual environment, or pass its Python with --python.')
for name in ('torch', 'torchvision'):
    version = m.version(name)
    if not version.endswith('+cpu'):
        raise SystemExit(f'{name} is {version}, expected the already-verified +cpu wheel.')
    print(f'Keeping {name}=={version}')
PY

mkdir -p -- "$source_root"
source_root=$(cd -- "$source_root" && pwd)
export GIT_TERMINAL_PROMPT=0

fetch_source() {
  local name=$1 sha=$2 repo_dir="$source_root/$1"
  local url="https://github.com/huggingface/$1.git" attempt fetched=0
  if [[ ! -d "$repo_dir/.git" ]]; then
    # Refuse to initialize inside an occupied non-repository directory.
    "$python_bin" - "$repo_dir" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
if p.exists() and (not p.is_dir() or any(p.iterdir())):
    raise SystemExit(f'Refusing occupied non-Git directory: {p}')
PY
    git init "$repo_dir"
    git -C "$repo_dir" remote add origin "$url"
  else
    [[ $(git -C "$repo_dir" remote get-url origin) == "$url" ]] || {
      printf 'Unexpected origin in %s; refusing to replace it.\n' "$repo_dir" >&2
      exit 1
    }
    [[ -z $(git -C "$repo_dir" status --porcelain) ]] || {
      printf 'Working tree is not clean: %s; refusing to overwrite changes.\n' "$repo_dir" >&2
      exit 1
    }
  fi
  if ! git -C "$repo_dir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
    for attempt in 1 2 3; do
      printf 'Fetching %s at fixed SHA using HTTP/1.1 (attempt %s/3).\n' "$name" "$attempt"
      if git -c http.version=HTTP/1.1 -C "$repo_dir" fetch --depth=1 --no-tags origin "$sha"; then
        fetched=1
        break
      fi
      if [[ $attempt != 3 ]]; then sleep 3; fi
    done
    [[ $fetched == 1 ]] || {
      printf 'Source fetch still failed; no fallback to another version.\n' >&2
      exit 1
    }
  fi
  git -C "$repo_dir" checkout --detach "$sha"
  [[ $(git -C "$repo_dir" rev-parse HEAD) == "$sha" ]] || exit 1
}

fetch_source diffusers "$diffusers_sha"
fetch_source transformers "$transformers_sha"

# Keep all other requirements unchanged, but remove GitHub from pip's VCS path.
# Plain file URLs bypass pip's Git --filter=blob:none clone, which can loop on shallow sources.
"$python_bin" - "$script_dir/requirements-cpu.txt" "$source_root" \
  "$diffusers_sha" "$transformers_sha" <<'PY'
from pathlib import Path
import importlib.metadata as m
import sys
requirements, root = Path(sys.argv[1]), Path(sys.argv[2])
text = requirements.read_text()
for name, sha in zip(('diffusers', 'transformers'), sys.argv[3:]):
    remote = f'{name} @ git+https://github.com/huggingface/{name}.git@{sha}'
    local = f'{name} @ {(root / name).as_uri()}'
    if text.count(remote) != 1:
        raise SystemExit(f'Pin mismatch in requirements-cpu.txt: {name}')
    text = text.replace(remote, local)
(root / 'requirements-sources-local.txt').write_text(text)
(root / 'constraints-cpu.txt').write_text(''.join(
    f'{name}=={m.version(name)}\n' for name in ('torch', 'torchvision')
))
PY

"$python_bin" -m pip install \
  -c "$script_dir/constraints-cpu-core.txt" \
  -c "$source_root/constraints-cpu.txt" \
  -r "$source_root/requirements-sources-local.txt"
"$python_bin" -m pip check
"$python_bin" "$script_dir/source_provenance.py" record --source-root "$source_root"
"$python_bin" -m pip freeze > "$source_root/dependency-freeze.txt"
printf 'Source installation verified. Next run the existing dependency check, not model download.\n'
printf '%s h3_cpu_trial.py check --threads 32 --report /data/h3-cpu/cpu-check.json\n' "$python_bin"
