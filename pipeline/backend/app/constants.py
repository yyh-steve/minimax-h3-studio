"""Take 状态与错误分类常量。

状态命名尽量与官网任务状态对齐（queued/running/succeeded/failed/cancelled），
本地额外的状态描述提交、下载、质检和人工介入阶段。
"""
from __future__ import annotations

# ---- Take 主状态 ----
PENDING = "pending"            # 还没调用创建接口
SUBMITTING = "submitting"      # 正在调用创建接口（先落库再请求）
UNKNOWN = "unknown"            # 创建请求超时，不知道平台是否建成任务，等待对账
QUEUED = "queued"              # 平台排队中
RUNNING = "running"            # 平台生成中
SUCCEEDED = "succeeded"        # 平台已成功（本地可能还没下载）
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"      # 已落盘且 ffprobe 通过，等待质检
CHECKING = "checking"          # 技术检查 / M3 看片进行中
ACCEPTED = "accepted"          # 质检通过或人工采纳，可以进入合成
REJECTED = "rejected"          # 本轮不通过，触发新建一条 Take
REVIEW = "review"              # 需要人工决定
NEEDS_EDIT = "needs_edit"      # 400/422 等，需要改分镜/提示词
PAUSED = "paused"              # 401/402 等，整个队列暂停
BACKOFF = "backoff"            # 429/5xx 退避等待
CANCELLED = "cancelled"
CANCELLING = "cancelling"      # 取消请求在途，旧轮询结果不能再推进此任务
EXPIRED = "expired"            # 平台只保留 7 天，任务已查不到

# 可被 worker 自动拾取继续推进的状态
ACTIVE_STATES = {PENDING, SUBMITTING, CANCELLING, QUEUED, RUNNING, SUCCEEDED, DOWNLOADING, DOWNLOADED,
                 CHECKING, BACKOFF, UNKNOWN}
# 终态（除非人工操作，不再变化）
TERMINAL_STATES = {REVIEW, NEEDS_EDIT, PAUSED, CANCELLED, EXPIRED}

# ---- 官网错误码（见 V2 创建/查询文档）----
CODE_BAD_REQUEST = 2013
CODE_AUTH = 1004
CODE_BALANCE = 1008
CODE_SENSITIVE = 1026
CODE_RATE_LIMIT = 1002
CODE_SERVER = 1000

# 退避计划（秒）。429/5xx 使用，超过次数后转人工。
BACKOFF_SCHEDULE_SECONDS = (30, 60, 120)

# 平台任务只保留最近 7 天
PLATFORM_TTL_SECONDS = 7 * 24 * 3600

# 轮询官方建议间隔
OFFICIAL_POLL_INTERVAL_SECONDS = 10

# ---- M4 技术质检阈值（ffmpeg blackdetect / freezedetect / silencedetect）----
# 都是可调整的初始参数：先拦住明显坏片，真实出片后按误判情况再校准。
QC_BLACK_MIN_SECONDS = 0.5          # blackdetect d：短于它的黑场忽略
QC_BLACK_PIX_TH = 0.10              # 像素亮度低于 10% 视为黑
QC_BLACK_BLOCK_SECONDS = 1.0        # 单段黑场 ≥1s 判不通过
QC_BLACK_BLOCK_RATIO = 0.25         # 黑场合计占比 ≥25% 判不通过
QC_FREEZE_NOISE_DB = -60            # 帧间差异低于 -60dB 视为同一帧
QC_FREEZE_MIN_SECONDS = 2.0         # 冻帧至少持续 2s 才记录
QC_FREEZE_BLOCK_RATIO = 0.5         # 冻帧占比 ≥50%（基本是一张图）判不通过
QC_SILENCE_NOISE_DB = -50
QC_SILENCE_MIN_SECONDS = 1.0
QC_VOICE_SILENCE_BLOCK_RATIO = 0.8  # 脚本要求画内口播但 ≥80% 静音，判不通过
QC_DURATION_SHORT_TOLERANCE = 0.6   # 比请求时长短 0.6s 以上判不通过
QC_DURATION_LONG_TOLERANCE = 1.0    # 长 1s 以上只提示
QC_RATIO_TOLERANCE = 0.03           # 画幅偏差 3% 以内视为一致

# ---- M4 M3 看片 ----
REVIEW_PROXY_SHORT_EDGE = 384       # 代理视频短边；只给 M3 看，不进成片
REVIEW_PROXY_FPS = 4                # 代理视频帧率（M3 再按 video_url.fps 抽帧）
REVIEW_REF_IMAGE_LONG_EDGE = 1024   # 送给 M3 的参考图长边上限
REVIEW_MAX_REF_IMAGES = 3
M3_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
M3_VIDEO_MAX_BYTES = 50 * 1024 * 1024   # 官网：URL/base64 视频 ≤50MB
M3_REVIEW_BACKOFF_SECONDS = (5, 20)     # M3 看片调用失败后的等待；用完转人工
MAX_PARALLEL_CHECKS = 2

# ---- M4 合成 ----
RENDER_FPS = 24
RENDER_PRESET = "veryfast"
RENDER_CRF = 20
RENDER_AUDIO_RATE = 48000
RENDER_DURATION_TOLERANCE = 0.25    # 成片时长与预期偏差上限（秒）
RENDER_FADE_SECONDS = 0.3           # 淡入淡出转场时长
RENDER_MAX_PARALLEL = 1
