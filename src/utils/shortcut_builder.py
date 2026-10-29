"""Generate an unsigned iOS Shortcut that opens the mobile parser with shared text.

The built-in file is a fallback. Admins can drop a real iPhone-exported
``.shortcut`` under the data directory, or point the install button at an
iCloud share URL, without rebuilding the app.
"""

from datetime import datetime, timezone
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

import plistlib
from flask import current_app


OVERRIDE_FILENAME = "media-parser.shortcut"
MAX_OVERRIDE_BYTES = 1024 * 1024
ICLOUD_INSTALL_PREFIXES = (
    "https://www.icloud.com/shortcuts/",
    "https://shortcuts.apple.com/",
)

OBJ = "\ufffc"  # U+FFFC, Shortcuts interpolation placeholder


def _uuid():
    return str(uuid4()).upper()


def _action(identifier, params):
    params.setdefault("UUID", _uuid())
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": params,
    }


def _token_string(text, attachments=None):
    return {
        "Value": {
            "string": text,
            "attachmentsByRange": attachments or {},
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _output_attachment(output_uuid, name):
    return {
        "Value": {
            "OutputUUID": output_uuid,
            "OutputName": name,
            "Type": "ActionOutput",
        },
        "WFSerializationType": "WFTextTokenAttachment",
    }


def build_shortcut_plist(base_url):
    """Return binary plist bytes for the「媒体解析」shortcut.

    The shortcut URL-encodes share-sheet / shortcut input and opens
    ``{base_url}/s?text=...``. Parsing and saving happen on the web page so
    the user only pastes an API key once in Safari.
    """
    base_url = (base_url or "").rstrip("/")
    share_uuid = _uuid()
    encode_uuid = _uuid()
    url_uuid = _uuid()

    actions = [
        _action("is.workflow.actions.comment", {
            "WFCommentActionText": (
                "媒体解析 1.0\n"
                "从抖音 / 小红书等 App 点分享即可。\n"
                "首次请用 Safari 打开网站注册，密钥会自动保存在浏览器。"
            ),
        }),
        _action("is.workflow.actions.gettext", {
            "UUID": share_uuid,
            "CustomOutputName": "分享内容",
            "WFTextActionText": _token_string(OBJ, {
                "{0, 1}": {"Type": "ExtensionInput"},
            }),
        }),
        _action("is.workflow.actions.urlencode", {
            "UUID": encode_uuid,
            "CustomOutputName": "编码文本",
            "WFInput": _output_attachment(share_uuid, "分享内容"),
        }),
        _action("is.workflow.actions.url", {
            "UUID": url_uuid,
            "CustomOutputName": "解析页",
            "WFURLActionURL": _token_string(
                f"{base_url}/s?text={OBJ}",
                {
                    f"{{{len(base_url) + len('/s?text=')}, 1}}": {
                        "OutputUUID": encode_uuid,
                        "OutputName": "编码文本",
                        "Type": "ActionOutput",
                    },
                },
            ),
        }),
        _action("is.workflow.actions.openurl", {
            "WFInput": _output_attachment(url_uuid, "解析页"),
        }),
    ]

    workflow = {
        "WFWorkflowClientVersion": "1200",
        "WFWorkflowClientRelease": "2.2.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": "媒体解析",
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 946986751,
            "WFWorkflowIconGlyphNumber": 59785,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowTypes": ["ActionExtension", "NCWidget", "MenuBar", "QuickActions"],
        "WFQuickActionSurfaces": [],
        "WFWorkflowInputContentItemClasses": [
            "WFStringContentItem",
            "WFURLContentItem",
            "WFSafariWebPageContentItem",
            "WFRichTextContentItem",
        ],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowActions": actions,
    }
    return plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY)


def shortcut_dir():
    database = current_app.config.get("DATABASE") or ""
    base = os.path.dirname(database) if database else current_app.instance_path
    return os.path.join(base, "shortcuts")


def shortcut_override_path():
    return os.path.join(shortcut_dir(), OVERRIDE_FILENAME)


def override_status():
    path = shortcut_override_path()
    if not os.path.isfile(path):
        return {"present": False, "size": 0, "mtime_text": ""}
    stat = os.stat(path)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    return {
        "present": True,
        "size": stat.st_size,
        "mtime_text": mtime.strftime("%Y-%m-%d %H:%M"),
    }


def validate_shortcut_bytes(data):
    if not data or len(data) < 32:
        return "文件太小，不像快捷指令"
    if len(data) > MAX_OVERRIDE_BYTES:
        return f"文件超过 {MAX_OVERRIDE_BYTES // 1024} KB"
    if not (
        data.startswith(b"bplist")
        or data.startswith(b"<?xml")
        or data.startswith(b"<plist")
    ):
        return "请上传 iPhone 导出的 .shortcut 文件"
    try:
        workflow = plistlib.loads(data)
    except Exception:
        return "无法解析为快捷指令文件"
    if not isinstance(workflow, dict) or "WFWorkflowActions" not in workflow:
        return "不是有效的快捷指令（缺少工作流）"
    return None


def save_shortcut_override(data):
    error = validate_shortcut_bytes(data)
    if error:
        return error
    directory = shortcut_dir()
    os.makedirs(directory, exist_ok=True)
    path = shortcut_override_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return None


def clear_shortcut_override():
    path = shortcut_override_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def load_shortcut_bytes(base_url):
    path = shortcut_override_path()
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return handle.read()
    return build_shortcut_plist(base_url)


def normalize_icloud_install_url(raw):
    url = (raw or "").strip()
    if not url:
        return ""
    if not any(url.startswith(prefix) for prefix in ICLOUD_INSTALL_PREFIXES):
        raise ValueError(
            "请填写 iCloud 快捷指令分享链接（icloud.com/shortcuts 或 shortcuts.apple.com）"
        )
    return url
