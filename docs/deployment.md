# 从零部署 H3 CPU API

在 Linux x86_64 CPU 服务器上安装 H3 模型运行时和异步 API。runner 接收一张参考图与提示词，执行单图 Ref2VA 音视频生成；FastAPI 接收请求，SQLite 保存任务，独立 worker 串行执行推理，提供提交任务、查询状态和下载视频的 MiniMax V2 风格接口。

模型使用固定版本的原始权重，Transformer 与条件编码器主要采用 BF16，保留框架要求的 FP32 子模块，视频和音频 VAE 使用 FP32。API 与模型分别安装在独立 Python 环境中。

命令以 Linux x86_64、Ubuntu 24.04、Python 3.12 为例，依次完成主机准备、CPU 模型环境、权重下载、推理入口、独立 API 环境和 systemd 服务；其他发行版需先准备对应的 Python、系统工具和服务管理环境。

参考实测主机为 **160 vCPU / 768 GiB**：生成 **5 秒、768×768、120 帧、24 fps、32 kHz 双声道、49 次实际采样**的视频，单次推理约 110.9 分钟。运行前的可用内存检查、磁盘要求和安装步骤如下。

本文中的安装、下载和启动命令由部署者在目标主机逐步执行。仓库不包含模型权重、虚拟环境或完整离线依赖包；安装阶段需要访问软件源，源码引导需要访问 GitHub，权重下载需要访问 ModelScope。推理阶段使用已准备的本地文件。

## 1. 确认主机和空间

| 项目 | 要求 |
|---|---|
| 系统 | Linux x86_64；本文使用 Ubuntu 的 systemd |
| Python | Python 3.12，模型与 API 各用一个虚拟环境 |
| 内存 | runner 默认要求运行前至少 **256 GiB 当前可用内存**；这是工程保护值，不是官方最低配置或主机总内存要求 |
| 权重空间 | base 63 个文件加 ref 16 个文件，共 **210,331,768,271 bytes，约 195.887 GiB** |
| 额外空间 | 还需两个虚拟环境、源码、安装缓存、下载临时文件、输入图片、日志和生成视频；建议数据盘起步至少有 **300 GiB 可用空间**，并按保留量增加 |
| 系统工具 | `git`、`ffmpeg`、`ffprobe`、系统 CA 证书；部署服务时需要 systemd 管理权限 |

下载器每次还会检查“当前缺少的文件大小 + 10 GiB”空间。这个检查只覆盖该轮下载，不代表整个服务的数据盘容量已足够。长时间运行时，任务文件会继续占用空间。

在目标 Ubuntu 主机准备系统工具：

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git ffmpeg ca-certificates
uname -sm
python3.12 --version
ffmpeg -version
ffprobe -version
```

将本仓库源码放到一个固定目录。下文所有路径都是可替换的示例；请在同一个 Bash 会话设置这些变量，后续命令复用它们。所选用户须能读写对应安装目录；`sudo` 步骤在目标主机执行。

```bash
export H3_REPO_DIR='/opt/minimax-h3-studio'
export H3_WORK_DIR='/data/h3-cpu'
export H3_RUNNER_DIR="$H3_REPO_DIR/cpu-runtime"
export H3_MODEL_DIR="$H3_WORK_DIR/model"
export H3_MODEL_PYTHON="$H3_WORK_DIR/.venv/bin/python"
export H3_SOURCE_DIR="$H3_WORK_DIR/src"
export H3_API_PREFIX="$H3_REPO_DIR/h3-api"
export H3_DATA_DIR='/var/lib/h3-api'
export H3_RENDER_DIR="$H3_WORK_DIR/systemd-review"
export H3_SERVICE_USER='root'

