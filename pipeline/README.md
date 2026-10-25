# H3 图文短视频流水线

以**自己部署的 H3** 为生成引擎的单用户 Web 工作台：参考图 + 创作要求 → M3 生成分镜 → 人工确认 →
自部署 H3 逐镜生成 → 技术检查 + 可选 M3 看片 → 分镜级人工处理 / 续跑 → FFmpeg 合成成片。
工作台配置 `H3_PROVIDER=local`、`M3_PROVIDER=official`，H3 在大内存 CPU 主机上推理，M3 使用官方 API。
Web 服务只监听 `127.0.0.1`，配置和素材保存在本机；H3 请求发送到自己的服务，
M3 所需文本和图片/视频发送到配置的 M3 API。

本地 H3 仅支持 `MiniMax-H3`、`768P`、单张参考图 `reference`、每镜 4–15 秒。
切换 provider 不会转换已有分镜：首帧、首尾帧或文生镜头须手动改为单参考图并通过规则校验。

## 功能

**质检与合成出片**

- 每镜下载后先跑**技术检查**（本机 FFmpeg）：黑场（blackdetect）、冻帧（freezedetect）、
  静音（silencedetect，仅脚本要求画内口播时）、成片时长/分辨率/画幅
- 技术检查通过再让 **M3 看片**：本实现把成片压成短边 384、4fps、无音轨的代理视频，
  按官网 `video_url` 传 `data:video/mp4;base64,...` + `fps=2` + `detail=low`，
  用 `submit_take_review` 工具返回「通过 / 不通过 / 拿不准」和具体问题、重试建议
- 判定：自部署 H3 的生成失败或质检失败交人工处理，不自动重新生成。M3 拿不准或调用失败（鉴权/余额等不可重试错误）也转人工；
  设置里可关掉 M3 看片只做技术检查
- 人工兜底：看板上「人工采纳」（误判也能用，并取消还没提交的重试）、「重新质检」（不重生成视频）
- 人工重试会创建新的 Take 并重新计算；项目输出秒数预算不阻止首次确认和人工重试
- **合成出片**：逐镜归一化到项目画幅（补黑边不拉伸、24fps、时长精确到帧），
  Pillow 渲染字幕 PNG 再 overlay（不依赖 libass/drawtext，Mac 自带 ffmpeg 可跑）；
  硬切或 0.3s 淡入淡出（xfade/acrossfade）；项目可上传 BGM，循环铺满 + 音量调节 + 首尾淡出，
  与原声混音带限幅防爆音；输出 H.264+AAC、faststart，可在线播放和下载
- 合成不花 H3/M3 的钱，可反复换转场/字幕/BGM 重新合成；重启时中断的质检和合成会自动续跑

**多镜头批量与按改稿重跑**

- 确认分镜时按镜头算**内容指纹**（画面/动作/时长/模式/图片/模型/档位/画幅/风格），逐镜决定新建还是复用
- 已确认的分镜可「修改分镜（开新版本）」：复制成可编辑新草稿，旧版本置为 superseded，旧成片全部保留
- 二次确认：只有指纹变了或新增的镜头才新建 H3 任务；指纹没变且已有 accepted 成片的镜头直接「沿用上版」
- 被删镜头不再出现在新版本，但历史 take 保留；失败/未成功（非 accepted）的镜头不会被误复用
- 项目预算 = 目标时长 × 3，按平台已返回的输出秒数统计（重试花掉的也算）

**分镜编写与人工确认**

- 新建项目：商品名、画幅、目标时长（15–60 秒）、卖点、风格、违禁词
- 上传商品图：自动生成目标画幅的两个首帧候选（居中裁切 / 模糊补边，短边 768、对齐 32）
- M3 看商品图，产出「商品理解卡 + 分镜」，要求每条卖点都被镜头覆盖
- 分镜编辑器：理解卡可改（后续重写以它为准）、镜头逐个编辑、让 M3 重写整份或单个镜头
- 两道脚本质检：确定性规则（时长 4–15、总时长 ±1、图片数量与互斥、提示词 7000 字、口播 4 字/秒、
  卖点覆盖、极限词提示）+ M3 独立评审
