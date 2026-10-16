"""AstrBot 插件：AI 图片检测（OpenAI / ChatGPT 水印）。

触发方式（两种）：

1. 用户在**同一条消息**里发送图片，并且 @机器人 + 配文「ai吗」（或「AI吗」）。
2. 用户**引用**一条图片消息，并且在自己的消息里 @机器人 + 配文「ai吗」（或「AI吗」）。

实现方式：把待检测图片上传到 OpenAI 的 Content Provenance API
（``POST /v1/content_provenance_checks``），根据返回的 ``c2pa`` / ``synthid``
结果判断图片是否携带 OpenAI 系（ChatGPT / DALL·E / Sora）的来源信号。

API 文档：https://developers.openai.com/api/docs/guides/content-provenance
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Image, Plain, Reply
from astrbot.api.star import Context, Star

try:  # pragma: no cover - openai 是可选依赖，缺失时走内置 HTTP 兜底
    from openai import AsyncOpenAI
    from openai import RateLimitError as _OpenAIRateLimitError
except ImportError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment]
    _OpenAIRateLimitError = None  # type: ignore[assignment]


DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_TRIGGER_KEYWORDS = ("ai吗",)
DEFAULT_MAX_IMAGES = 1
DEFAULT_TIMEOUT = 60
MAX_IMAGES_HARD_LIMIT = 5
#: 单张图片的字节上限（图片解析与 base64 解码都受此限制，防止内存被打爆）
MAX_IMAGE_BYTES = 50 * 1024 * 1024

#: 自定义回复文案的配置项名（检测到 / 未检测到）
CONFIG_REPLY_DETECTED = "reply_text_detected"
CONFIG_REPLY_NOT_DETECTED = "reply_text_not_detected"

#: 插件自己发出的请求支持的代理协议
#: （标准库只支持 http/https；socks5 需要额外依赖，插件不再引入依赖）
PROXY_SCHEMES = ("http", "https")
#: 显式直连的标记（既没配代理、也不沿用系统代理时使用）
DIRECT_MARKER = "__direct__"
#: 构造好的 opener 缓存，键是代理配置
_OPENER_CACHE: dict[str, urllib.request.OpenerDirector] = {}

#: 服务端 5xx 等瞬时故障的重试次数（含首次请求）
TRANSIENT_RETRY_ATTEMPTS = 3
#: 退避基数（秒），实际等待为 base * 2**(第几次重试) + 抖动
RETRY_BACKOFF_BASE = 1.5
#: 收到 429 但响应没有 Retry-After 时的默认冷却时长（秒）
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
#: 额度耗尽 / 服务端故障的冷却时长（秒）
QUOTA_COOLDOWN = 600.0
SERVER_ERROR_COOLDOWN = 20.0
#: 冷却时长上限（秒）
MAX_COOLDOWN = 1800.0
#: 冷却状态在插件重载后依然有效，避免热重载把限流保护清零
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_REASON = ""

#: 命中的来源信号 -> 模型名/签发方缺省时的占位描述
_SIGNAL_LABELS = {
    "c2pa": "C2PA 内容凭证",
    "synthid": "SynthID 水印",
}

#: 这些标记说明是「额度/计费」问题而不是「短时频率」问题
_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "quota",
    "credit",
)

_MSG_RATE_LIMITED = (
    "OpenAI 接口当前触发调用频率限制，暂时不可用（已自动停止重试），"
    "约 {wait}后再试。"
)
_MSG_QUOTA_EXHAUSTED = (
    "OpenAI 账号额度不足或计费异常，检测暂时不可用。"
    "请检查账号额度后重试。"
)
_MSG_UNAUTHORIZED = "OpenAI API Key 无效或不匹配，请检查插件配置里的 Key。"
_MSG_FORBIDDEN = "OpenAI 拒绝了本次请求（可能是 Key 权限不足），暂时无法检测。"
_MSG_SERVER_ERROR = "OpenAI 服务端暂时不可用（HTTP {code}），请稍后再试。"
_MSG_UNEXPECTED_STATUS = "OpenAI 接口返回异常状态 HTTP {code}，暂时无法检测。"

MESSAGE_RATE_LIMITED_COOLDOWN = "检测暂时不可用：接口限流冷却中，约 {wait}后自动恢复。"


MESSAGE_DETECTED = (
    "检测到 OpenAI 水印，图片是 AI 生成（命中：{signals}）\n"
    "来源模型/签发方：{models}"
)
MESSAGE_NOT_DETECTED = "未检测到 OpenAI 水印，无法判断"
#: 上面两条是默认回复文案（与 _conf_schema.json 的默认值一致），
#: 用户可在插件配置里改（见 reply_text_* 配置项）
MESSAGE_NO_IMAGE = "没有识别到图片。请把图片和「ai吗」发在同一条消息里，或者引用一条图片消息再发「ai吗」。"
MESSAGE_DOWNLOAD_FAILED = "图片读取失败，无法进行检测，请重新发送图片后再试。"
MESSAGE_IMAGE_TOO_LARGE = (
    f"图片过大（超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB），已跳过检测。"
    "请压缩后再发，或换一张更小的图片。"
)
MESSAGE_NO_API_KEY = (
    "插件未配置 OpenAI API Key，请在 AstrBot WebUI 的插件配置里填写。"
)
MESSAGE_PROXY_UNSUPPORTED = (
    "代理配置无效：仅支持以 http:// 或 https:// 开头的代理地址"
    "（例如 http://127.0.0.1:7890），不支持 socks5。"
    "请改用代理软件提供的 HTTP 端口，或把代理留空。"
)
MESSAGE_PROXY_INVALID = "代理配置无效，请检查格式，例如 http://127.0.0.1:7890。"

#: 把形如 sk-xxxx 的密钥从任何要落日志/回给用户的文本里抹掉
_KEY_SHAPED = re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_*\-]{8,}")

#: 带认证信息的 URL（例如代理 http://user:pass@host:port）里的密码部分
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][\w+.\-]*://)[^/@\s]+@")

#: 上传文件名只保留 \w . - （见 sanitize_filename）
_FILENAME_UNSAFE = re.compile(r"[^\w.\-]", re.UNICODE)
_FILENAME_MAX_LENGTH = 120

#: 只剩空括号的片段，例如自定义文案里 {signals} 没有内容时留下的「（命中：）」
_EMPTY_PARENS = re.compile(r"[（(]\s*[^）)]*[：:]\s*[）)]")


# --------------------------------------------------------------------------- #
# 限流冷却：模块级状态，插件热重载后依然有效（否则重载即可绕过限流保护）
# --------------------------------------------------------------------------- #
def _format_wait(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def cooldown_remaining() -> float:
    """返回当前限流冷却的剩余秒数（0 表示可用）。"""
    remaining = _COOLDOWN_UNTIL - time.monotonic()
    return remaining if remaining > 0 else 0.0


def start_cooldown(seconds: float, reason: str = "") -> float:
    """开启（或延长）限流冷却，返回生效的冷却秒数。"""
    global _COOLDOWN_UNTIL, _COOLDOWN_REASON
    seconds = max(0.0, min(float(seconds), MAX_COOLDOWN))
    if seconds <= 0:
        return 0.0
    until = time.monotonic() + seconds
    if until > _COOLDOWN_UNTIL:
        _COOLDOWN_UNTIL = until
        _COOLDOWN_REASON = reason
    return seconds


def clear_cooldown() -> None:
    global _COOLDOWN_UNTIL, _COOLDOWN_REASON
    _COOLDOWN_UNTIL = 0.0
    _COOLDOWN_REASON = ""


class ImageTooLargeError(Exception):
    """图片超过 :data:`MAX_IMAGE_BYTES`，不再读取/解码，避免占用大量内存。"""

    def __init__(self, size: int, limit: int = MAX_IMAGE_BYTES) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"图片大小 {size} 字节，超过上限 {limit} 字节")


def ensure_within_size_limit(size: int) -> None:
    """超出上限时抛出 :class:`ImageTooLargeError`。"""
    if size > MAX_IMAGE_BYTES:
        raise ImageTooLargeError(size)


def _parse_retry_after(headers) -> float:
    """解析 Retry-After 响应头，支持「秒数」与「HTTP 日期」两种写法。"""
    if headers is None:
        return 0.0
    try:
        raw = headers.get("Retry-After")
    except Exception:  # pragma: no cover - 罕见的 header 实现差异
        return 0.0
    if not raw:
        return 0.0
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            return 0.0
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _looks_like_rate_limit(exc: BaseException) -> bool:
    """判断异常是否属于「限流 / 额度受限」——这类情况绝不能重试。"""
    if _OpenAIRateLimitError is not None and isinstance(exc, _OpenAIRateLimitError):
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    text = f"{exc}".lower()
    return any(
        marker in text
        for marker in ("rate limit", "rate_limit", "429", *_QUOTA_MARKERS)
    )


def _safe_error_message(exc: urllib.error.HTTPError) -> str:
    """从错误响应体里取出 OpenAI 的错误描述（脱敏、压成一行、限长）。"""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - 读取失败时退化为空
        return ""
    # 先对整段原始报文脱敏：任何字段里出现密钥都不会被带出去
    raw = redact(raw)
    text = raw.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                parts = [
                    str(error.get(key))
                    for key in ("message", "type", "code")
                    if error.get(key)
                ]
                text = " / ".join(parts)
            elif isinstance(error, str):
                text = error
    except json.JSONDecodeError:
        pass
    text = redact(text)
    text = " ".join(text.split())
    return text[:200]


def sanitize_filename(name: str, default: str = "image.jpg") -> str:
    """把文件名限制在 ``\\w``、``.``、``-`` 白名单内。

    文件名会写进 multipart 的 ``Content-Disposition`` 头，来源是平台下发的
    消息段（不可信），因此必须转义：引号、换行、路径分隔符等都可能破坏
    表单结构甚至注入额外的头部字段。

    会尽量保留扩展名，例如 ``"a/b'\\"c.png"`` -> ``"ac.png"``。
    """
    raw = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    # 拆出扩展名并分别过滤，避免扩展名里的非法字符导致整个后缀被丢掉
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, ""
    stem = _FILENAME_UNSAFE.sub("", stem)
    ext = _FILENAME_UNSAFE.sub("", ext)
    safe = f"{stem}.{ext}" if stem and ext else (stem or ext)
    if not safe or safe in {".", ".."}:
        return default
    return safe[: _FILENAME_MAX_LENGTH]


def redact(text: object) -> str:
    """抹掉文本里的密钥与 URL 认证信息。

    代理地址常写成 ``http://user:password@host:port``，这类文本会经
    「网络错误：…」「代理配置无效：…」以及日志回给用户，必须先脱敏。
    """
    cleaned = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}***@", str(text))
    return _KEY_SHAPED.sub("sk-***", cleaned)


def validate_proxy(proxy: str) -> str:
    """校验代理地址，返回可用的代理 URL；格式非法时抛 :class:`ValueError`。

    只接受 http/https：标准库的 ProxyHandler 不支持 socks5，与其静默失败，
    不如给出可操作的提示。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme.lower() not in PROXY_SCHEMES or not parsed.hostname:
        raise ValueError(f"unsupported proxy: {redact(proxy)}")
    return proxy


