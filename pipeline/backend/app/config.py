"""运行配置：环境变量 + 数据目录。

H3 支持本地或官网 V2；M3 支持官网 OpenAI 兼容 Chat Completions。
两者默认都是 mock（不花钱），各有独立 Key；官网保留旧 Key 回退。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parent.parent
_DNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z", re.ASCII)
_OFFICIAL_H3_HOSTS = {'api.minimax.cn', 'api.minimaxi.com', 'api.minimax.io',
                      'api.minimax.chat', 'api.minimaxi.chat'}


def is_official_h3_origin(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or '').rstrip('.').lower() in _OFFICIAL_H3_HOSTS
    except ValueError:
        return False


def _load_dotenv() -> None:
    """极简 .env 加载，不引入额外依赖。已存在的环境变量优先。"""
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
DATA_DIR = Path(os.environ.get("PIPELINE_DATA_DIR", BACKEND_DIR / "data"))
TAKES_DIR = DATA_DIR / "takes"
ASSETS_DIR = DATA_DIR / "assets"
RENDERS_DIR = DATA_DIR / "renders"
DB_PATH = DATA_DIR / "app.db"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("MINIMAX_API_KEY", ""))
    h3_api_key: str = field(default_factory=lambda: os.environ.get("H3_API_KEY", ""))
    m3_api_key: str = field(default_factory=lambda: os.environ.get("M3_API_KEY", ""))
    m3_base_url: str = field(default_factory=lambda: os.environ.get("M3_BASE_URL", "https://api.minimax.cn/v1").rstrip("/"))
    h3_base_url: str = field(default_factory=lambda: os.environ.get("H3_BASE_URL", "").rstrip("/"))
    provider: str = field(default_factory=lambda: os.environ.get("H3_PROVIDER", "mock").lower())
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    max_concurrency: int = field(default_factory=lambda: _int("MAX_CONCURRENCY", 2))
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 10))
    auto_retry_limit: int = field(default_factory=lambda: _int("AUTO_RETRY_LIMIT", 1))
    # 无项目的单镜调试任务的输出秒数预算；项目任务按「目标时长 × 3」。
    budget_output_seconds: int = field(default_factory=lambda: _int("BUDGET_OUTPUT_SECONDS", 90))
    # mock 场景，见 providers/h3_mock.py。可在设置接口里改。
    mock_scenario: str = field(default_factory=lambda: os.environ.get("MOCK_SCENARIO", "success"))
    # M3 默认也走 mock；official 优先 M3_API_KEY，再兼容旧 Key。
    m3_provider: str = field(default_factory=lambda: os.environ.get("M3_PROVIDER", "mock").lower())
    # 对外可访问的本机地址，mock 用它拼下载 URL。
    public_base_url: str = ""
    # ---- M4：M3 看片质检 ----
    # 关闭后只做技术检查（不花 M3 token），技术检查通过即采纳。
    m3_take_review: bool = field(default_factory=lambda: _bool("M3_TAKE_REVIEW", True))
    # 官网 video_url.fps：0.2-5，默认 1；看片默认 2 帧/秒
    m3_review_fps: float = field(default_factory=lambda: _float("M3_REVIEW_FPS", 2.0))
    # 官网 video_url.detail：low/default/high；low 最省 token
    m3_review_detail: str = field(default_factory=lambda: os.environ.get("M3_REVIEW_DETAIL", "low"))
    # 假 M3 看片结论：pass / fail_once / fail / uncertain（只在 M3_PROVIDER=mock 时生效）
    m3_mock_review: str = field(default_factory=lambda: os.environ.get("M3_MOCK_REVIEW", "pass"))

    def __post_init__(self) -> None:
        if not self.h3_base_url:
            self.h3_base_url = ("http://127.0.0.1:30010" if self.provider == "local"
                                else "https://api.minimax.cn")

    @property
    def h3_key(self) -> str:
        return self.h3_api_key or (self.api_key if self.provider == "official" else "")

    @property
    def m3_key(self) -> str:
        return self.m3_api_key or (self.api_key if self.m3_provider == "official" else "")

    @property
    def h3_origin(self) -> str:
        """Canonical origin used to keep tasks from different H3 services separate."""
        try:
            value = self.h3_base_url
            if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
                raise ValueError
            url = urlsplit(value)
            port = url.port
            if (url.scheme not in ("http", "https") or not url.hostname
                    or url.username is not None or url.password is not None
                    or url.query or url.fragment or url.path not in ("", "/")
                    or "%" in url.hostname or url.netloc.endswith(":")
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
            host = url.hostname.lower()
            if ":" in host:
                from ipaddress import IPv6Address
                host = f"[{IPv6Address(host).compressed}]"
            else:
                host = host.removesuffix(".").encode("idna").decode("ascii")
                if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
                    raise ValueError
            if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
                host += f":{port}"
            return f"{url.scheme}://{host}"
        except (ValueError, UnicodeError):
            raise ValueError("H3_BASE_URL 必须是无账号、路径或查询参数的 HTTP(S) origin") from None

    def public_dict(self) -> dict:
        """返回给前端的配置：只公开 Key 是否可用，不返回任何 Key 明文。"""
        d = asdict(self)
        d["api_key_set"] = bool(self.api_key)
        d["h3_api_key_set"] = bool(self.h3_key)
        d["m3_api_key_set"] = bool(self.m3_key)
        for key in ("api_key", "h3_api_key", "m3_api_key"):
            d.pop(key, None)
        return d

    def validate_provider(self) -> str | None:
        if self.provider not in ("official", "mock", "local"):
            return f"未知 H3 provider: {self.provider}"
        if self.m3_provider not in ("official", "mock"):
            return f"未知 M3 provider: {self.m3_provider}"
        if self.provider == "local" and (not isinstance(self.h3_api_key, str)
                or len(self.h3_api_key) < 24 or not self.h3_api_key.isascii()
                or any(c.isspace() for c in self.h3_api_key)):
            return "H3 provider=local 必须显式配置至少 24 位、无空白的 ASCII H3_API_KEY"
        if self.provider == "official" and not self.h3_key:
            return "H3 provider=official 但未配置 H3_API_KEY（或兼容 MINIMAX_API_KEY）"
        if self.m3_provider == "official" and not self.m3_key:
            return "M3 provider=official 但未配置 M3_API_KEY（或兼容 MINIMAX_API_KEY）"
        try:
            self.h3_origin
        except ValueError as error:
            return str(error)
        if self.provider == 'local' and is_official_h3_origin(self.h3_base_url):
            return 'local 模式不能使用官方 H3 地址；请填写自部署服务地址，官方服务使用 official 模式'
        if not (0.2 <= float(self.m3_review_fps) <= 5):
            return "M3 看片 fps 必须在 0.2-5 之间（官网 video_url.fps 取值范围）"
        if self.m3_review_detail not in ("low", "default", "high"):
            return "M3 看片 detail 只能是 low / default / high"
        return None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TAKES_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
