# H3 图文短视频流水线

本地单机 Web 应用：商品图 + 卖点 → M3 生成分镜 → 人工确认 →
H3 逐镜生成 → 技术检查 + 可选 M3 看片 → 分镜级人工处理 / 续跑 → ffmpeg 合成成片。
H3 支持 `mock`、`local`、`official`，M3 支持 `mock`、`official`；默认双 mock。
Web 服务只监听 `127.0.0.1`，配置和素材保存在本机；连接真实 H3/M3 provider 时，
会把必要文本和图片/视频发送到所配置的 API。本地存储不代表离线运行。

本地 H3 仅支持 `MiniMax-H3`、`768P`、单张参考图 `reference`、每镜 4–15 秒。
切换 provider 不会转换已有分镜：首帧、首尾帧或文生镜头须手动改为单参考图并通过规则校验。
本发布目录的本地适配不等于已完成真实模型端到端联调；请先按下面的步骤验证。

## 现在能做什么

**M4：质检 + 合成出片**
- 每镜下载后先跑**技术检查**（ffmpeg，不花钱）：黑场（blackdetect）、冻帧（freezedetect）、
  静音（silencedetect，仅脚本要求画内口播时）、成片时长/分辨率/画幅
- 技术检查通过再让 **M3 看片**：本实现把成片压成短边 384、4fps、无音轨的代理视频，
  按官网 `video_url` 传 `data:video/mp4;base64,...` + `fps=2` + `detail=low`，
  用 `submit_take_review` 工具返回「通过 / 不通过 / 拿不准」和具体问题、重试建议
- 判定：`mock` / `official` 下，技术/M3 任一判 blocker 会在**自动重试上限和项目预算内新建一条 Take**；
  `local` 下不自动重新生成，失败交人工处理。M3 拿不准或调用失败（鉴权/余额等不可重试错误）也转人工；
  设置里可关掉 M3 看片只做技术检查
- 人工兜底：看板上「人工采纳」（误判也能用，并取消还没提交的重试）、「重新质检」（不重生成视频）
- 自动重试计数按「连续自动重试」算，人工重试后重新计数；预算护栏只拦自动重试，首次确认和人工重试不拦
- **合成出片**：逐镜归一化到项目画幅（补黑边不拉伸、24fps、时长精确到帧），
  Pillow 渲染字幕 PNG 再 overlay（不依赖 libass/drawtext，Mac 自带 ffmpeg 可跑）；
  硬切或 0.3s 淡入淡出（xfade/acrossfade）；项目可上传 BGM，循环铺满 + 音量调节 + 首尾淡出，
  与原声混音带限幅防爆音；输出 H.264+AAC、faststart，可在线播放和下载
- 合成不花 H3/M3 的钱，可反复换转场/字幕/BGM 重新合成；重启时中断的质检和合成会自动续跑

**M3：多镜头批量 + 改稿只重跑变化的镜头**
- 确认分镜时按镜头算**内容指纹**（画面/动作/时长/模式/图片/模型/档位/画幅/风格），逐镜决定新建还是复用
- 已确认的分镜可「修改分镜（开新版本）」：复制成可编辑新草稿，旧版本置为 superseded，旧成片全部保留
- 二次确认：只有指纹变了或新增的镜头才新建 H3 任务；指纹没变且已有 accepted 成片的镜头直接「沿用上版」
- 被删镜头不再出现在新版本，但历史 take 保留；失败/未成功（非 accepted）的镜头不会被误复用
- 自动重试成功后，镜头当前选用自动跟随到新 take（跨分镜版本也生效）
- 项目预算 = 目标时长 × 3，按平台已返回的输出秒数统计（重试花掉的也算）

**M2：写脚本 + 确认闸门**
- 新建项目：商品名、画幅、目标时长（15–60 秒）、卖点、风格、违禁词
- 上传商品图：自动生成目标画幅的两个首帧候选（居中裁切 / 模糊补边，短边 768、对齐 32）
- M3 看商品图，产出「商品理解卡 + 分镜」，要求每条卖点都被镜头覆盖
- 分镜编辑器：理解卡可改（后续重写以它为准）、镜头逐个编辑、让 M3 重写整份或单个镜头
- 两道脚本质检：确定性规则（时长 4–15、总时长 ±1、图片数量与互斥、提示词 7000 字、口播 4 字/秒、
  卖点覆盖、极限词提示）+ M3 独立评审
- **确认前不调用任何 H3**；阻断项不清零，确认按钮不可点
- 确认后每个镜头按内容指纹创建一条 H3 take，自动进入 M1 队列

**M1：单镜 H3 生成链路**
- 官网 V2 与本地适配：`POST /v2/video_generation`、任务查询、`DELETE` 取消、结果下载
- Shot/Take 分离、SQLite 先写后调、重启续跑；本地提交按 Take 使用稳定的 `Idempotency-Key`
- 400/422 待修改、401/402 整队暂停、可重试请求退避（30/60/120 秒）；
  `local` 的生成失败和质检失败不自动新建 Take，官网未知提交交人工处理，详见下文
- 假 H3（mock）模拟成功、429、422、402、创建超时、平台失败、排队取消、首帧黑场，用 ffmpeg 造真 mp4
- 假 M3（mock）模拟看片通过 / 首次不通过 / 总不通过 / 拿不准 / 首次报错，不花钱打通全链路

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
│   │   ├── h3_official.py      # 官网 H3 V2 客户端
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

## 启动双 mock 流水线

