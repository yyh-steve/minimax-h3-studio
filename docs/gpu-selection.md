# MiniMax H3 的 GPU 选型：显存、型号与整机配置

资料核对日期：2026 年 10 月 24 日。

自己部署 H3，先选运行方式，再选 GPU。**原精度常驻、多卡切分、CPU 卸载和 INT8 量化，对每卡显存与主机内存的要求不同。** H3 包含约 33B 的生成 Transformer、Qwen3-VL-32B 条件编码器和双 VAE；本项目固定快照的一份任务分支及共享组件约 134.158 GiB。权重文件容量只用于估算，运行时还要留出激活、条件序列、注意力和解码空间。

本页根据 [Diffusers 的 H3 Memory 配方][diffusers]和 [SGLang H3 配方][sglang]整理 GPU 选择。**上游具体配方**说明框架如何放置组件；**按显存档位的型号候选**用于筛选硬件；**工程 RAM 建议**用于整机采购预算，三者分别列出。仓库现有实现采用 CPU runner 与异步 API；GPU 路线使用上游 SGLang 或 Diffusers 推理后端。

Qwen 编码器先把提示词与参考媒体转换成条件特征，H3 Transformer 再反复去噪，最后由 VAE 解码。因此可以让不同组件轮流占用一张卡，也可以分别放在两张卡上。单张卡的容量要能承载“当前计算组件＋该阶段的激活与缓存”；如果组件本身也装不下，就进一步按层卸载或切分权重。这样才能从组件清单推导显存需求。

## 1. 企业级 GPU：80 GB 与 141 GB 怎么选

| 上游 H3 具体配方 | 显存条件 | 组件放置与并行 | 适合的取舍 |
| --- | --- | --- | --- |
| Diffusers 单卡卸载 | 1×80 GB | BF16；CPU 暂存组件，计算时搬入 GPU；预留 `12GB` 显存余量 | 控制 GPU 卡数，接受组件搬运开销 |
| Diffusers 双卡分组件 | 2×80 GB | Qwen 编码器放一张卡，Transformer 与 VAE 放另一张卡；BF16 常驻 | 保留原精度，避免各阶段反复从 CPU 搬权重 |
| SGLang H100 常驻 | 4×H100 80 GB | BF16/FP32；TP2＋Ulysses2，编码器跨四卡放置 | 80 GB 卡上的原精度多卡服务 |
| SGLang H200 常驻 | 4×H200 141 GB | BF16/FP32；Ulysses4，组件常驻 | 有较大显存与高速互联时，优先降低单请求延迟 |

Diffusers 的单、双 80 GB 示例代码使用 **T2VA**。SGLang 上述常用启动示例使用 `fl2va` 分区。部署本项目的单图 **Ref2VA** 时，需要选择 `ref2va` 分区，并按实际参考条件、画幅、时长配置资源。15 秒比短片包含更多视频潜变量，参考图也会增加条件序列；这些因素必须纳入每卡峰值预算。

“四卡”是对应配置的组成，不是 H3 的统一最低要求。H200 容量较大，适合纯 Ulysses4；H100 80 GB 通过 TP2 切分权重，再组合 Ulysses2。把 H200 的参数直接用于 H100，可能因权重放置不同而超出单卡容量。

### 同名 GPU 也要看完整规格

| 型号候选 | 每卡显存与显存带宽 | 选择时看什么 |
| --- | --- | --- |
| A100 80 GB SXM / PCIe | 均为 80 GB；约 2.039 / 1.935 TB/s | 可进入 80 GB 容量档；Ampere 原生支持 BF16，attention 内核按 SM80 选择 |
| A100 40 GB | 40 GB | 完整 BF16 Transformer 约 61.729 GiB，需量化、逐层卸载或权重切分 |
| H100 SXM 80 GB | 80 GB，约 3.35 TB/s | Hopper；适合带高速卡间互联的服务器 |
| H100 PCIe 80 GB | 80 GB，约 2 TB/s | 容量同为 80 GB，但带宽、功耗和互联形态与 SXM 不同 |
| H100 NVL | **94 GB/卡**，约 3.9 TB/s | 常以两张卡组成 188 GB 产品配置；仍是两个独立显存空间 |
| H200 SXM / NVL | 141 GB/卡，约 4.8 TB/s | 显存余量更大；购买时继续区分平台形态与卡间连接 |

