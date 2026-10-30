"""把静图 + 实况动轨合成 iOS 可导入的 Motion Photo JPEG。"""

from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlparse

import requests

from configs.general_constants import USER_AGENT_M
from configs.logging_config import get_logger
from src.db import get_db, utcnow
from src.utils.cookie_manager import get_platform_cookie

logger = get_logger(__name__)

ASSET_TTL_SECONDS = 2 * 60 * 60
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 25
VIDEO_PADDING = 0
VIDEO_STILL_SEEK_SECONDS = 0.5
VIDEO_STILL_TIMESTAMP_US = 500_000
_BLACK_SAMPLE_COUNT = 2048
_BLACK_PIXEL_THRESHOLD = 8
_BLACK_MAX_BRIGHT_RATIO = 0.02
_SAFARI_UA = USER_AGENT_M[0] if USER_AGENT_M else (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


class LivePhotoError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def register_live_asset(still_url, video_url):
    """记下这对地址，返回短 token。同一对地址复用同一 token。"""
    still_url = (still_url or "").strip()
    video_url = (video_url or "").strip()
    if not still_url or not video_url:
        raise LivePhotoError("LIVE_PHOTO_INVALID", "缺少静图或动轨地址")
    db = get_db()
    db.execute(
        "DELETE FROM live_photo_assets WHERE expires_at < ?",
        (utcnow(),),
    )
    existing = db.execute(
        "SELECT token FROM live_photo_assets WHERE still_url=? AND video_url=? AND expires_at >= ?",
        (still_url, video_url, utcnow()),
    ).fetchone()
    if existing:
        return existing["token"]
    token = secrets.token_urlsafe(16)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ASSET_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO live_photo_assets(token, still_url, video_url, created_at, expires_at) "
        "VALUES(?,?,?,?,?)",
        (token, still_url, video_url, utcnow(), expires_at),
    )
    db.commit()
    return token


def normalize_live_token(token):
    token = (token or "").strip()
    lower = token.lower()
    if lower.endswith("mp.jpg"):
        token = token[:-6]
    elif lower.endswith(".jpg"):
        token = token[:-4]
    if token.endswith("/LIVEMP") or token.endswith("/livemp"):
        token = token.rsplit("/", 1)[0]
    return token


def live_asset_public_url(token):
    from src.auth import api_base_url

    token = normalize_live_token(token)
    # 文件名必须是 *MP.jpg，否则 Motion Photo 读取方可以忽略后面的动轨。
    return f"{api_base_url()}api/v1/live-photo/{token}/LIVEMP.jpg"


def get_live_asset(token):
    token = normalize_live_token(token)
    if not token:
        return None
    db = get_db()
    row = db.execute(
        "SELECT still_url, video_url FROM live_photo_assets WHERE token=? AND expires_at >= ?",
        (token, utcnow()),
    ).fetchone()
    if not row:
        return None
    return {"still_url": row["still_url"], "video_url": row["video_url"]}


def live_asset_api_path(token):
    token = normalize_live_token(token)
    return f"/api/v1/live-photo/{token}/LIVEMP.jpg"


def build_live_asset(token):
    asset = get_live_asset(token)
    if not asset:
        raise LivePhotoError("LIVE_PHOTO_EXPIRED", "实况照片已过期或不存在，请重新解析")
    still = download_media(asset["still_url"], MAX_IMAGE_BYTES)
    video = download_media(asset["video_url"], MAX_VIDEO_BYTES)
    timestamp_us = 0
    if _is_mostly_black_image(still):
        logger.info(
            "live photo still looks like a black placeholder; extracting a frame from video"
        )
        still = _still_from_video(video, VIDEO_STILL_SEEK_SECONDS)
        timestamp_us = VIDEO_STILL_TIMESTAMP_US
    return mux_motion_photo(still, video, timestamp_us=timestamp_us)


