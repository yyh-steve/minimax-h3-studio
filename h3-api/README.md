# 自部署 H3 API

为自己部署的 H3 CPU runner 提供 **MiniMax V2 风格的异步接口**，支持单图 Ref2VA 音视频生成、任务排队、查询和下载，可独立调用或接入本项目的视频工作台。

**实测记录：**自部署服务完成了一次“云端 API → H3 CPU 推理 → 笔记本下载”的 5 秒请求：768×768、120 帧、24 fps、32 kHz 双声道、49 次实际采样，推理耗时约 110.9 分钟。完整解码和下载哈希一致性检查通过；客户端断线后按原任务 ID 恢复，API 重启后仍可查询和下载，实际仅生成一个任务。[查看原始视频](../docs/media/h3-api-ref2va-5s.mp4)与[详细记录](../docs/validation.md)。

本仓库的`h3-api/`包含API、锁定依赖与客户端，`cpu-runtime/`包含runner辅助脚本和两份模型审计清单。完整项目还包含`pipeline/`视频工作台，见[根README](../README.md)。从零安装模型环境见[部署指南](../docs/deployment.md)；仓库不包含权重、账号资料或完整离线安装镜像。

## 支持范围

请求必须包含 `model="MiniMax-H3"`、`resolution="768P"`、整数 `duration=4..15`，以及恰好一条文本和一张参考图片：

```json
{
  "model": "MiniMax-H3",
  "resolution": "768P",
  "duration": 5,
  "ratio": "1:1",
  "content": [
    {"type": "text", "text": "A character looks toward the window and smiles. Quiet room ambience."},
    {
      "type": "image_url",
      "role": "reference_image",
      "image_url": {"url": "https://your-public-image-host.example/reference.png"}
    }
  ]
}
```

上面的图片URL仅为结构示例，调用时替换为真实可访问地址。

- 文本必须非空白，最多7000个字符；不会自动调用Context-IR增强提示词。
- 图片为单帧JPEG、PNG或WEBP；支持公网HTTPS URL，或`data:image/png;base64,...`等对应格式的data URI。不接受裸base64、HTTP图片、服务器文件路径、`file://`、`mm_file://`或带用户名密码的URL。
- 原始图片最多30,000,000 bytes；每边256–5760像素，最多33,177,600像素，宽高比0.4–2.5。动画图片不支持。worker校验真实格式，按EXIF方向归正并转为去除元数据的RGB PNG；URL下载和图片解码发生在排队之后，提交成功不表示图片已经通过验证。
- `aigc_watermark`只能省略或传`false`。本地API不实现水印开关的其他行为。
- 未实现的字段会报错，不会静默忽略。例如`callback_url`、`seed`、自定义采样步数、多图、视频/音频参考、首尾帧角色、T2VA、2K、Turbo、Context-IR和Regenerate均不在本版范围内。未实现的路由返回404。

### 时长与画布

`duration`是最终交付的整数秒数。内部先生成符合H3帧网格的视频和音频，再裁取前`duration×24`帧及每声道`duration×32000`样本；不补帧、不变速。4秒内部生成124帧再裁至96帧；15秒内部362帧裁至360帧。

`ratio`默认`adaptive`，支持下列固定画布。宽高是实际像素，比例名是接口标签：

| ratio | 实际宽×高 |
|---|---:|
| `1:1` | 768×768 |
| `16:9` | 1344×768 |
| `9:16` | 768×1344 |
| `4:3` | 1024×768 |
| `3:4` | 768×1024 |
| `21:9` | 1536×672 |

`adaptive`按参考图比例选择最接近的一个标签，不返回任意尺寸。受32像素网格和面积限制，`16:9`、`9:16`、`21:9`是近似画布；`768P`是本版档位名，不保证每种画布短边都为768像素。模型精度固定BF16/双VAE FP32，实际采样49次；这些不是本版可调API字段。

## 安装与启动

在 Ubuntu 上由 systemd 管理时，可使用 `deploy/render_units.py` 生成两个服务及配置示例，详见 `deploy/README.md`。生成器只写文件；安装、启动和云资源启停仍是独立步骤。服务不设置开机启动或自动重启，首次运行前需检查排队任务。

0.1.1 保留模型虚拟环境的 Python 入口符号链接，避免将其解析为系统解释器而丢失模型依赖；新增真实虚拟环境和部署配置回归测试。

真实worker要求已部署的Linux x86_64 H3 CPU环境、足够内存和磁盘，以及系统`ffmpeg`/`ffprobe`。API环境与模型Python环境分开；不要将API依赖装进已验证的模型虚拟环境。

在本目录创建独立API环境：