规格来源：[A100][a100]、[H100 SXM/NVL][h100]、[H100 PCIe 产品资料][h100-pcie]、[H200][h200]。显存带宽是 GPU 读取自身显存的能力，不能替代卡间通信带宽。A100 80 GB 是 Diffusers 80 GB 档位的型号候选；SGLang 的四卡 H100 配方对应 Hopper，应同时匹配计算架构、内核与拓扑。

## 2. 消费级与工作站 GPU：显卡之外还要配 RAM

| 上游配方或容量档位 | 显存 | 配置方式 | 使用条件 |
| --- | --- | --- | --- |
| RTX 5060 Ti 所属低显存档 | 16 GB | Diffusers：Transformer 与编码器 INT8，block/leaf offload；视频 VAE 也卸载 | T2VA 示例采用 960×544 小画布，约 75 GB 主机 RAM 承载 INT8 权重 |
| RTX 4090 / 4090 D | 24 GB | SGLang：DiT INT8＋FlashAttention，DiT 与编码器逐层卸载 | VAE 保留适当驻留空间；该配方不把 VAE 加入逐层卸载列表 |
| RTX 5090 单卡 | 32 GB | SGLang：原始 BF16 权重，DiT、编码器与视频 VAE 分层驻留和卸载 | 主机 RAM、NVMe 和画布决定驻留层数；480P 的驻留参数需随 768P 调整 |
| RTX 5090 双卡 | 2×32 GB | SGLang：TP2，DiT 驻留 20 个逻辑层，其余逐层卸载 | 上游配方采用 377 GiB 主机，建议配 **384 GiB 级 RAM** |
| 双 48 GB 工作站/服务器卡 | 2×48 GB | Diffusers：Transformer 与编码器 INT8，按组件分卡常驻 | T2VA 内存配方；例如 L40S 属于 48 GB 型号候选，按其 Ada 架构配置内核 |

表中上游消费卡示例对应 T2VA；选择 Ref2VA 时沿用容量与组件放置的判断方法，并把参考媒体、目标画布和时长计入运行空间。

INT8 主要降低权重内存占用，敏感层保留原精度；它会改变部分数值计算。卸载则改变权重放置位置，以 CPU↔GPU 传输换取显存空间。两种手段可以组合，选择时应明确接受哪一种取舍。

自己的 **RTX 5060 Ti 16 GB** 无法直接容纳本项目完整 BF16/FP32 组件常驻方案；若选择低显存路线，就要同时准备 INT8、卸载、小画布和足够主机 RAM。仅把显卡升级成 24 GB 或 32 GB，也不能省略整机内存规划。[5060 Ti][rtx5060]、[4090][rtx4090]与[5090][rtx5090]的显存规格分别为 16 GB、24 GB、32 GB。

三者都支持原生 BF16，但显存带宽分别约为 448、1,008、1,792 GB/s，且都没有 NVLink。更高带宽有助于 GPU 上的计算，实际收益仍取决于多少权重需要走 PCIe。[L40S][l40s] 虽然是服务器卡，配有 48 GB ECC 显存与 864 GB/s 带宽，同样没有 NVLink；“企业卡”这个名称不能代替容量与互联检查。来源：[5060 Ti 带宽表][rtx5060-bandwidth]、[Blackwell 架构][blackwell]、[Ada 架构][ada]。

