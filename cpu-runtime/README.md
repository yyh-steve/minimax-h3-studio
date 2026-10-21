# H3 CPU 运行时

从零安装、模型下载、API 接入和手动停止流程见 [部署指南](../docs/deployment.md)。本目录是 API 与流水线共用的 CPU runner；正式执行面向 Linux x86_64、Python 3.12 和高内存主机。

`h3_cpu_trial.py` 提供分阶段检查、离线推理和媒体验收。推理必须显式加 `--execute-inference`；默认不会加载模型。`--duration-seconds 4..15` 先生成对齐的原生帧，再截取真实前缀为目标时长；没有插帧、补帧或变速。自部署 H3 的 5 秒 API 样例和 15 秒电影人物样例见[生成记录](../docs/validation.md)。

## 安装与来源

- `requirements-cpu.txt` 固定 Diffusers 和 Transformers 的源码 commit。
- `constraints-cpu-core.txt` 约束已验证的核心版本；它不是完整传递依赖锁。先安装官方 CPU wheels，再运行 bootstrap；安装后保留 `pip freeze` 和来源记录。
- `bootstrap-sources.sh` 默认仅输出计划；显式执行时获取固定源码、从普通目录安装并运行 `source_provenance.py`。不需要另附上游 tar，也不会下载模型。
- `download_from_modelscope.py` 使用两份原始审计清单，先完成 base 63 文件，再完成 ref 16 文件；清单按完整文件 SHA256 校验，不能格式化或改字节。
- `fixtures/` 为带原许可证和 SHA256 的上游测试夹具，不参与正式推理。

## 不依赖 torch 或真实模型的检查

需要 Python 3.12、NumPy、Pillow、git、ffmpeg 和 ffprobe；可使用独立开发环境，不必安装模型环境。以下从仓库根目录执行：

```bash
cd cpu-runtime
python -m unittest -v \
  test_duration_seconds test_exact_duration \
  test_modelscope_download test_ref2va_inputs test_source_provenance \
  test_exact_duration_qa test_qa_reference_match
```

这些检查覆盖时长计划、原生解码几何、输入契约、审计清单、来源记录及小型合成媒体验收，不会下载权重或执行真实 H3 推理。`test_exact_duration` 中两项已安装编码器检查默认跳过。

## 需要 CPU torch 的检查

`test_ref_audio_resample.py` 需要 CPU torch、NumPy 与 ffmpeg，只验证合成 PCM 的真实重采样，不加载 H3 权重。在准备好的 Linux CPU 虚拟环境中运行：

```bash
cd cpu-runtime
python -m unittest -v test_ref_audio_resample
```

如果还要启用已安装框架的编码器检查，应先完成部署指南中的来源审计，并提供本地模型配置目录：

```bash
cd cpu-runtime
H3_TEST_INSTALLED_ENCODER=1 H3_TEST_MODEL_DIR=/data/h3/model \
  python -m unittest -v test_exact_duration.InstalledEncoderTest
```

该可选检查使用真实框架和合成小媒体，并读取本地模型元数据。运行生成任务后，可结合输出报告检查画面和声音。