```bash
python3.12 -m venv .venv-api
.venv-api/bin/python -m pip install -r requirements.lock
.venv-api/bin/python -m pip install --no-deps .
.venv-api/bin/python -m pip check
```

`requirements.lock`固定API依赖版本。升级依赖时同步更新锁文件，并运行本项目的API回归检查。

准备一个两端共用的私有密钥文件。下列命令只写本地文件，不打印密钥；文件已存在时会拒绝覆盖：

```bash
umask 077
.venv-api/bin/python - <<'PY'
from pathlib import Path
import secrets
with Path('.h3-api.env').open('x', encoding='utf-8') as out:
    out.write('export H3_API_KEY=' + secrets.token_urlsafe(32) + '\n')
PY
```

`H3_API_KEY`至少24个无空白ASCII字符。不要将`.h3-api.env`提交到版本库或分享给不应访问任务的人。本版为**一个共享身份**：持有该密钥即可访问同一数据目录内的任务，不提供账号、租户或项目隔离。

在两个终端中均进入本目录，并设置相同的配置。以下模型路径是默认示例，请改为本机已存在的安装位置：

```bash
source .h3-api.env
export H3_DATA_DIR="$PWD/h3-data"
export H3_PUBLIC_BASE_URL="http://127.0.0.1:30010"
export H3_RUNNER_DIR="/data/h3-cpu"
export H3_MODEL_DIR="/data/h3-cpu/model"
export H3_MODEL_PYTHON="/data/h3-cpu/.venv/bin/python"
export H3_THREADS=80
export H3_JOB_TIMEOUT=64800
export H3_QUEUE_LIMIT=8
```

| 配置 | 用途与默认值 |
|---|---|
| `H3_DATA_DIR` | SQLite任务库和任务文件；默认`./h3-data`。两个进程必须共用同一个绝对路径 |
| `H3_PUBLIC_BASE_URL` | 签名下载URL的来源地址，默认`http://127.0.0.1:30010`；只接受不含路径、查询或凭据的HTTP(S) origin |
| `H3_RUNNER_DIR` | runner及辅助Python文件目录，默认`/data/h3-cpu` |
| `H3_MODEL_DIR` | 已下载且有完成清单的模型目录，默认`/data/h3-cpu/model` |
| `H3_MODEL_PYTHON` | 已安装H3依赖的Python，默认`/data/h3-cpu/.venv/bin/python` |
| `H3_THREADS` | 模型计算线程，默认80；不能超过实际CPU affinity |
| `H3_JOB_TIMEOUT` | 单个模型子进程的作业上限，默认64800秒（18小时） |
| `H3_QUEUE_LIMIT` | queued与running任务合计上限，默认8；超出返回429 |

终端一启动API：

```bash
.venv-api/bin/python -m h3_api api
```

终端二启动串行worker：

```bash
.venv-api/bin/python -m h3_api worker
```

API默认仅绑定`127.0.0.1:30010`，以上步骤不开放公网。`H3_PUBLIC_BASE_URL`只影响返回链接，不改变监听地址；若以后经受控代理使用，需由部署人员配置客户端可访问的地址、TLS和访问控制。

复用原模型环境前，需将发布包`cpu-runtime/`内更新后的`h3_cpu_trial.py`、`exact_duration.py`、`ref2va_inputs.py`、`ref_audio_resample.py`、`source_provenance.py`、`download_from_modelscope.py`及两份`modelscope-h3-*.json`审计清单同步到`H3_RUNNER_DIR`；源码目录就是本仓库的`cpu-runtime/`。也可以将`H3_RUNNER_DIR`设为解压后`cpu-runtime/`的绝对路径，前提是worker可访问它。runner必须支持`--duration-seconds`和`--prompt-file`。保留原模型权重、模型虚拟环境和源码来源记录；安装API包不会完成这些模型准备。

`GET /healthz`用于HTTP存活探测，返回的`model_readiness`固定为`not_checked`。模型作业进度通过任务状态和worker日志查看。

## 使用示例客户端

`examples/client.py`仅使用Python标准库，不需要额外客户端依赖。密钥只从环境变量读取；提示词从UTF-8文件读取，不放进客户端命令行参数。

```bash
source .h3-api.env
python3.12 examples/client.py \
  --image ./reference.png \
  --prompt-file ./prompt.txt \
  --duration 5 --ratio 1:1 \
  --base-url http://127.0.0.1:30010 \
  --output ./result.mp4
```

本地图片会转成data URI提交。`--image`也可接受公网HTTPS URL或完整data URI；较大的图片推荐传本地文件路径。输出已存在时，客户端会在提交前拒绝执行。

客户端提交后立即打印任务ID，并保存`result.mp4.task.json`回执。回执包含任务ID、服务地址、请求指纹和本地幂等键，不含API密钥、提示词正文、图片数据或签名下载URL。可用`--receipt`指定其他文件。每次HTTP请求都有超时，默认30秒；轮询间隔默认15秒，仅在状态变化时打印。