双 5090 的“驻留 20 层”建立在 TP2 切分基础上，每张卡只承载对应层的一部分，不能直接照搬到单 5090。消费级桌面的额外需求还包括电源、散热、PCIe 插槽宽度及系统占用；购买云实例时，优先查看整机规格。

### V100、T4 为什么不是这次优先选项

[V100][v100] 常见 16/32 GB，[T4][t4] 为 16 GB。两者没有原生 BF16 Tensor Core 加速，需选择适配旧架构的精度和 attention 后端；T4 也不能因为支持 INT8，就直接套用面向新架构的 H3 INT8 内核。

BF16 与 FP16 虽然都占两字节，数值范围并不相同。把 BF16 模型改成 FP16 需要连同敏感算子一起考虑；采用 FP32 又会增加权重和运行空间。原生 BF16 的代际边界可见 [NVIDIA Ampere Tensor Core 说明][bf16]。

本次云控制台中可选 V100 32 GB 整机仅配 32 GiB RAM，T4 整机配 16 或 26 GiB RAM，难以承载前述约 75 GB 的 INT8 主机权重。它们的判断依据是**显存＋主机 RAM＋软件架构一起看**；可购买一张 GPU，并不意味着整台实例适合这条卸载方案。

火山引擎的[公开实例规格][volc-gpu]提供了更直接的整机对照：`ecs.pni2.3xlarge` 为 A100 80 GB×1、14 vCPU、245 GiB RAM；`ecs.pni2.7xlarge` 为 A100 80 GB×2、28 vCPU、490 GiB RAM。相较之下，`ecs.g1ve.2xlarge` 是 V100 32 GB×1、32 GiB RAM，`ecs.g1te.2xlarge` 是 T4 16 GB×1、26 GiB RAM。这解释了为什么要先筛选“大显存＋足够主机内存”的整机，再检查目标地域是否能购买。

## 3. 显存不能相加，互联也必须匹配

- **组件分卡**：编码器与生成网络分别放置，阶段之间传递特征。两张 80 GB 卡各自管理内存。
- **TP**：将模型权重和计算切到多卡，每层都有集合通信；互联慢会抵消部分计算收益。
- **Ulysses**：切分序列与注意力工作量，不等于把全部权重平均分摊。SGLang 的相关通信流水需要卡间 P2P 与 stream memory operations。
- **FSDP**：这份 H3 实现切分 DiT 权重，在使用时汇集；编码器与 VAE 仍需各自安排。
- **CPU offload**：主机 RAM 存权重，PCIe 搬运到 GPU；RAM 不够而频繁读盘时，NVMe 性能成为重要条件。

多卡实例应查看 NVLink/NVSwitch、PCIe 拓扑、实际 P2P 能力和 NUMA 分布。两台独立单卡云服务器还多了一层网络通信，不能视为一台双卡服务器。H100/H200 的 Hopper 内核、A100 的 Ampere 内核与 RTX 5090 的 Blackwell 内核也应分别匹配；例如面向 Hopper 的 FlashAttention3 配方不能机械套用 A100。