从仓库根目录执行。流水线使用独立 Python 3.12 环境，与模型推理环境分开；
按已提供的锁文件安装，不重新解析 `requirements.txt` 或生成新的依赖版本。

```bash
cd pipeline/backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements.lock
cp .env.example .env            # 默认 H3/M3 都是 mock，不花钱
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

复制模板前请确认这是新实例目录；已有实例应编辑自己的配置文件。这里显式用 Web 端口 `8100`，
不连接其他目录的流水线进程。另需本机有 `ffmpeg`/`ffprobe`（技术检查和合成都用它）。
打开 [http://127.0.0.1:8100](http://127.0.0.1:8100)。双 mock 可以检查操作流程，但不能验证真实 H3/M3 的质量。

可选：从 `pipeline/backend` 安装已有测试锁文件并运行离线回归测试：

```bash
uv pip install --python .venv/bin/python --no-deps -r requirements-dev.lock
.venv/bin/python -m pytest -q
node --test tests/test_web_security.cjs
```

## 接本地 H3

先按 [模型部署说明](../docs/deployment.md) 在独立环境启动本地 H3 API，
再为一个新的流水线实例使用 `backend/.env.local.example`：

```bash
cd pipeline/backend
cp .env.local.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

此模板使用 `H3_PROVIDER=local`、`M3_PROVIDER=mock`、`H3_BASE_URL=http://127.0.0.1:30010`，
`MAX_CONCURRENCY=1`、`AUTO_RETRY_LIMIT=0`、`M3_TAKE_REVIEW=false`。
复制模板不会下载权重或启动模型，真实 H3 生成会占用本地推理资源。
`H3_API_KEY` 必须显式填写与本地服务一致的专用 Key，至少 24 位无空白 ASCII 字符。
M3 保持 mock 时不需要官网凭证；需要真实分镜或看片时，再单独配置 M3 official。

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

## 独立 Key 与官网配置

在「设置」页或 `.env` 中分别选择 provider 和凭证：

| 服务 | 独立配置 | 兼容回退 |
| --- | --- | --- |
| H3 official | `H3_API_KEY` / 设置字段 `h3_api_key` | 独立 Key 为空时可使用旧 `MINIMAX_API_KEY` / `api_key` |
| H3 local | `H3_API_KEY` / 设置字段 `h3_api_key` | 不读取共享官网 Key，也不使用 M3 Key |
| M3 official | `M3_API_KEY` / 设置字段 `m3_api_key` | 独立 Key 为空时可使用旧 `MINIMAX_API_KEY` / `api_key` |
| H3/M3 mock | 无需 Key | 不调用真实服务 |

独立 Key 优先于旧共享 Key。使用同一个官网 Key 时可分别填到 H3/M3，旧共享字段仅为兼容既有官方配置。
设置接口只返回 `h3_api_key_set` / `m3_api_key_set` 等是否已配置标记；页面使用密码输入框，
不回显 Key，空输入不提交也不修改原值，保存成功后清空输入框。

接官网时将对应 provider 设为 `official`，再填写该服务的独立 Key 与 Base URL：

- M3：`POST {M3_BASE_URL}/chat/completions`，`model=MiniMax-M3`
  - 分镜/看片都用 tools 拿结构化输出（M3 没有 response_format 保证，校验失败会回传让模型修复）
  - 看片用多模态 `video_url`：MP4/AVI/MOV/MKV，URL 或 base64 ≤50MB，`fps` 0.2–5（默认 1，本项目用 2），
    `detail` low/default/high；本实现提交的看片代理不含音轨，声音问题交给本地技术检查和人工复核
  - 业务错误可能以 HTTP 200 + `base_resp.status_code` 返回（1004 鉴权 / 1008 余额 / 2013 参数等）
- H3：`{H3_BASE_URL}/v2/video_generation` 等；base URL 以控制台为准（文档里出现过 `api.minimax.cn` 和 `api.minimaxi.com`）

官网提交超时且没有可靠任务 ID 时，不能保证重新发送不会创建第二个收费任务，因此转人工检查；
先到官网核实是否已创建任务，再在看板点「绑定平台任务」填入准确任务 ID，恢复后续查询与处理。
绑定只查询该任务，不按时间猜测匹配，也不新建生成任务。不要把未知提交当成明确失败而直接重复生成。

## 质检阈值与注意事项

`app/constants.py` 里的黑场/冻帧/静音阈值是初始参数（黑场单段 ≥1s 或合计 ≥25%、冻帧 ≥50%、
要求口播但静音 ≥80%、成片短于请求 0.6s 等）。真实 H3 出片后建议先人工抽看，按误判情况再调。
已知限制：
- 本实现发给 M3 的看片代理仅保留画面；声音语义和声画同步仍需人工复核，静音可由技术检查辅助发现。
  代理视频是压缩版，极细微瑕疵可能漏检
- 字体：macOS 优先用苹方，Linux 用 Noto Sans CJK；都没有时跳过字幕并在合成结果里提示，
  可设 `SUBTITLE_FONT` 环境变量指定字体文件

## 里程碑

- [x] M1 H3 单镜：创建 / 轮询 / 下载 / 重试 / 续跑 / 对账
- [x] M2 脚本：M3 理解卡 + 分镜、规则 + 评审、编辑器、确认闸门
- [x] M3 多镜头批量、改稿只重跑指纹变化的镜头、项目维度镜头看板
- [x] M4 黑屏/冻帧/静音技术检查、M3 看片打分、分镜级重试、ffmpeg 合成（字幕/转场/BGM）
- [ ] P1 768P→2K 再生成、画外音 TTS、mm_file 大文件复用