test -f "$H3_RUNNER_DIR/h3_cpu_trial.py"
test -f "$H3_API_PREFIX/pyproject.toml"
mkdir -p "$H3_WORK_DIR"
df -h "$H3_WORK_DIR"
```

`H3_SERVICE_USER` 可以换成已存在的普通服务账号。该账号需要对 API、runner、模型 Python、权重和来源记录具有读/执行权限，对整个 `H3_DATA_DIR` 具有写权限。生成器不会创建账号或改变模型目录的权限。

模型使用需遵守 [MiniMax-H3 固定版本模型许可](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE)。本仓库服务代码的许可不会替代模型及其他上游组件的许可。

## 2. 建立独立 CPU 模型环境

新建虚拟环境，保留其 Python 入口路径；不要把虚拟环境里的 Python 符号链接替换成系统解释器路径。

```bash
python3.12 -m venv "$H3_WORK_DIR/.venv"
"$H3_MODEL_PYTHON" -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.14.1+cpu' 'torchvision==0.29.1+cpu'
```

这是固定的 CPU 构建组合。如果官方索引没有适用于当前平台的这些版本，或依赖解析发生冲突，应先解决版本来源问题；升级依赖时同步更新版本清单，并执行下文的环境检查。

核心约束文件为 [`cpu-runtime/constraints-cpu-core.txt`](../cpu-runtime/constraints-cpu-core.txt)：

| 包 | 核心固定版本 |
|---|---|
| torch | `2.14.1+cpu` |
| torchvision | `0.29.1+cpu` |
| accelerate | `1.15.0` |
| huggingface-hub | `1.33.0` |
| safetensors | `0.8.0` |
| av | `17.1.0` |
| Pillow | `12.3.0` |
| NumPy | `2.5.3` |

**核心 constraints 不是全部传递依赖的锁文件。** 其余模型依赖、构建工具和系统库仍取决于安装环境；引导完成后保存实际依赖清单，迁移或升级时重新检查。API 的运行时依赖由另一份 `h3-api/requirements.lock` 固定，不应装进模型环境。

## 3. 安装固定源码并验证来源

引导脚本使用以下固定提交，应用核心约束，从源码目录安装并生成本机来源记录：

| 组件 | 固定提交 |
|---|---|
| Diffusers | `b800f828f3951c4d206e154411bcbfa693cd322e` |
| Transformers | `d4804f9d76b8c071b993d813e9e87067b780cbee` |

先查看计划，再执行安装：

```bash
bash "$H3_RUNNER_DIR/bootstrap-sources.sh" \
  --python "$H3_MODEL_PYTHON" --source-root "$H3_SOURCE_DIR"

bash "$H3_RUNNER_DIR/bootstrap-sources.sh" --execute \
  --python "$H3_MODEL_PYTHON" --source-root "$H3_SOURCE_DIR"

"$H3_MODEL_PYTHON" -m pip check
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/source_provenance.py" verify
```

第一条只打印计划。带 `--execute` 的命令获取固定源码、安装依赖，核对真实 Git HEAD/tree 与安装后的 Python 文件哈希，并在模型虚拟环境内写入 `h3-source-install.json`；实际依赖清单写入 `H3_SOURCE_DIR/dependency-freeze.txt`。该记录由本机安装过程生成，不应直接复用其他机器的记录。

保留 `H3_SOURCE_DIR` 中的源码和审计材料。遇到已有目录冲突、源码变更或校验失败时，先查明原因；不要修改版本元数据来跳过来源检查。后续更新依赖，需要重新安装和记录来源。

进行不加载权重的小算子检查：

```bash
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/h3_cpu_trial.py" inspect \
  --report "$H3_WORK_DIR/host-inspection.json"
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/h3_cpu_trial.py" check \
  --threads 2 --report "$H3_WORK_DIR/cpu-check.json"

"$H3_MODEL_PYTHON" - "$H3_WORK_DIR/cpu-check.json" <<'PY'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
checks = report['small_operator_checks']
if not all(checks[name]['ok'] for name in ('float32', 'bfloat16')):
    raise SystemExit('CPU FP32/BF16 checks did not both pass; resolve this before downloading weights.')
if report.get('available_memory_gib', 0) < 256:
    raise SystemExit('Current available memory is below the default 256 GiB runner guard.')