拿到 GPU 主机后，可以先读取硬件信息，再选上游启动配置：

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
nvidia-smi topo -m
free -h
```

第一条确认真实卡型与每卡容量，第二条查看卡间连接和 CPU 亲和关系，第三条核对当前可用主机 RAM。还应确认驱动、CUDA、PyTorch 与目标 attention 实现适配，并为显示输出或其他进程预留显存。运行目标请求时分别记录每卡峰值、主机内存、编码、去噪、解码耗时，用同一画幅、参考图数量和步数比较配置。

## 4. 主机内存怎样留预算

下面是**工程采购建议**，用于覆盖权重暂存、加载过程、激活及系统余量；不是模型的固定最低内存。部署时再以目标 Ref2VA 请求的峰值调整。

| 路线 | 工程 RAM 建议起点 | 依据 |
| --- | --- | --- |
| 两大组件 INT8＋卸载 | 128 GiB 级 | 上游约 75 GB 为权重承载量级，另外留出加载和运行空间 |
| 单消费卡原精度逐层卸载 | 192 GiB 级 | 完整组件体量较大，优先用 RAM 减少反复读盘与 swap |
| 单/双 80 GB 与四卡企业级方案 | 256 GiB 级，结合实例规格增加 | 覆盖完整组件加载、缓存和进程余量；GPU 常驻不等于无需主机 RAM |
| 双 5090 原精度 TP2 配方 | 384 GiB 级 | 沿用上游该配方的整机内存建议 |

磁盘另算：一份任务分支及共享组件约 134.158 GiB，本项目两分支共存约 195.887 GiB；还要留模型下载缓存、虚拟环境和生成文件空间。RAM 与 GPU 显存都不能当作磁盘容量。

## 5. 实际选卡顺序与本项目落点

1. **先定目标**：单图 Ref2VA、768P、4–15 秒、原精度还是量化，再核对每卡峰值。
2. **有企业级资源**：先看单/双 80 GB 能否满足卡数与传输取舍；需要原精度常驻多卡服务时选 4×H100，显存和预算充足时选 4×H200。
3. **有消费卡**：连同 RAM 选择卸载方案；16 GB 从 INT8 小画布起步，24/32 GB 可保留更多组件；双 5090 同时落实 384 GiB 级主机与 PCIe 拓扑。
4. **再核对云端条件**：型号、每卡显存、主机内存、互联、库存、权限和整机价格。历史价格与实际资源筛选见[前期调研](research-and-selection.md#4-gpu-费用怎样纳入判断)，按 2026-10-20 的补查口径保留。

本项目最终落在可获得的 160 vCPU / 768 GiB CPU ECS，保留原始权重并完成单图 Ref2VA、15 秒人物视频和异步 API。安装沿用 [CPU 部署指南](deployment.md)。迁移 GPU 时，应先采用上游 SGLang/Diffusers 的相应后端，再将任务提交、查询和结果下载接到服务层；现有 CPU runner 的环境、设备选择与算子配置属于 CPU 实现。

[diffusers]: https://github.com/huggingface/diffusers/blob/b800f828f3951c4d206e154411bcbfa693cd322e/docs/source/en/api/pipelines/minimax_h3.md#memory
[sglang]: https://github.com/sgl-project/sglang/blob/d4e3c163c95afe5f549116c9c73a6f0e6a4830a7/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx
[a100]: https://www.nvidia.com/en-us/data-center/a100/
[h100]: https://www.nvidia.com/en-us/data-center/h100/
[h100-pcie]: https://www.nvidia.com/content/dam/en-zz/Solutions/gtcs22/data-center/h100/PB-11133-001_v01.pdf
[h200]: https://www.nvidia.com/en-us/data-center/h200/
[l40s]: https://www.nvidia.com/en-us/data-center/l40s/
[rtx5060]: https://www.nvidia.com/en-eu/geforce/graphics-cards/50-series/rtx-5060-family/
[rtx5060-bandwidth]: https://www.nvidia.com/es-la/geforce/graphics-cards/50-series/rtx-5060-family/
[rtx4090]: https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4090/
[rtx5090]: https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/
[v100]: https://images.nvidia.com/content/technologies/volta/pdf/volta-v100-datasheet-update-us-1165301-r5.pdf
[t4]: https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/tesla-t4/t4-tensor-core-datasheet.pdf
[bf16]: https://docs.nvidia.com/cuda/archive/12.3.0/ampere-tuning-guide/index.html#improved-tensor-core-operations
[blackwell]: https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf
[ada]: https://images.nvidia.com/aem-dam/Solutions/Data-Center/l4/nvidia-ada-gpu-architecture-whitepaper-V2.02.pdf
[volc-gpu]: https://www.volcengine.com/docs/6396/100717?lang=zh