def _is_mostly_black_image(data):
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        image = Image.open(BytesIO(data))
        image.load()
        if image.mode != "RGB":
            image = image.convert("RGB")
        pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        total = len(pixels)
        if total <= 0:
            return True
        step = max(1, total // _BLACK_SAMPLE_COUNT)
        bright = 0
        samples = 0
        for index in range(0, total, step):
            pixel = pixels[index]
            if isinstance(pixel, int):
                r = g = b = pixel
            else:
                r, g, b = pixel
            samples += 1
            if (
                r > _BLACK_PIXEL_THRESHOLD
                or g > _BLACK_PIXEL_THRESHOLD
                or b > _BLACK_PIXEL_THRESHOLD
            ):
                bright += 1
        if samples <= 0:
            return True
        return (bright / samples) <= _BLACK_MAX_BRIGHT_RATIO
    except Exception:
        return False


def _still_from_video(video_bytes, seek_seconds):
    if not shutil.which("ffmpeg"):
        raise LivePhotoError(
            "LIVE_PHOTO_BUILD_FAILED",
            "静图为黑图占位且服务器未安装 ffmpeg，无法从动轨取封面",
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        video_path = os.path.join(temp_dir, "live.mp4")
        frame_path = os.path.join(temp_dir, "frame.jpg")
        with open(video_path, "wb") as handle:
            handle.write(video_bytes)
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seek_seconds),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            frame_path,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LivePhotoError(
                "LIVE_PHOTO_BUILD_FAILED",
                "无法从动轨提取封面帧",
            ) from exc
        if completed.returncode != 0 or not os.path.isfile(frame_path):
            detail = (completed.stderr or b"").decode("utf-8", errors="replace")[:200]
            raise LivePhotoError(
                "LIVE_PHOTO_BUILD_FAILED",
                f"无法从动轨提取封面帧{(': ' + detail) if detail else ''}",
            )
        with open(frame_path, "rb") as handle:
            frame_bytes = handle.read()
    if not frame_bytes or _is_mostly_black_image(frame_bytes):
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "动轨封面帧无效")
    return frame_bytes


def mux_motion_photo(image_bytes, video_bytes, timestamp_us=0):
    """合成 Google Motion Photo / Micro Video JPEG，iOS 相册可当实况导入。"""
    if not image_bytes:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "静图为空")
    if not video_bytes:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "动轨为空")
    jpeg = _to_jpeg(image_bytes)
    video = _normalize_video(video_bytes)
    xmp = _motion_photo_xmp(len(video), timestamp_us)
    jpeg = _insert_xmp(jpeg, xmp)
    return jpeg + (b"\x00" * VIDEO_PADDING) + video


def download_media(url, max_bytes):
    parsed = _require_public_http_url(url)
    headers = {
        "User-Agent": _SAFARI_UA,
        "Accept": "*/*",
    }
    referer = _referer_for_host(parsed.hostname)
    if referer:
        headers["Referer"] = referer
    cookie = _cookie_for_host(parsed.hostname)
    if cookie:
        headers["Cookie"] = cookie
    try:
        with requests.get(
            url,
            headers=headers,
            timeout=DOWNLOAD_TIMEOUT,
            stream=True,
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            final_host = urlparse(resp.url).hostname
            if final_host:
                _require_public_host(final_host)
            chunks = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "媒体文件过大，无法合成实况")
                chunks.append(chunk)
            data = b"".join(chunks)
    except LivePhotoError:
        raise
    except requests.RequestException as exc:
        logger.warning("live photo download failed: %s", exc)
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "下载静图或动轨失败") from exc
    if not data:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "下载结果为空")
    return data


def _to_jpeg(data):
    jpeg = _jpeg_without_trailer(data) if data[:2] == b"\xff\xd8" else None
    if jpeg:
        return jpeg
    try:
        from PIL import Image
    except ImportError as exc:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "静图不是 JPEG，无法合成实况") from exc
    try:
        image = Image.open(BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=95)
        jpeg = output.getvalue()
    except Exception as exc:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "静图无法转成 JPEG") from exc
    if jpeg[:2] != b"\xff\xd8":
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "静图无法转成 JPEG")
    return _jpeg_without_trailer(jpeg)


def _normalize_video(data):
    if len(data) < 12:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "动轨不是有效视频")
    if data[4:8] != b"ftyp":
        logger.warning("live photo video missing ftyp, muxing anyway")
    return data


def _jpeg_without_trailer(data):
    end = data.rfind(b"\xff\xd9")
    if end < 0:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "静图不是有效 JPEG")
    return data[: end + 2]