print('CPU import/operator checks passed.')
PY
```

此处检查库导入、FP32/BF16 运算和环境信息。API 固定使用 BF16 模型组件及 FP32 双 VAE，因此脚本会逐项确认两种精度的检查结果。

## 4. 按原始清单下载 base，再补 ref

本项目 Ref2VA runner 要求两份下载审计均完整：

| 清单 | 文件数 | 大小 | 内容 |
|---|---:|---:|---|
| `modelscope-h3-manifest.json` | 63 | 144,051,199,021 bytes | base 选择及共享组件 |
| `modelscope-h3-ref-manifest.json` | 16 | 66,280,569,250 bytes | `transformer_ref` 补充 |

固定 Hugging Face 模型 revision 为 `42ed227ee7df40d41602854ae760620d6eb651fe`，对应审计的 ModelScope revision 为 `57559a6720ddaa2eaee52b09ec2ef011127997b2`。

**两个 JSON 的字节必须保持原样，包括空白与换行。** 下载器校验清单自身的 SHA256；不要格式化 JSON、修改下载地址、删文件项或重算清单指纹以绕过校验。它们和 `download_from_modelscope.py` 必须位于同一 runner 目录。

先读取本地计划，不发出网络请求：

```bash
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/download_from_modelscope.py" \
  --manifest "$H3_RUNNER_DIR/modelscope-h3-manifest.json" \
  --model-dir "$H3_MODEL_DIR"
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/download_from_modelscope.py" \
  --manifest "$H3_RUNNER_DIR/modelscope-h3-ref-manifest.json" \
  --model-dir "$H3_MODEL_DIR"
```

核对计划后，**先完整执行 base，成功后再执行 ref**。下面每轮下载和哈希检查的时间上限为 14,400 秒，可根据网络和磁盘速度调整；这不是预计完成时间。

```bash
"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/download_from_modelscope.py" \
  --manifest "$H3_RUNNER_DIR/modelscope-h3-manifest.json" \
  --model-dir "$H3_MODEL_DIR" --workers 4 --max-seconds 14400 \
  --execute-download

"$H3_MODEL_PYTHON" "$H3_RUNNER_DIR/download_from_modelscope.py" \
  --manifest "$H3_RUNNER_DIR/modelscope-h3-ref-manifest.json" \
  --model-dir "$H3_MODEL_DIR" --workers 4 --max-seconds 14400 \
  --execute-download
```

下载器逐文件校验大小和 SHA256，完成后在模型目录合并写入 `.h3-cpu-manifest.json`。最终应有 **79 个已记录文件**、`base` 和 `ref` 两种 selection。ref 不能在缺失完整 base 记录时独立补齐。超时会保留下载中间文件；处理错误后用原清单重试，不手工伪造完成记录。

核对完成记录：

```bash
"$H3_MODEL_PYTHON" - "$H3_RUNNER_DIR" "$H3_MODEL_DIR" <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from download_from_modelscope import validate_completed_manifest
record = json.loads((Path(sys.argv[2]) / '.h3-cpu-manifest.json').read_text())
for selection in ('base', 'ref'):
    validate_completed_manifest(record, required_selection=selection)
if len(record['files']) != 79:
    raise SystemExit('The completed base/ref record does not contain 79 files.')
print('Complete base/ref audit records found. Keep the downloaded files unchanged.')
PY
```

此命令检查完成记录及其与原始审计清单的一致性，不重新扫描约 196 GiB 文件内容。下载时已完成逐文件哈希，推理启动时还会检查本地文件存在与大小；后续若修改或替换权重，需要重新验证。

## 5. 安装独立 API 环境

API 服务不导入 torch，worker 通过 `H3_MODEL_PYTHON` 启动上述模型 runner。两个 Python 环境必须分开：

```bash
python3.12 -m venv "$H3_API_PREFIX/.venv-api"
"$H3_API_PREFIX/.venv-api/bin/python" -m pip install \
  -r "$H3_API_PREFIX/requirements.lock"
