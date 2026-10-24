# 从 H3 部署条件到 CPU 方案：前期调研与选型

最初的需求是：**把 MiniMax H3 部署到自己的设备或服务器上，再封装成可以调用的 API。** 我先考虑自己的 RTX 5060 Ti 16 GB，然后查看公司已有云账号能够提供哪些资源。CPU 是这一轮调研、对照和筛选后的选择。

整个判断过程是：**查清 H3 组件和运行方式 → 对照本地 GPU → 检查云产品与实际资源 → 选择大内存 CPU → 完成 Ref2VA → 封装异步 API。**

## 1. 先查模型到底需要加载什么

我先读 [MiniMax 官方模型卡][model-card]，确认任务分区和组件；再读 [Diffusers 部署文档][diffusers]与 [SGLang H3 配方][sglang]，看不同设备、精度和组件放置方式怎样运行；最后按本项目固定版本的[基础清单](../cpu-runtime/modelscope-h3-manifest.json)与[参考分支清单](../cpu-runtime/modelscope-h3-ref-manifest.json)累加文件尺寸。

H3 包含 33B 参数的 Omni-Transformer，条件编码器使用 Qwen3-VL-32B，还需要视频 VAE、音频 VAE、处理器和调度器。FL2VA 分区承接文本及首尾帧条件，Ref2VA 分区承接参考媒体条件；本项目最终使用单张参考图生成音视频。

| 组件 | 作用 | 固定快照文件容量 |
| --- | --- | ---: |
| Ref2VA Transformer | 在参考条件下生成音视频潜变量 | 61.729 GiB |
| 条件编码器 | 理解文本与参考媒体，提供生成条件 | 62.144 GiB |
| 视频 VAE | 视频像素与潜变量之间的转换 | 9.700 GiB |
| 音频 VAE | 音频与潜变量之间的转换 | 0.564 GiB |

一份任务 Transformer 加共享组件、配置等约 **134.158 GiB**。本项目先保留基础分支，再补参考分支，磁盘共存文件约 **195.887 GiB**。这些是文件容量；运行时还要考虑激活、注意力、条件缓存、解码和框架开销。显存、主机 RAM 和磁盘分别承担计算驻留、卸载或 CPU 执行、文件保存，容量需要分开核算。

因此先看“能装下什么、组件放在哪里”，再看算力。GPU 宣传的 AI TOPS 无法直接回答 H3 能否加载或生成多久。

这一步还决定下载范围：先看工作流会调用哪些组件，避免把整个仓库中的不同任务分支和不同格式全部下载。模型路径、框架版本、权重版本和组件配置要能对应起来，才能把“文件下载完成”推进为“这组文件能够组成需要的推理管线”。

## 2. 为什么没有直接用自己的 5060 Ti

我的显卡是 **RTX 5060 Ti 16 GB**。[NVIDIA 官方规格][nvidia]区分 8 GB 和 16 GB 两个版本，这里按自己的 16 GB 判断。单个完整 Transformer 文件就约 61.729 GiB，显存不足以直接照搬本方案的完整 BF16/FP32 组件常驻方式。

继续看框架文档，可以发现显存要求随运行方式变化：SGLang 给出 4×H100 80 GB 的 TP2＋Ulysses2，以及 4×H200 141 GB 的 Ulysses4 等服务器配方。它们说明权重切分、序列并行、单卡容量和互联需要一起配置，不能将“四卡”机械当成所有 H3 路线的最低要求。

Diffusers 同时提供低显存方向：对 Transformer 和条件编码器做 INT8 量化，通过 block/leaf offload 将主要权重放在主机 RAM；12–16 GB 显存时继续卸载视频 VAE，并使用例如 960×544 的小画布，文档给出的主机 RAM 量级约 75 GB。这个示例使用 T2VA 工作流。

| 路线 | 主要条件 | 对本次选择的影响 |
| --- | --- | --- |
| 完整组件常驻 GPU | 足够显存，并匹配并行拓扑和框架 | 自有 16 GB 显卡无法直接采用 |
| INT8＋分层卸载 | 量化、足够主机 RAM、小画布及组件迁移 | 适合另行配置低显存实验 |
| 本项目 CPU 路线 | 大容量 RAM、CPU 算子和充足运行时间 | 保留原始权重与 768P 生成目标 |

所以判断是“这张显卡不能直接承载本次选定方案”。低显存卸载仍有专门路径，需要连同主机 RAM 和工作流一起选型。本次优先使用可获得的大内存资源，维持 Transformer、条件编码器 BF16 和双 VAE FP32 的执行方式。

## 3. 再查公司云账号中实际能用什么

接着进入公司已有云账号，查看现有资源和创建页面。检查顺序包括 veMLP 开发机、GPU ECS，以及 VKE/VCI 等容器入口。每个入口都要分清三件事：是否有使用权限、额度是多少、目标地域实际能分配什么机器。

| 候选 | 查什么 | 本次排查得到的判断 |
| --- | --- | --- |
| veMLP 开发机 | 队列角色、资源组、镜像及底层 GPU | 有队列需要申请加入，部分入口要求跨服务授权；一些地域没有可用队列或资源组 |
| GPU ECS | GPU 型号、显存、主机 RAM、可用区与价格 | 部分 A100/A10/HPC 规格当时显示售罄或无可用区；部分 V100/T4 可继续配置 |
| VKE / VCI | 实际 GPU 节点、实例及可分配资源 | 集群和额度展示不能直接提供所需 GPU；还需落实底层算力 |
| CPU ECS | 能否创建大内存实例、CPU 架构、存储 | 现有账号可以创建 160 vCPU / 768 GiB 规格，具备明确的落地路径 |

