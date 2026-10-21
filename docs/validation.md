# 实测记录与复现检查

本文汇总流水线软件、H3 API 协议和 CPU 模型执行的测试结果、真实生成记录与复现命令。

整理本仓库时的本地验证结果：

| 检查 | 结果 |
| --- | --- |
| 流水线 Python 测试 | 217 项通过，无跳过 |
| 前端安全测试 | 9 项通过 |
| H3 API 测试 | 75 项及 108 个子检查通过，真实 HTTP smoke 通过 |
| CPU 轻量回归 | 57 项通过，2 项需真实安装环境的检查按设计跳过 |
| 隔离页面检查 | 启动、模拟项目创建、本地模式默认值、缺少本地密钥的阻断均通过 |

合成与质检测试会在临时目录现场生成小视频，不依赖开发者机器上的历史素材目录。

## 自动化检查

### 视频流水线

```bash
cd pipeline/backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pytest -q
node --test tests/test_web_security.cjs
```

测试用临时目录与模拟 M3/H3 覆盖分镜、确认闸门、Take 状态、重试、恢复、技术质检、人工采纳和 FFmpeg 合成。开发测试需要 `ffmpeg`、`ffprobe` 和 Node.js 20 或以上；Node 只用于前端安全测试，运行工作台不需要它。不需要真实 API Key。

### H3 异步 API

```bash
cd h3-api
python3.12 -m venv .venv-api
.venv-api/bin/python -m pip install -r requirements-dev.lock
.venv-api/bin/python -m pytest -q
.venv-api/bin/python tests/http_smoke.py
```

这组测试验证鉴权、字段白名单、SQLite 原子领取、幂等、输入安全、worker 锁、子进程、超时、签名下载与媒体检查。HTTP smoke 使用真实回环 socket 和 FFmpeg 合成媒体，不加载 H3 权重。

### CPU 轻量回归

从仓库根目录执行，环境需要 NumPy、Pillow、FFmpeg：

```bash
PYTHONPATH=cpu-runtime python3.12 -m unittest \
  test_duration_seconds test_exact_duration test_exact_duration_qa \
  test_ref2va_inputs test_modelscope_download test_source_provenance \
  test_qa_reference_match -q
```

安装环境检查可在准备好 CPU 模型环境后显式开启，音频重采样测试需要 CPU torch。完整要求见 [CPU runtime README](../cpu-runtime/README.md)。

## 已取得的真实 H3 证据

| 场景 | 输出 | 实测执行时间 | 验收 |
| --- | --- | --- | --- |
| 独立 runner 单图参考 | 约 5.17 秒，768×768 | 约 109 分钟 | 完整 49 次采样、可解码媒体 |
| 独立 runner 电影人物片 | 精确 15 秒，1344×768 | 约 14 小时 21 分钟 | 360 帧、24 fps、32 kHz 双声道 |
| 新 API 真实单图参考 | 精确 5 秒，768×768 | 110.9 分钟 | 120 帧、49 次采样、完整解码、下载哈希一致 |

最后一项还验证了客户端断线后按原任务 ID 恢复，以及 API 重启后的查询和签名下载；实际只生成一个任务。原实例在验收后进入节省停机模式，没有为了仓库整理再次开机。

两条随仓库提供的媒体：

| 文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| `docs/media/h3-api-ref2va-5s.mp4` | 860202 | `3365be506e5893c768f5e60a3a70699feb1f37a1ccafa510ba375a67ffa8bb2b` |
| `docs/media/h3-cinematic-15s.mp4` | 1534926 | `1259819a5218d05f3339f5da8f3f8f724a30102fee5514237537c68e2c110ea9` |

来源摘要在 [`h3-api/test-results/`](../h3-api/test-results/)。原实验的私有日志、账号、地址和任务数据库没有收入仓库。