def get_opener(proxy: str) -> urllib.request.OpenerDirector:
    """按代理配置返回（并缓存）一个 opener。

    - ``proxy`` 为具体地址：显式走该代理，**不**读取系统代理环境变量，
      也就不会被 NO_PROXY 之类的设置意外截断；
    - ``proxy`` 为空串：等价于 ``urlopen`` 的默认行为，沿用系统代理；
    - ``proxy`` 为 :data:`DIRECT_MARKER`：显式直连，忽略系统代理。
    """
    key = DIRECT_MARKER if proxy == DIRECT_MARKER else (proxy or "__env__")
    opener = _OPENER_CACHE.get(key)
    if opener is None:
        if proxy == DIRECT_MARKER:
            handler = urllib.request.ProxyHandler({})  # 显式直连
        elif proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler()  # 沿用系统代理
        opener = urllib.request.build_opener(handler)
        _OPENER_CACHE[key] = opener
    return opener


@dataclass
class DetectionResult:
    """一次 Content Provenance 检测的结论。"""

    #: True=检测到 OpenAI 水印；False=未检测到
    detected: bool = False
    #: API 返回的原始 results 列表
    raw_results: list[dict] = field(default_factory=list)
    #: 命中的信号类型，如 ["c2pa"]
    signals: list[str] = field(default_factory=list)
    #: 命中的模型名 / 签发方（可能为空）
    models: list[str] = field(default_factory=list)
    #: 出错的简要说明（None 表示调用成功）
    error: str | None = None
    #: 附加细节文本（show_details 时展示）
    details: str = ""
    #: 面向用户的整条提示（如限流/额度/Key 失效），优先级高于 error
    notice: str | None = None
    #: 本次失败建议的冷却秒数，>0 表示应暂停后续调用
    retry_after: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and self.notice is None