看到 GPU 额度，不等于已有空闲卡；看到可选规格，还要核实整机条件与最终分配。这里记录的是当时所查资源状态。涉及新增权限、队列加入和跨服务授权时，按照约定先停在申请前。

V100/T4 也做了进一步对照：可选单卡 V100 有 32 GB 显存，但整机 RAM 为 32 GiB；T4 有 16 GB 显存，对应整机 RAM 为 16 或 26 GiB。它们难以直接承载前述约 75 GB 主机 RAM 的 INT8 卸载配方，而且没有原生 BF16 加速，还涉及旧架构 CUDA 和 attention 内核选择。因此“能选到 GPU”需要继续落实为“整台机器适合这条 H3 路线”。

## 4. GPU 费用怎样纳入判断

资源选择还要考虑持续运行的成本。复盘时于 **2026-10-20** 补查了[火山引擎 ECS 官网刊例价][pricing]，用于说明 GPU 整机的费用量级；CPU 实例已于 10 月 18 日创建，报价补查与最初决策在时间上分开记录。

| 北京地域整机配置 | 按量参考价，元/台·小时 |
| --- | ---: |
| T4 16 GB×1，4 vCPU / 16 GiB | 11.2811 |
| V100 32 GB×1，8 vCPU / 32 GiB | 25.6662 |
| A100 80 GB×1，14 vCPU / 245 GiB | 40.39 |
| A100 80 GB×4，56 vCPU / 980 GiB | 161.54 |

这些报价覆盖实例计算资源，云盘、公网等另计，实际以购买时的配置和价格为准。高显存或多卡资源还可能涉及[购买条件][gpu-purchase]、[配额][quota]和[可购买地域][availability]。单条视频成本取决于小时单价乘实际占用时间，再加其他费用；CPU 的长时间生成同样需要计入。

## 5. 为什么大内存 CPU 成为实际方案

现有云账号能开通 **160 vCPU / 768 GiB CPU ECS**，大内存解决了完整组件的承载空间。软件侧，Diffusers H3 提供 CPU 设备路径，可以选择 PyTorch CPU 构建和 native PyTorch attention，逐步建立可执行的推理程序。

阅读文档时还要区分 CPU offload 和纯 CPU 推理：前者把权重暂存主机内存，计算时搬回 GPU；后者在 CPU 上执行模型计算。因此纯 CPU 路线需要单独检查算子、精度与 attention 后端，不能直接复制依赖 CUDA、Triton 或 GPU 专用内核的启动参数。

实际创建后，继续检查主机：Xeon 6986P-C、80 物理核/160 线程、2 个 NUMA 节点，配 80 GiB 系统盘和 500 GiB 数据盘。160 vCPU 包含逻辑线程；线程数要通过实际计算选择。

部署按四层推进：先检查 CPU 指令集、可用内存与 FP32/BF16 小算子；再固定框架源码和权重版本，核对文件哈希；然后运行少量计算，测加载、单步采样、解码时间和内存；最后完成单图 Ref2VA，并扩展到 15 秒人物镜头。

实际 768×768、约 5.17 秒单图 Ref2VA 在 80 计算线程下耗时约 109 分钟，峰值 RSS 134.80 GiB；1344×768 的 15 秒人物片约 14 小时 21 分钟，峰值 RSS 145.09 GiB。结果说明这台机器可以完成目标生成，同时给出了等待时间。768 GiB 配置包含较大余量，进程 RSS 随任务形状变化；本项目 runner 的 256 GiB 可用内存检查用于保留工程余量。

## 6. 生成耗时决定 API 怎样封装

小时级推理适合异步接口：FastAPI 接收参考图和提示词，立即返回 `task_id`；SQLite 持久保存队列与状态；独立 worker 串行调用 CPU runner；客户端轮询状态后下载 MP4。任务记录和幂等提交支持断线恢复，避免因为客户端重连重复生成。

最终形成“自己部署的 H3＋MiniMax V2 风格 API”，再由 HTTP 客户端或视频工作台接入。具体安装命令见 [CPU 部署指南](deployment.md)，请求参数与示例见 [H3 API 说明](../h3-api/README.md)。

[model-card]: https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/README.md
[diffusers]: https://github.com/huggingface/diffusers/blob/b800f828f3951c4d206e154411bcbfa693cd322e/docs/source/en/api/pipelines/minimax_h3.md
[sglang]: https://github.com/sgl-project/sglang/blob/d4e3c163c95afe5f549116c9c73a6f0e6a4830a7/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx
[nvidia]: https://www.nvidia.com/en-eu/geforce/graphics-cards/50-series/rtx-5060-family/
[pricing]: https://www.volcengine.com/pricing?product=ECS&tab=1
[gpu-purchase]: https://www.volcengine.com/docs/6419/69806?lang=zh
[quota]: https://www.volcengine.com/docs/6396/1328784?lang=zh
[availability]: https://www.volcengine.com/docs/6396/1515377
