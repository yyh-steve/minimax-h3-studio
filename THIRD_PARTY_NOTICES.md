# 第三方来源与许可

本仓库的应用代码、模型权重、上游框架和样例素材属于不同的许可范围。将它们组成一个项目不改变各自的使用条件；当前未为原创应用代码指定额外的统一开源许可证。

## 模型与框架

| 项目 | 来源与固定版本 | 说明 |
| --- | --- | --- |
| MiniMax H3 | [模型仓库](https://huggingface.co/MiniMaxAI/MiniMax-H3)，revision `42ed227ee7df40d41602854ae760620d6eb651fe` | 权重未收入本仓库；下载和使用前阅读模型仓库 LICENSE |
| ModelScope 镜像 | revision `57559a6720ddaa2eaee52b09ec2ef011127997b2` | 两份下载清单用于核验固定模型文件，不是模型使用授权 |
| Diffusers | [GitHub](https://github.com/huggingface/diffusers)，commit `b800f828f3951c4d206e154411bcbfa693cd322e` | Apache-2.0；CPU 几何测试用到的固定源码夹具单独保留来源和许可证 |
| Transformers | [GitHub](https://github.com/huggingface/transformers)，commit `d4804f9d76b8c071b993d813e9e87067b780cbee` | Apache-2.0；安装脚本从官方仓库取得源码 |

Python 和系统依赖仍遵循各自许可证。FFmpeg 的适用许可证与编译配置有关，请查看实际使用的发行包。上述许可证不覆盖未随仓库分发的模型、服务协议或商业 API 权限。

## 视频样例与参考照片

`docs/media/h3-cinematic-15s.mp4` 和 `docs/media/h3-api-ref2va-5s.mp4` 是 H3 生成的虚构表演样例，不是原照片人物的真实录像。

人物参考照片为 Lu Yu 的 **Asian young woman**，来源：[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Asian_young_woman.jpg)，许可：[CC BY 2.0](https://creativecommons.org/licenses/by/2.0/)。本项目对参考照片做了裁剪并用于生成样例；照片原图未包含在仓库中。Word 部署方案也保留了该署名和来源。

对样例的技术核验仅说明媒体可解码、时长和文件一致性等指标，不代表对模型输出授予了额外权利。项目与 MiniMax、火山引擎及其他依赖提供方没有官方隶属关系。