"$H3_API_PREFIX/.venv-api/bin/python" -m pip install --no-deps "$H3_API_PREFIX"
"$H3_API_PREFIX/.venv-api/bin/python" -m pip check
```

API 锁文件固定运行时包版本。安装本项目时仍可能需要获取构建工具；这些步骤不是离线安装流程。开发测试另用 `requirements-dev.lock`，部署服务不需要安装测试依赖。

保持 `cpu-runtime/` 完整：runner 依赖 `exact_duration.py`、`ref2va_inputs.py`、`ref_audio_resample.py`、`source_provenance.py`、`download_from_modelscope.py` 和两个原始审计 JSON。只复制 `h3_cpu_trial.py` 或只安装 API wheel 无法构成完整模型部署。

## 6. 生成并审阅 systemd 配置

文件生成器的参数和行为见 [部署模板说明](../h3-api/deploy/README.md)。运行前可查看帮助；生成器本身不会安装、启动服务或创建云资源。

```bash
python3.12 "$H3_API_PREFIX/deploy/render_units.py" --help
python3.12 "$H3_API_PREFIX/deploy/render_units.py" \
  --prefix "$H3_API_PREFIX" \
  --runner-dir "$H3_RUNNER_DIR" \
  --model-dir "$H3_MODEL_DIR" \
  --model-python "$H3_MODEL_PYTHON" \
  --data-dir "$H3_DATA_DIR" \
  --service-user "$H3_SERVICE_USER" \
  --output-dir "$H3_RENDER_DIR"
```

输出目录应为新位置；已有同名输出文件时生成器拒绝覆盖。阅读生成的 `INSTALL.txt`、两个 `.service` 和 `h3-api.env.example`，确认路径指向目标主机的真实位置。

安装前在 `h3-api.env.example` 中核对配置：

| 配置 | 模板默认值及含义 |
|---|---|
| `H3_THREADS` | `80`；应按实际 CPU affinity 调整，不能超过可用逻辑 CPU 数 |
| `H3_JOB_TIMEOUT` | `64800` 秒，即单个模型子进程最多 18 小时；不是预期生成时间 |
| `H3_QUEUE_LIMIT` | `8`；queued 与 running 任务合计上限 |
| `H3_PUBLIC_BASE_URL` | `http://127.0.0.1:30010`；影响签名下载链接，不改变监听地址 |
| 数据与模型路径 | 两个服务共用同一数据目录，模型 Python 保持虚拟环境入口路径 |

API 默认只监听本机回环地址。需要外部访问时，另行配置 TLS 反向代理和访问控制，并将 `H3_PUBLIC_BASE_URL` 改为客户端可访问的 origin；本文不开放公网端口。

## 7. 安装配置、创建密钥、验证服务

以下在目标主机执行，需要管理员权限。若已有同名部署，先备份并核对配置；`install` 会替换所指的配置和 unit 文件。

```bash
sudo install -d -m 0755 /etc/h3-api
sudo install -m 0600 "$H3_RENDER_DIR/h3-api.env.example" /etc/h3-api/h3-api.env
sudo install -d -m 0700 -o "$H3_SERVICE_USER" "$H3_DATA_DIR"
```

首次部署时创建私有共享密钥，不打印其值；文件已存在时拒绝覆盖。复用既有部署时保留原密钥，不要每次启动都重新生成。

```bash
sudo python3.12 - <<'PY'
import os
import secrets
path = '/etc/h3-api/h3-api-secret.env'
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
    output.write('H3_API_KEY=' + secrets.token_urlsafe(32) + '\n')
PY
```

两个服务读取相同的密钥文件。配置文件供 systemd 读取，不要直接 `source` 到 shell；不要把密钥文件提交到仓库。本版只有一个共享身份，持有密钥者可访问同一数据目录内的任务，不提供多租户隔离。

安装 unit，并在启动前验证：

```bash
sudo install -m 0644 "$H3_RENDER_DIR/h3-api.service" /etc/systemd/system/h3-api.service
sudo install -m 0644 "$H3_RENDER_DIR/h3-worker.service" /etc/systemd/system/h3-worker.service
sudo systemd-analyze verify /etc/systemd/system/h3-api.service /etc/systemd/system/h3-worker.service
sudo systemctl daemon-reload
```

先处理验证输出中的错误，再进入下一节。两个服务均为 `Restart=no`、`UMask=0077`，没有 `[Install]` 节，不配置开机启动，也不互相拉起。API 的工作目录为 `/`，模型子进程的工作目录由 backend 设置为 `H3_RUNNER_DIR`。

## 8. 手工启动与首次链路验收

先启动 API，检查 HTTP 存活状态，再确认队列内任务和运行预算后启动 worker。若复用状态目录，worker 启动后可能立即处理其中的 queued 任务。