- **确认前不调用任何 H3**；阻断项不清零，确认按钮不可点
- 确认后每个镜头按内容指纹创建一条 H3 Take，自动进入生成队列

**自部署 H3 单镜生成**

- MiniMax V2 风格接口：`POST /v2/video_generation`、任务查询、`DELETE` 取消排队任务、结果下载
- Shot/Take 分离、SQLite 先写后调、重启续跑；本地提交按 Take 使用稳定的 `Idempotency-Key`
- 400/422 待修改、401/402 整队暂停、可重试请求退避（30/60/120 秒）；
  `local` 的生成失败和质检失败不自动新建 Take；提交超时按原幂等键恢复，详见下文

## 目录

```
backend/
├── app/
│   ├── main.py                 # FastAPI 入口，托管 web/
│   ├── config.py / constants.py
│   ├── db.py                   # projects/assets/storyboards/shots/shot_takes/takes/renders/events
│   ├── media.py / image_ops.py # ffprobe、data URI、画幅裁切/补边
│   ├── qc.py                   # 技术检查：黑场/冻帧/静音/时长
│   ├── review_inputs.py        # M3 看片输入：代理视频（384/4fps/无音轨）+ 参考图 + 分镜要求
│   ├── compose.py              # 合成：归一化/字幕/转场/BGM
│   ├── storyboard_schema.py    # 分镜模型、规则引擎、H3 请求拼装、内容指纹、看片结论模型
│   ├── providers/
│   │   ├── h3_official.py      # V2 协议客户端，由本地 provider 复用
│   │   ├── h3_local.py         # 本地 H3：单参考图、稳定提交幂等键
│   │   ├── h3_mock.py          # 假 H3
│   │   ├── m3_official.py      # 官网 M3 Chat Completions（分镜/评审/看片 video_url+tools）
│   │   └── m3_mock.py          # 假 M3
│   ├── pipeline/
│   │   ├── runner.py           # 提交/轮询/下载/质检/退避/重试/对账/续跑/后台合成
│   │   ├── storyboard_service.py
│   │   └── render_service.py   # 合成计划、排队执行、成片校验、BGM 上传
│   └── api/routes.py
├── web/index.html              # 零构建单页：项目 / 分镜 / 生成看板 / 合成出片 / 设置
└── tests/                      # 后端回归测试
```

## 接入自部署 H3 并启动

先按 [模型部署说明](../docs/deployment.md)启动自己的 H3 API 与 CPU worker。
流水线使用独立 Python 3.12 环境，与模型推理环境分开；本机需有 `ffmpeg`/`ffprobe`。
以下从仓库根目录执行：

```bash
cd pipeline/backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.local.example .env
```

已有实例直接编辑自己的 `.env`。新复制的模板将 M3 设为 `mock`；连接模型服务时编辑以下配置，
将 M3 改为 `official`，并填入两个服务各自的密钥：

```dotenv
H3_PROVIDER=local
H3_BASE_URL=http://127.0.0.1:30010
H3_API_KEY=
M3_PROVIDER=official
M3_BASE_URL=https://api.minimax.cn/v1
M3_API_KEY=
M3_TAKE_REVIEW=true
MAX_CONCURRENCY=1
AUTO_RETRY_LIMIT=0
HOST=127.0.0.1
PORT=8100
```

`H3_API_KEY` 必须与自部署 H3 服务一致，至少 24 位无空白 ASCII 字符。
`M3_API_KEY` 为官方 M3 密钥，M3 地址和模型权限以自己的控制台为准。
`M3_TAKE_REVIEW=true` 开启看片；改为 `false` 时只做技术检查。
H3 在另一台主机上时，按 [接入指南](../docs/integration.md)建立 SSH 隧道，并配置可达的下载地址。

在当前 `pipeline/backend` 目录启动工作台：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