def _insert_xmp(jpeg, xmp_xml):
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp_xml
    if len(payload) + 2 > 65535:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "实况元数据过长")
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    body = _strip_xmp_app1(jpeg[2:])
    return b"\xff\xd8" + app1 + body


def _strip_xmp_app1(body):
    offset = 0
    out = bytearray()
    while offset + 4 <= len(body):
        if body[offset] != 0xFF:
            out.extend(body[offset:])
            return bytes(out)
        marker = body[offset + 1]
        if marker == 0xDA:  # SOS
            out.extend(body[offset:])
            return bytes(out)
        if marker in (0xD8, 0xD9) or marker < 0xD0:
            out.extend(body[offset: offset + 2])
            offset += 2
            continue
        length = int.from_bytes(body[offset + 2: offset + 4], "big")
        end = offset + 2 + length
        segment = body[offset:end]
        if marker == 0xE1 and b"http://ns.adobe.com/xap/1.0/" in segment:
            offset = end
            continue
        out.extend(segment)
        offset = end
    out.extend(body[offset:])
    return bytes(out)


def _motion_photo_xmp(video_size, timestamp_us):
    ts = max(0, int(timestamp_us or 0))
    xml = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        ' xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"'
        ' xmlns:Container="http://ns.google.com/photos/1.0/container/"'
        ' xmlns:Item="http://ns.google.com/photos/1.0/container/item/"'
        ' GCamera:MotionPhoto="1"'
        ' GCamera:MotionPhotoVersion="1"'
        f' GCamera:MotionPhotoPresentationTimestampUs="{ts}"'
        ' GCamera:MicroVideo="1"'
        ' GCamera:MicroVideoVersion="1"'
        f' GCamera:MicroVideoOffset="{video_size}"'
        f' GCamera:MicroVideoPresentationTimestampUs="{ts}">'
        "<Container:Directory><rdf:Seq>"
        '<rdf:li rdf:parseType="Resource">'
        '<Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"'
        f' Item:Length="0" Item:Padding="{VIDEO_PADDING}"/>'
        "</rdf:li>"
        '<rdf:li rdf:parseType="Resource">'
        '<Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto"'
        f' Item:Length="{video_size}" Item:Padding="0"/>'
        "</rdf:li>"
        "</rdf:Seq></Container:Directory>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return xml.encode("utf-8")


def _require_public_http_url(url):
    if not isinstance(url, str) or not url.strip():
        raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址无效")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise LivePhotoError("LIVE_PHOTO_INVALID", "仅允许 http/https 媒体地址")
    if parsed.username or parsed.password:
        raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址无效")
    if parsed.port not in {None, 80, 443}:
        raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址端口不允许")
    host = (parsed.hostname or "").lower().rstrip(".")
    _require_public_host(host)
    return parsed


def _require_public_host(host):
    host = (host or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址无效")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _is_bad_ip(ip):
        raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址无效")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "无法解析媒体地址") from exc
    if not infos:
        raise LivePhotoError("LIVE_PHOTO_BUILD_FAILED", "无法解析媒体地址")
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if _is_bad_ip(resolved):
            raise LivePhotoError("LIVE_PHOTO_INVALID", "媒体地址无效")


def _is_bad_ip(ip):
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _referer_for_host(host):
    host = (host or "").lower()
    if any(part in host for part in ("douyin", "snssdk", "iesdouyin", "douyinvod", "byteimg", "douyinpic")):
        return "https://www.douyin.com/"
    if any(part in host for part in ("xhscdn", "xiaohongshu", "xhslink")):
        return "https://www.xiaohongshu.com/"
    if "weibo" in host:
        return "https://weibo.com/"
    return None


def _cookie_for_host(host):
    host = (host or "").lower()
    if any(part in host for part in ("douyin", "snssdk", "iesdouyin", "douyinvod", "byteimg", "douyinpic")):
        return get_platform_cookie("douyin")
    if any(part in host for part in ("xhscdn", "xiaohongshu", "xhslink")):
        return get_platform_cookie("xiaohongshu")
    if any(part in host for part in ("weibo", "sinaimg")):
        return get_platform_cookie("weibo")
    return ""
