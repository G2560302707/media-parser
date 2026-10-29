"""Cookie 真实探针：金丝雀链接双跑对照判定。

同一条金丝雀链接分别带当前 Cookie 与强制无 Cookie 各解析一次：

- 两路都成功且结果一致 → healthy（增益型平台提示可更换金丝雀）
- 带 Cookie 成功、无 Cookie 失败/降级 → healthy（Cookie 增益明确）
- 带 Cookie 失败、无 Cookie 成功 → stale（凭据已失效的实锤信号）
- 两路都失败 → probe_failed（金丝雀链接或网络问题，不怪 Cookie）

探针直接复用解析链路（ParserFactory 建实例），不走 HTTP 自调用，
不占用 worker 与限流额度；"强制无 Cookie"通过临时遮蔽
cookie_manager.get_platform_cookie 实现，不改动解析器本身。
"""

import logging
import threading
import time
from contextlib import contextmanager

from configs.logging_config import get_logger
from src.db import setting
from src.parser_factory import ParserFactory
from src.utils import cookie_registry
from src.utils.cookie_manager import get_platform_cookie as _real_get_platform_cookie
from utils.web_fetcher import UrlParser, WebFetcher

logger = get_logger(__name__)

# 同平台探针最小间隔（秒），防止管理员连点触发目标平台风控。
PROBE_DEBOUNCE_SECONDS = 5 * 60

# 内置金丝雀：优先读 system_settings 的 canary_{platform}，为空时用内置样例。
DEFAULT_CANARIES = {
    # 抖音实况作品（测试集样例）：无 Cookie 时仅返回静态图，可体现增益差异。
    "douyin": "https://v.douyin.com/ZtiyRAA2JLs/",
}


@contextmanager
def _no_cookie():
    """临时遮蔽所有平台 Cookie 读取，用于无 Cookie 对照路。"""

    def _blank(platform_key=None, env_var=None):
        return ""

    import src.utils.cookie_manager as cm

    original = cm.get_platform_cookie
    cm.get_platform_cookie = _blank
    try:
        yield
    finally:
        cm.get_platform_cookie = original


def _canary_url(platform_key):
    entry = cookie_registry.get_entry(platform_key)
    if not entry:
        return None
    url = (setting(entry["canary_setting"]) or "").strip()
    if url:
        return url
    return DEFAULT_CANARIES.get(platform_key)


def _run_parse(share_url, expected_platforms):
    """复用解析链路跑一次完整解析，返回统一的媒体结果摘要。"""
    redirect_url = WebFetcher.fetch_redirect_url(share_url)
    if not redirect_url:
        return {"ok": False, "reason": "REDIRECT_FAILED", "detail": "短链重定向失败"}
    platform = UrlParser.get_platform(redirect_url)
    if platform not in expected_platforms:
        return {"ok": False, "reason": "PLATFORM_MISMATCH",
                "detail": f"期望 {'/'.join(expected_platforms)}，实际 {platform}"}
    real_url = UrlParser.extract_video_address(redirect_url)
    parser = ParserFactory.create_parser(platform, real_url)

    result = {
        "title": parser.get_title_content(),
        "video_url": parser.get_real_video_url(),
        "video_list": _safe(parser.get_video_list) or [],
        "cover_url": parser.get_cover_photo_url(),
        "image_list": _safe(parser.get_image_list) or [],
        "audio_url": _safe(parser.get_audio_url),
    }
    live_urls = [
        img.get("live_photo_url") for img in result["image_list"]
        if isinstance(img, dict) and img.get("live_photo_url")
    ]
    has_media = bool(
        result["video_url"] or result["cover_url"] or result["image_list"] or result["audio_url"]
    )
    return {
        "ok": has_media,
        "reason": "OK" if has_media else "MEDIA_NOT_FOUND",
        "detail": {
            "has_video": bool(result["video_url"]),
            "image_count": len(result["image_list"]),
            "live_count": len(live_urls),
        },
        "live_count": len(live_urls),
    }


def _safe(func, default=None):
    if not callable(func):
        return default
    try:
        return func()
    except Exception:
        return default


def _judge(platform_key, with_result, without_result):
    """按双跑对照矩阵判定。"""
    if with_result["ok"] and without_result["ok"]:
        gain = (
            with_result.get("live_count", 0) > without_result.get("live_count", 0)
        )
        if gain:
            return "healthy", {"signal": "COOKIE_GAIN", "with": with_result["detail"], "without": without_result["detail"]}
        return "healthy", {"signal": "BOTH_OK", "with": with_result["detail"], "without": without_result["detail"],
                           "note": "两路结果一致，金丝雀未能体现增益，建议更换样例"}
    if with_result["ok"] and not without_result["ok"]:
        return "healthy", {"signal": "COOKIE_GAIN", "with": with_result["detail"],
                           "without": {"reason": without_result["reason"]}}
    if not with_result["ok"] and without_result["ok"]:
        return "stale", {"signal": "COOKIE_BROKEN", "with": {"reason": with_result["reason"]},
                         "without": without_result["detail"]}
    return "probe_failed", {"signal": "BOTH_FAILED", "with": {"reason": with_result["reason"]},
                            "without": {"reason": without_result["reason"]}}


_probe_lock = threading.Lock()
_last_probe_at = {}


def probe_platform(platform_key, force=False):
    """对单平台执行双跑对照探针。返回 {status, detail, duration_ms}。"""
    entry = cookie_registry.get_entry(platform_key)
    if not entry:
        return {"status": "probe_failed", "detail": {"error": "未注册的平台"}, "duration_ms": 0}
    if cookie_registry.get_cookie_source(platform_key) == "none":
        return {"status": "not_configured", "detail": {"error": "未配置凭据"}, "duration_ms": 0}

    canary = _canary_url(platform_key)
    if not canary:
        return {"status": "probe_failed",
                "detail": {"error": "未配置金丝雀链接，请在巡检页设置后重试"},
                "duration_ms": 0}

    with _probe_lock:
        last = _last_probe_at.get(platform_key, 0)
        elapsed = time.monotonic() - last
        if not force and elapsed < PROBE_DEBOUNCE_SECONDS:
            return {"status": "debounced",
                    "detail": {"error": f"距上次探针不足 {PROBE_DEBOUNCE_SECONDS // 60} 分钟，已跳过（防风控）"},
                    "duration_ms": 0}
        _last_probe_at[platform_key] = time.monotonic()

    started = time.monotonic()
    expected_platforms = set(entry["log_names"])
    try:
        with_result = _run_parse(canary, expected_platforms)
        with _no_cookie():
            without_result = _run_parse(canary, expected_platforms)
        status, detail = _judge(platform_key, with_result, without_result)
    except Exception as exc:  # 探针失败不抛出，统一结构化返回
        logger.warning(f"Cookie 探针异常 {platform_key}: {exc}")
        status, detail = "probe_failed", {"signal": "EXCEPTION", "error": str(exc)[:200]}

    duration_ms = int((time.monotonic() - started) * 1000)
    cookie_registry.record_probe(platform_key, status, "probe", detail, duration_ms)
    return {"status": status, "detail": detail, "duration_ms": duration_ms}