打开 [http://127.0.0.1:8100](http://127.0.0.1:8100)，创建项目、上传参考图、生成并确认分镜，再到生成看板查看各镜头。
每镜使用 `reference` 模式、一张参考图、`MiniMax-H3`、`768P` 和 4–15 秒整数时长。

## 长任务恢复与人工处理

已有分镜不会在切换 provider 时改写。请在编辑器里将需要生成的镜头明确设为 `reference`，
只填一个参考图 `asset_id`，清空首帧/尾帧字段，确保 `MiniMax-H3`、`768P`、4–15 秒并通过规则校验后再确认。
有活动任务时，后端拒绝切换 H3 provider 或服务源地址（origin）；先处理任务，再修改配置。

本地运行的恢复和人工处理规则：

- 每条 Take 的本地提交使用稳定的 `Idempotency-Key`。提交超时后的同一 Take 重送沿用该键，
  用于找回已有任务；人工「重试」会创建新的 Take，属于一次新的生成。
- 本地生成失败、技术检查不通过或 M3 判失败都不会自动新建生成 Take，即使把自动重试次数调大也一样。
  先在看板检查错误和已有视频，再决定人工重试、人工采纳或重新质检。
- 下载链接失效时，流水线重新查询原任务以刷新下载 URL；这一步不重新提交视频生成。
- `DELETE` 取消必须以服务端结果为准。若请求与任务完成的终态竞争，或结果无法确认，任务进入人工检查
  `review`，不会仅凭发出取消请求就显示 `cancelled`。请查看该 Take 和事件日志后决定如何处理。

## 服务接口与独立密钥

两个服务分别配置地址和密钥：

| 服务 | 配置 | 请求地址 |
| --- | --- | --- |
| 自部署 H3 | `H3_PROVIDER=local`、`H3_API_KEY` | `{H3_BASE_URL}/v2/video_generation` |
| 官方 M3 | `M3_PROVIDER=official`、`M3_API_KEY` | `{M3_BASE_URL}/chat/completions` |

自部署 H3 只读取自己的专用密钥，不读取 M3 Key 或旧共享 `MINIMAX_API_KEY`。
设置接口只返回 `h3_api_key_set` / `m3_api_key_set` 等是否已配置标记；页面使用密码输入框，
不回显 Key，空输入不提交也不修改原值，保存成功后清空输入框。

M3 调用使用 `model=MiniMax-M3`：

- 分镜/看片都用 tools 获取结构化输出，校验失败会回传让模型修复。
- 看片用多模态 `video_url`，本项目设置 `fps=2`、`detail=low`；发送的代理视频不含音轨，声音问题交给本地技术检查和人工复核。
- 业务错误可能以 HTTP 200 + `base_resp.status_code` 返回，客户端据此处理鉴权、余额和参数错误。

H3 请求参数、画布与状态语义见 [H3 API 说明](../h3-api/README.md)。

## 质检阈值与注意事项

`app/constants.py` 里的黑场/冻帧/静音阈值是初始参数（黑场单段 ≥1s 或合计 ≥25%、冻帧 ≥50%、
要求口播但静音 ≥80%、成片短于请求 0.6s 等）。真实 H3 出片后建议先人工抽看，按误判情况再调。
已知限制：

- 本实现发给 M3 的看片代理仅保留画面；声音语义和声画同步仍需人工复核，静音可由技术检查辅助发现。
  代理视频是压缩版，极细微瑕疵可能漏检
- 字体：macOS 优先用苹方，Linux 用 Noto Sans CJK；都没有时跳过字幕并在合成结果里提示，
  可设 `SUBTITLE_FONT` 环境变量指定字体文件

## 开发与测试

从 `pipeline/backend` 安装测试依赖并运行回归检查：

```bash
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pytest -q
node --test tests/test_web_security.cjs
```

自动化检查使用测试替身和合成媒体，覆盖队列、异常处理、质检、合成与页面安全，不触发模型推理。
更多测试命令及生成样例见 [验证指南](../docs/validation.md)。