class AIImageDetector(Star):
    """检测图片是否带有 OpenAI(ChatGPT) 来源水印。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        #: SDK 一旦调用失败（例如版本过旧）就不再重复尝试
        self._sdk_unusable = False
        #: 串行化检测请求：冷却检查与请求占位必须原子（见 _check_image）
        self._check_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    def _api_base(self) -> str:
        base = str(self._cfg("api_base", DEFAULT_API_BASE)).strip() or DEFAULT_API_BASE
        return base.rstrip("/")

    def _trigger_keywords(self) -> list[str]:
        raw = self._cfg("trigger_keywords", list(DEFAULT_TRIGGER_KEYWORDS))
        if isinstance(raw, str):
            raw = [raw]
        keywords = [str(item).strip() for item in raw if str(item).strip()]
        return keywords or list(DEFAULT_TRIGGER_KEYWORDS)

    def _max_images(self) -> int:
        try:
            value = int(self._cfg("max_images_per_message", DEFAULT_MAX_IMAGES))
        except (TypeError, ValueError):
            value = DEFAULT_MAX_IMAGES
        return max(1, min(value, MAX_IMAGES_HARD_LIMIT))

    def _timeout(self) -> float:
        try:
            value = float(self._cfg("timeout_seconds", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT
        return max(5.0, min(value, 300.0))

    def _proxy(self) -> str:
        """返回校验后的代理地址；格式不支持时抛 :class:`ValueError`。

        留空时返回空串，由 :func:`get_opener` 决定是沿用系统代理还是直连。
        """
        return validate_proxy(str(self._cfg("proxy", "") or ""))

    def _get_opener(self):
        """按当前配置取 opener；代理配置非法时抛 :class:`ValueError`。"""
        proxy = self._proxy()
        if not proxy and not self._cfg("use_env_proxy", True):
            # 既没配代理，也不允许沿用系统代理 -> 显式直连
            return get_opener(DIRECT_MARKER)
        return get_opener(proxy)

    def _get_api_key(self) -> str | None:
        """从插件配置里读取 API Key，未配置时返回 None。"""
        key = str(self._cfg("openai_api_key", "") or "").strip()
        return key or None

    # ------------------------------------------------------------------ #
    # 触发判定
    # ------------------------------------------------------------------ #
    def _message_text(self, event: AstrMessageEvent) -> str:
        """只取顶层消息链（不含被引用消息）的纯文本部分。"""
        try:
            texts = [
                component.text
                for component in event.get_messages()
                if isinstance(component, Plain)
            ]
        except Exception:  # pragma: no cover - 极端适配器差异
            texts = []
        if texts:
            return " ".join(texts)
        return event.get_message_str()

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """判断消息链里是否 @ 了机器人本人（@全体成员 不算）。"""
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return False
        for component in event.get_messages():
            if isinstance(component, AtAll):
                continue
            if isinstance(component, At) and str(component.qq) == self_id:
                return True
        return False

    def _should_handle(self, event: AstrMessageEvent) -> bool:
        if self._cfg("require_at_bot", True) and not self._is_at_bot(event):
            return False

        text = self._message_text(event).strip().lower()
        if not text:
            return False
        return any(keyword.lower() in text for keyword in self._trigger_keywords())

    # ------------------------------------------------------------------ #
    # 图片收集
    # ------------------------------------------------------------------ #
    def _collect_images(self, event: AstrMessageEvent) -> list[Image]:
        """优先取本条消息里的图片，其次取被引用消息里的图片。"""
        messages = event.get_messages()
        images = [item for item in messages if isinstance(item, Image)]
        if images:
            return images

        for component in messages:
            if isinstance(component, Reply) and component.chain:
                images = [item for item in component.chain if isinstance(item, Image)]
                if images:
                    return images
        return []

    async def _resolve_image(self, image: Image) -> tuple[bytes, str]:
        """把消息段里的图片解析成 (字节内容, 建议文件名)。

        返回值中的文件名只用于 multipart 表单，OpenAI 依据内容判断格式。
        """
        raw_path = getattr(image, "path", "") or ""
        try:
            path = await image.convert_to_file_path()
        except Exception as exc:
            logger.debug(f"图片转本地路径失败，回落 base64：{exc}")
            return await self._resolve_image_via_base64(image)

        if not path:
            return await self._resolve_image_via_base64(image)

        try:
            data = await asyncio.to_thread(self._read_file, path)
        except ImageTooLargeError:
            raise
        except OSError as exc:
            logger.warning(f"读取图片文件失败：{path}（{exc}）")
            return await self._resolve_image_via_base64(image)

        return data, self._guess_filename(path, raw_path)

    async def _resolve_image_via_base64(self, image: Image) -> tuple[bytes, str]:
        encoded = await image.convert_to_base64()
        if encoded.startswith("data:"):
            header, _, payload = encoded.partition(",")
            mime = header[5:].split(";")[0] or "image/jpeg"
            suffix = mimetypes.guess_extension(mime) or ".jpg"
            # 先按 base64 长度估算解码后大小，避免为超限图片分配大块内存
            ensure_within_size_limit(len(payload) * 3 // 4)
            data = base64.b64decode(payload)
            ensure_within_size_limit(len(data))
            return data, sanitize_filename(f"image{suffix}")
        if encoded.startswith("base64://"):
            encoded = encoded[len("base64://") :]
        ensure_within_size_limit(len(encoded) * 3 // 4)
        data = base64.b64decode(encoded)
        ensure_within_size_limit(len(data))
        return data, "image.jpg"

    @staticmethod
    def _read_file(path: str) -> bytes:
        """读取图片文件；超过上限时直接拒绝，不把整个文件读进内存。"""
        ensure_within_size_limit(os.path.getsize(path))
        with open(path, "rb") as handle:
            data = handle.read()
        # 文件在读取过程中变大等边界情况，以实际读到的长度为准
        ensure_within_size_limit(len(data))
        return data

    @staticmethod
    def _guess_filename(path: str, fallback: str = "") -> str:
        """从路径推断上传用的文件名（纯展示用途，非可信输入）。"""
        for candidate in (path, fallback):
            if not candidate:
                continue
            name = PurePosixPath(str(candidate).replace("\\", "/")).name
            if name and "." in name:
                return sanitize_filename(name)
        return "image.jpg"

    # ------------------------------------------------------------------ #
    # 调用 OpenAI Content Provenance API
    # ------------------------------------------------------------------ #
    def _get_check_lock(self) -> asyncio.Lock:
        """惰性创建检测用的互斥锁（只能在事件循环内首次获取）。"""
        if self._check_lock is None:
            self._check_lock = asyncio.Lock()
        return self._check_lock

    async def _check_image(
        self, data: bytes, filename: str, api_key: str
    ) -> DetectionResult:
        if not data:
            return DetectionResult(error="图片内容为空")

        # 冷却检查与请求占位必须原子：否则并发触发时多个调用会同时通过
        # 冷却检查、一起打向接口，既放大限流又浪费配额。整段加锁后，
        # 同一时刻只有一次检测在途，后到的调用会看到刚设置的冷却并直接返回。
        async with self._get_check_lock():
            return await self._check_image_locked(data, filename, api_key)

    async def _check_image_locked(
        self, data: bytes, filename: str, api_key: str
    ) -> DetectionResult:
        remaining = cooldown_remaining()
        if remaining > 0:
            # 双重保险：即使调用方漏了检查，也不在冷却期内浪费配额
            return DetectionResult(
                notice=MESSAGE_RATE_LIMITED_COOLDOWN.format(
                    wait=_format_wait(remaining)
                ),
                error="cooldown",
                retry_after=remaining,
            )

        if AsyncOpenAI is not None and not self._sdk_unusable:
            try:
                return await self._check_image_with_sdk(data, filename, api_key)
            except Exception as exc:  # noqa: BLE001 - 兜底到 HTTP 实现
                if _looks_like_rate_limit(exc):
                    # 限流时绝不能落到 HTTP 再打一次，否则等于双倍消耗配额
                    return self._sdk_rate_limit_result(exc)

                # 常见情况：环境里的 openai 版本较旧，没有 content_provenance_checks。
                # AstrBot 会锁定核心已安装的 openai 版本，插件不应也不能升级它，
                # 因此这里只提示一次并始终使用插件自带的实现。
                self._sdk_unusable = True
                logger.info(
                    f"环境中的 openai SDK 不可用于内容溯源接口（{exc!r}），"
                    "已改用插件自带的请求实现，功能不受影响。"
                )

        return await asyncio.to_thread(
            self._check_image_with_http, data, filename, api_key
        )

    def _sdk_rate_limit_result(self, exc: Exception) -> DetectionResult:
        """SDK 抛出的限流/额度异常 -> 与 HTTP 路径一致的提示与冷却。"""
        status = getattr(exc, "status_code", None)
        text = f"{exc}"
        lowered = text.lower()
        is_quota = status == 403 or any(m in lowered for m in _QUOTA_MARKERS)

        if is_quota:
            seconds = start_cooldown(QUOTA_COOLDOWN, "insufficient_quota")
            return DetectionResult(
                notice=_MSG_QUOTA_EXHAUSTED,
                error="sdk insufficient_quota",
                retry_after=seconds,
            )

        wait = 0.0
        response = getattr(exc, "response", None)
        if response is not None:
            wait = _parse_retry_after(getattr(response, "headers", None))
        if wait <= 0:
            wait = DEFAULT_RATE_LIMIT_COOLDOWN
        seconds = start_cooldown(wait, "rate_limited")
        logger.warning(f"OpenAI SDK 报限流，冷却 {seconds:.0f}s")
        return DetectionResult(
            notice=_MSG_RATE_LIMITED.format(wait=_format_wait(seconds)),
            error="sdk rate_limited",
            retry_after=seconds,
        )

    async def _check_image_with_sdk(
        self, data: bytes, filename: str, api_key: str
    ) -> DetectionResult:
        """通过官方 openai SDK 调用（可选路径）。

        需要 ``openai>=3.24.0``（该版本起才有 ``content_provenance_checks``）。
        关掉 SDK 自带的自动重试，避免限流时重复消耗配额；瞬时故障由
        ``_check_image_with_http`` 的退避逻辑负责，SDK 失败也会回落到那里。

        客户端持有 httpx 连接池，必须关闭：既要在正常路径关闭，也要在
        抛异常（例如版本过旧没有该接口）时关闭，否则每次调用都会泄漏连接。
        """
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._api_base(),
            timeout=self._timeout(),
            max_retries=0,
        )
        try:
            # SDK 的 multipart 上传依赖文件对象的 name 作为 filename
            stream = io.BytesIO(data)
            stream.name = filename
            response = await client.content_provenance_checks.create(file=stream)
            payload = (
                response.model_dump() if hasattr(response, "model_dump") else response
            )
        finally:
            await client.close()
        return self._parse_response(payload)

    def _build_request(self, data: bytes, filename: str, api_key: str):
        """构造一次 Content Provenance multipart 请求。"""
        boundary = f"----AstrBotAIImageDetector{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="file"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        return urllib.request.Request(  # noqa: S310 - 端点来自配置，默认 https
            f"{self._api_base()}/content_provenance_checks",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    def _check_image_with_http(
        self, data: bytes, filename: str, api_key: str
    ) -> DetectionResult:
        """不依赖 openai SDK 的 multipart 实现（默认路径，标准库即可）。

        瞬时故障（5xx / 网络错误）最多重试 ``TRANSIENT_RETRY_ATTEMPTS`` 次；
        限流（429）与额度、鉴权问题**不重试**，直接返回带冷却建议的提示，
        避免在额度已经受限时继续消耗配额。

        请求经 :func:`get_opener` 发出的 opener，代理配置在进入线程池前就
        完成了校验，因此这里不会因为配置问题抛异常。
        """
        last_result: DetectionResult | None = None
        opener = self._get_opener()

        for attempt in range(TRANSIENT_RETRY_ATTEMPTS):
            request = self._build_request(data, filename, api_key)
            try:
                with opener.open(  # noqa: S310 - 端点来自配置，默认 https
                    request, timeout=self._timeout()
                ) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return self._parse_response(payload)
            except urllib.error.HTTPError as exc:
                result = self._http_error_result(exc)
                if exc.code < 500:
                    return result
                last_result = result
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_result = DetectionResult(
                    error=f"网络错误：{redact(exc)}"[:200]
                )
            except json.JSONDecodeError as exc:
                last_result = DetectionResult(error=f"响应解析失败：{exc}")

            if attempt < TRANSIENT_RETRY_ATTEMPTS - 1:
                wait = RETRY_BACKOFF_BASE * (2**attempt) + random.uniform(0, 0.5)
                error_hint = last_result.error if last_result else ""
                logger.warning(
                    f"Content Provenance 调用失败，{wait:.1f}s 后重试"
                    f"（{attempt + 1}/{TRANSIENT_RETRY_ATTEMPTS - 1}）：{error_hint}"
                )
                time.sleep(wait)

        assert last_result is not None
        if last_result.error and last_result.error.startswith("网络错误"):
            return last_result
        last_result.retry_after = SERVER_ERROR_COOLDOWN
        start_cooldown(SERVER_ERROR_COOLDOWN, "server_error")
        return last_result

    def _http_error_result(self, exc: urllib.error.HTTPError) -> DetectionResult:
        """把 HTTP 错误码翻译成「用户看得懂的提示 + 冷却建议」。

        401/403/429 属于不会因为重试而好转的情况，一律不重试。
        """
        code = int(exc.code)
        detail = _safe_error_message(exc)
        lowered = detail.lower()
        is_quota = any(marker in lowered for marker in _QUOTA_MARKERS)
        suffix = f"（{detail}）" if detail else ""

        if code == 401:
            return DetectionResult(notice=_MSG_UNAUTHORIZED + suffix, error=f"HTTP 401")

        if code == 403 and is_quota:
            seconds = start_cooldown(QUOTA_COOLDOWN, "insufficient_quota")
            return DetectionResult(
                notice=_MSG_QUOTA_EXHAUSTED + suffix,
                error="HTTP 403 insufficient_quota",
                retry_after=seconds,
            )

        if code == 403:
            return DetectionResult(notice=_MSG_FORBIDDEN + suffix, error="HTTP 403")

        if code == 429:
            wait = _parse_retry_after(exc.headers)
            if wait <= 0:
                wait = DEFAULT_RATE_LIMIT_COOLDOWN
            seconds = start_cooldown(wait, "rate_limited")
            logger.warning(f"Content Provenance 接口限流，冷却 {seconds:.0f}s")
            return DetectionResult(
                notice=_MSG_RATE_LIMITED.format(wait=_format_wait(seconds)) + suffix,
                error="HTTP 429 rate_limited",
                retry_after=seconds,
            )

        if code >= 500:
            return DetectionResult(
                notice=_MSG_SERVER_ERROR.format(code=code) + suffix,
                error=f"HTTP {code}",
                retry_after=SERVER_ERROR_COOLDOWN,
            )

        return DetectionResult(
            notice=_MSG_UNEXPECTED_STATUS.format(code=code) + suffix,
            error=f"HTTP {code}",
        )

    def _parse_response(self, payload) -> DetectionResult:
        """把 API 响应翻译成「是否检测到 OpenAI 水印」。"""
        if not isinstance(payload, dict):
            return DetectionResult(error=f"响应格式异常：{payload!r}"[:200])

        raw_results = payload.get("results") or []
        if not isinstance(raw_results, list):
            raw_results = []

        detected_signals: list[str] = []
        models: list[str] = []
        detail_lines: list[str] = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue
            signal = str(item.get("type") or "unknown")
            outcome = str(item.get("outcome") or "unknown")
            label = _SIGNAL_LABELS.get(signal, signal)

            model = item.get("model")
            issuer = item.get("issuer")
            validation = item.get("validation_state")
            generated_at = item.get("generated_at")

            is_detected = outcome == "detected"
            if is_detected and signal not in detected_signals:
                detected_signals.append(signal)
            for candidate in (model, issuer):
                if candidate and candidate not in models:
                    models.append(str(candidate))

            extra = []
            if model:
                extra.append(f"model={model}")
            if issuer:
                extra.append(f"issuer={issuer}")
            if validation:
                extra.append(f"validation_state={validation}")
            if generated_at:
                extra.append(f"generated_at={generated_at}")
            suffix = f"（{'，'.join(extra)}）" if extra else ""
            detail_lines.append(f"- {label}: {outcome}{suffix}")

        details = "\n".join(detail_lines)
        if not detail_lines:
            details = "- API 未返回任何来源信号结果"

        return DetectionResult(
            detected=bool(detected_signals),
            raw_results=[item for item in raw_results if isinstance(item, dict)],
            signals=detected_signals,
            models=models,
            details=details,
        )

    # ------------------------------------------------------------------ #
    # 结果文案
    # ------------------------------------------------------------------ #
    def _reply_template(self, key: str, default: str) -> str:
        """读取自定义回复模板；未配置或内容为空时用内置默认文案。"""
        value = self._cfg(key, "")
        text = "" if value is None else str(value).strip()
        return text or default

    @staticmethod
    def _format_template(template: str, values: dict) -> str:
        """把模板里的 {signals} / {models} 占位符换成实际内容。

        模板由用户填写，可能含未知占位符或落单的大括号，此时原样返回模板，
        绝不因为一句配置把整次检测的回包搞崩。
        """
        try:
            return template.format_map(values)
        except (KeyError, IndexError, ValueError, AttributeError, TypeError):
            logger.warning(
                "自定义回复文案里的占位符无法替换，已按原文发送；"
                "支持的占位符只有 {signals} 与 {models}。"
            )
            return template

    @staticmethod
    def _tidy(message: str) -> str:
        """收尾：去掉占位符为空时留下的空壳行，再压掉多余空行。

        自定义模板里的 ``{signals}`` / ``{models}`` 替换为空字符串后，可能只剩下
        括号或标签，例如「（命中：）」或「来源模型/签发方：」；全空时还会整行只剩
        空白。默认模板在「没有来源模型」这种情况下就会出现后者，所以必须处理。

        只清理插件自己生成的这些固定形状，用户手写的内容（包括他自己写的冒号结尾
        的句子）不会被误删。
        """
        lines: list[str] = []
        for line in message.split("\n"):
            stripped = line.strip()
            # 整行只有空白（占位符整行被替换为空）
            if not stripped:
                lines.append("")
                continue
            # 「（命中：）」这类空括号：可能整行只有它，也可能跟在正文后面
            line = _EMPTY_PARENS.sub("", line)
            if not line.strip():
                continue
            # 只剩标签的行，例如「来源模型/签发方：」
            if re.fullmatch(r"[^\s：:]{1,20}[：:]\s*", stripped):
                continue
            lines.append(line.rstrip())

        collapsed: list[str] = []
        for line in lines:
            if not line.strip() and (not collapsed or not collapsed[-1].strip()):
                continue
            collapsed.append(line)
        while collapsed and not collapsed[-1].strip():
            collapsed.pop()
        return "\n".join(collapsed)

    @classmethod
    def _with_details(cls, message: str, details: str) -> str:
        """在结论后追加检测明细，不多留空行。"""
        if not details:
            return cls._tidy(message)
        return f"{cls._tidy(message)}\n\n检测明细：\n{details}"

    def _compose_message(self, result: DetectionResult, show_details: bool) -> str:
        if result.notice:
            # 限流 / 额度 / Key 失效等：提示本身就是完整句子，不再套「检测失败：」
            return result.notice

        if not result.ok:
            return f"检测失败：{result.error}"

        if result.detected:
            labels = [_SIGNAL_LABELS.get(sig, sig) for sig in result.signals]
            message = self._format_template(
                self._reply_template(CONFIG_REPLY_DETECTED, MESSAGE_DETECTED),
                {"signals": "、".join(labels), "models": "、".join(result.models)},
            )
        else:
            message = self._reply_template(
                CONFIG_REPLY_NOT_DETECTED, MESSAGE_NOT_DETECTED
            )

        if show_details:
            return self._with_details(message, result.details)
        return self._tidy(message)

    def _reply_text(self, event: AstrMessageEvent, text: str) -> str:
        if not self._cfg("reply_with_at", False):
            return text
        return f"@{event.get_sender_name()} {text}"

    @staticmethod
    def _config_error_message(exc: ValueError) -> str:
        """把配置类异常翻译成给用户看的提示。"""
        if "unsupported proxy" in str(exc) or "socks" in str(exc).lower():
            return MESSAGE_PROXY_UNSUPPORTED
        return MESSAGE_PROXY_INVALID

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_image_check(self, event: AstrMessageEvent):
        """监听所有消息，命中「@机器人 + ai吗」时检测图片水印。"""
        if not self._should_handle(event):
            return

        # 命中本插件后不再让默认 LLM 重复回答同一条消息
        should_call_llm = getattr(event, "should_call_llm", None)
        if callable(should_call_llm):
            should_call_llm(False)

        images = self._collect_images(event)
        if not images:
            yield event.plain_result(self._reply_text(event, MESSAGE_NO_IMAGE))
            return

        max_images = self._max_images()
        if len(images) > max_images:
            logger.info(f"消息包含 {len(images)} 张图片，只检测前 {max_images} 张")
            images = images[:max_images]

        api_key = self._get_api_key()
        if not api_key:
            yield event.plain_result(self._reply_text(event, MESSAGE_NO_API_KEY))
            return

        # 处于限流/额度冷却期内：直接告知，不再发起请求（避免继续消耗配额）
        remaining = cooldown_remaining()
        if remaining > 0:
            logger.info(
                f"接口冷却中（剩余 {remaining:.0f}s，原因 {_COOLDOWN_REASON}），跳过本次检测"
            )
            yield event.plain_result(
                self._reply_text(
                    event,
                    MESSAGE_RATE_LIMITED_COOLDOWN.format(wait=_format_wait(remaining)),
                )
            )
            return

        show_details = bool(self._cfg("show_details", False))
        if self._cfg("notify_on_start", False):
            yield event.plain_result(f"正在检测 {len(images)} 张图片，请稍候…")

        for position, image in enumerate(images):
            try:
                data, filename = await self._resolve_image(image)
            except ImageTooLargeError as exc:
                logger.info(f"图片超过大小上限，已跳过：{exc}")
                yield event.plain_result(
                    self._reply_text(event, MESSAGE_IMAGE_TOO_LARGE)
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 任何图片解析异常都提示用户
                logger.error(f"读取图片失败：{exc!r}")
                yield event.plain_result(
                    self._reply_text(event, MESSAGE_DOWNLOAD_FAILED)
                )
                continue

            try:
                result = await self._check_image(data, filename, api_key)
            except ValueError as exc:
                # 代理等配置问题：提示用户改配置，不要当成"检测失败"的原始报错
                logger.warning(f"插件配置有误：{exc}")
                yield event.plain_result(
                    self._reply_text(event, self._config_error_message(exc))
                )
                break
            except Exception as exc:  # noqa: BLE001 - 兜底，避免插件崩溃
                logger.error(f"调用 Content Provenance API 失败：{exc!r}")
                safe = redact(f"{type(exc).__name__}: {exc}")
                result = DetectionResult(error=safe[:200])

            yield event.plain_result(
                self._reply_text(event, self._compose_message(result, show_details))
            )

            if result.retry_after > 0:
                # 已进入冷却：剩余图片不再尝试，避免把配额浪费在注定失败的请求上
                logger.info(
                    f"接口限流/不可用（建议冷却 {result.retry_after:.0f}s），"
                    f"跳过本批剩余 {len(images) - position - 1} 张图片"
                )
                break

    async def terminate(self) -> None:
        """插件卸载 / 重载时调用。"""
        logger.debug("astrbot_plugin_ai_image_detector 已卸载")