```bash
sudo systemctl start h3-api.service
sudo systemctl status h3-api.service

python3.12 - <<'PY'
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:30010/healthz', timeout=5) as response:
    print(response.read().decode('utf-8'))
PY

sudo systemctl start h3-worker.service
sudo systemctl status h3-worker.service
```

`/healthz` 用于 HTTP 存活探测，响应中的 `model_readiness` 固定为 `"not_checked"`。模型作业进度通过任务状态和 worker 日志查看。查看两个服务日志：

```bash
sudo journalctl -u h3-api.service -u h3-worker.service -n 100 --no-pager
```

使用 [示例客户端](../h3-api/README.md#使用示例客户端) 提交自己准备的单张图片和 UTF-8 提示词。客户端的 `H3_API_KEY` 应通过私有配置提供，与服务密钥一致。首次模型验收建议沿用已验证的 5 秒、`ratio=1:1` 组合；实际提交会启动完整 CPU 推理，HTTP 快速返回任务 ID 不代表视频立即完成。

验收应覆盖成功状态、文件下载、完整解码、实际帧数与音轨时长，并人工检查图像参考遵循、画质、运动和声音。客户端断线后使用回执里的 `task_id` 继续查询和下载，不重新提交一份生成任务；如果 POST 结果不确定，按客户端说明处理原幂等键。

## 9. 手工停机与数据保留

停止本地服务时先停 worker，再停 API：

```bash
sudo systemctl stop h3-worker.service
sudo systemctl stop h3-api.service
sudo systemctl is-active h3-worker.service h3-api.service
```

最后一条在服务已停止时返回 `inactive` 和非零退出码，属于该命令的正常语义。worker 使用 `KillMode=control-group` 和 240 秒停止等待，结束其生成子进程；不要把停止命令发出当成所有进程已经退出，应核对服务状态和日志。

本版有以下运行边界：

- HTTP DELETE 只取消 queued 任务；**没有 HTTP 取消 running 任务**的接口。停止 worker 会中断作业，不能从中间采样步骤恢复；worker 恢复时将遗留 running 任务标记失败，不自动重算。
- 每个数据目录仅一个串行 worker，每项任务重新启动模型子进程。不要同时启动多个 worker 竞争同一目录。
- 任务仅在创建后 7 天内可查询；签名下载 URL 默认有效 1 小时，可在查询期内重新获取。签名链接本身可用于下载，应作为访问凭据保管。
- 过期或逻辑删除不会清理数据库历史行，也不会删除任务图片、提示词、日志和视频。自行安排备份与保留策略，在没有活动任务时进行存储清理。
- **作业超时、停止 API/worker、操作系统关机都不等于停止云端计算计费。** 若使用云主机，需要另行按云平台规则停止或释放计算资源，并核对实际资源状态；云盘等保留资源仍可能计费。

## 常见阻断点

| 现象 | 检查方向 |
|---|---|
| 固定 CPU wheel 无法安装 | 核对 Python 3.12、Linux x86_64、官方 CPU 索引和指定版本；更新依赖时同步更新版本清单并执行环境检查 |
| `Pinned ... source is unverified` | 检查模型 venv、固定提交、目录安装来源及 `h3-source-install.json`，重新安装并审计实际代码 |
| `Manifest digest mismatch` | 恢复仓库中两个原始 JSON 的完整字节，不编辑指纹或文件项 |
| `Missing completed ... selection` | 使用同一个模型目录，先完成 base，再补 ref，保留合并的 `.h3-cpu-manifest.json` |
| 可用内存或线程数检查失败 | 查看当前可用内存和 CPU affinity；API 不提供降低 256 GiB 内存保护的字段 |
| API 正常但任务失败 | 检查 worker 日志、任务私有日志、模型 Python 路径、runner 辅助文件和目录权限 |
| 采样结束但仍未成功 | 还需解码、封装、媒体检查及结果发布，查看后续阶段日志 |

接口字段、支持范围和客户端恢复方式以 [API README](../h3-api/README.md) 为准；systemd 参数和生成文件语义以 [部署模板说明](../h3-api/deploy/README.md) 为准。
