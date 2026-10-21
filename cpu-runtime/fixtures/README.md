# 固定上游解码器测试夹具

`diffusers/` 只包含 CPU 几何回归需要的一个完整上游文件、其原始 LICENSE 和来源清单。代码与许可证均未修改，不包含模型权重、源码 tar 或 `.git`。

- 上游项目：[huggingface/diffusers](https://github.com/huggingface/diffusers)
- 固定 commit：`b800f828f3951c4d206e154411bcbfa693cd322e`
- 原路径：`src/diffusers/models/autoencoders/autoencoder_kl_minimax_h3.py`
- [固定版本源码](https://github.com/huggingface/diffusers/blob/b800f828f3951c4d206e154411bcbfa693cd322e/src/diffusers/models/autoencoders/autoencoder_kl_minimax_h3.py)
- 许可证：Apache 2.0，完整文本见 [diffusers/LICENSE](diffusers/LICENSE)，保留原文件内的版权声明。
- 源文件 SHA256：`58e0ed47fdee8227d738eec5bb1e7a47acc21c65ee3de01aa3795c97d679d015`
- 源文件与许可证的大小及 SHA256 另见 [diffusers/manifest.json](diffusers/manifest.json)。

`test_exact_duration.py` 先核对源文件 SHA256，再从 AST 中提取 `AutoencoderKLMiniMaxH3._decode`。测试使用 NumPy 数组和轻量替身，只核对固定版本的解码分块几何：107 个视频 latent 能产生 362 帧。它不导入此上游模块、不安装 Diffusers、不加载模型或执行学习到的 VAE。

正式生成仍使用经过 `bootstrap-sources.sh` 与 `source_provenance.py` 安装和审计的真实框架。夹具不是运行时框架替代品，也不表示新增了一种模型实现。