断线、客户端退出或下载失败后，重新设置`H3_API_KEY`，用已保存的ID继续查询和下载，不重新生成：

```bash
python3.12 examples/client.py \
  --task-id YOUR_32_CHARACTER_TASK_ID \
  --base-url http://127.0.0.1:30010 \
  --output ./result.mp4
```

`--task-id`模式不会发送POST，也不接受新的图片、提示词、时长或比例。Ctrl+C只停止客户端，不取消服务器任务。下载使用查询得到的新签名URL，不把Bearer密钥发送给下载地址；已有输出不会覆盖，失败的临时下载会清理。

### 提交超时与本地幂等扩展

客户端**从不自动重试POST**。服务可能已接受任务，只是响应未到达；直接重新提交可能重复计算。客户端在POST之前先保存随机`Idempotency-Key`，也可显式传`--idempotency-key`。

本地扩展的规则是：同一个键、同一个规范化请求、任务仍在有效查询期且未删除时，返回同一任务ID；换请求、任务已删除或已过期时返回409。不能把旧键用于新的生成任务。这是本地约定，不是对官方服务幂等行为的承诺。

已failed或cancelled的任务使用原键仍返回原任务，不会重新生成。只有确实要发起新一轮计算时才使用新键。

如果回执有`task_id`，始终使用`--task-id`。若POST中断且回执仍是`submission_unknown`、没有ID，先查询任务列表确认；需要人工重新提交时，必须保留原图片/URL、提示词和参数，显式使用回执中的同一幂等键，并用新的`--receipt`文件。客户端不会自动执行这一步，也不能凭请求超时认定任务未创建。

## HTTP接口

除`/healthz`和带有效签名的媒体URL外，均要求：

```text
Authorization: Bearer <H3_API_KEY>
```

| 方法与路径 | 行为 |
|---|---|
| `GET /healthz` | HTTP存活检查 |
| `GET /capabilities` | 当前支持的参数与运行配置 |
| `POST /v2/video_generation` | 提交JSON请求，返回`{"task_id":"..."}`；图片处理由worker执行 |
| `GET /v2/query/video_generation/{task_id}` | 返回`{"task":{...}}` |
| `GET /v2/query/video_generation` | 返回`items`和`total`；支持分页和过滤 |
| `DELETE /v2/video_generation/{task_id}` | queued任务取消；succeeded/failed任务逻辑删除 |
| `GET /media/{task_id}.mp4?expires=...&sig=...` | 签名下载，无需Bearer |

列表参数：`page_num`默认1，`page_size`默认20、范围1–100；可传`filter.status`、重复的`filter.task_ids`、`filter.model`和`filter.task_type`。其他查询参数报错。

任务状态为`queued`、`running`、`succeeded`、`failed`、`cancelled`。只有`succeeded`提供`task.content.url`。`task.usage.*_seconds`表示输出媒体时长，不是模型运行耗时或费用。失败详情位于`task.error`。

DELETE行为有意限制：仅queued可取消；running与已经cancelled的任务返回400。删除succeeded/failed任务后，查询及下载返回404。没有通过HTTP取消运行中任务或从中间采样步骤继续的接口。

## 运行与存储边界

- 每个数据目录仅一个worker串行执行；任务之间重新启动模型子进程，不提供常驻模型或并行生成承诺。API进程重启不会直接重跑任务；worker恢复时将遗留running任务标记失败，不自动重试，也无断点续推。
- 签名下载链接默认有效1小时；在任务可查询期间重新GET任务可获得新链接。链接本身是访问凭据，持有链接者在有效期内无需Bearer即可下载，勿随意公开。
- 任务按创建时间仅能查询最近7天；过期或逻辑删除后不能再通过本API取回。上述时间是当前默认设置，当前环境变量入口不提供修改这两个期限的选项。
- 查询过期或DELETE**不自动清理数据库中的历史行，也不删除任务目录中的图片、提示词、日志和视频**。部署人员需自行安排备份、保留期和磁盘清理，清理前确认没有活动任务；否则存储会持续增长。
- 共用一个API密钥不构成多租户隔离。任务数据目录包含用户输入和私有日志，应保持私有权限。更换密钥还会使旧签名链接失效。
- 采样完成不等于视频已经可下载；仍需解码、封装、媒体检查和结果发布。HTTP异步响应速度不代表生成速度。
- `H3_JOB_TIMEOUT`只限制模型作业，**不会停止云实例计算计费**。停止API、worker或操作系统也不等于云端停费；云资源收尾需由操作者按所在平台的计费规则明确执行并验证。
