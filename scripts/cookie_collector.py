"""Cookie 凭据采集与同步工具（独立黑窗口程序，非服务端组件）。

在本机运行：弹浏览器人工登录 → 自动抓取 Cookie → 字段校验 →
写入本地库 / 打印 / 一键推送到远程服务器后台。

用法：
    python scripts/cookie_collector.py                    # 全交互：选平台→登录→选去向
    python scripts/cookie_collector.py -p douyin          # 跳过平台选择
    python scripts/cookie_collector.py -p weibo --print   # 只打印不落库
    python scripts/cookie_collector.py -p weibo --push https://srv.example.com --user admin

依赖（一次性安装）：
    pip install playwright
    python -m playwright install chromium
"""

import argparse
import getpass
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.cookie_registry import (  # noqa: E402
    COOKIE_REGISTRY,
    check_cookie_fields,
    get_cookie_value,
)

DB_PATH = PROJECT_ROOT / "data" / "media_parser.db"
PROFILE_ROOT = PROJECT_ROOT / "data" / ".cookie_profile"

# 默认推送服务器（生产部署地址），交互选择"推送到远程服务器"时回车即用
DEFAULT_SERVER = "https://after7lab.cloud:81"

# 各平台登录入口（采集哪个平台的 Cookie 就打开哪个站，人工登录）
SITE_URLS = {
    "douyin": "https://www.douyin.com",
    "yuanbao": "https://yuanbao.tencent.com",
    "pinduoduo": "https://mobile.pinduoduo.com",
    "doubao": "https://www.doubao.com",
    "xiaohongshu": "https://www.xiaohongshu.com",
    "kuaishou": "https://www.kuaishou.com",
    "weibo": "https://weibo.com",
    "jimeng": "https://jimeng.jianying.com",
}

DEFAULT_TIMEOUT = 300  # 等待人工登录的最长时间（秒）


def _fail(message):
    print(f"\n[×] {message}")
    sys.exit(1)


def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _fail(
            "缺少 playwright 依赖。请先执行:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        )


def _pick_platform(args):
    if args.platform:
        if args.platform not in COOKIE_REGISTRY:
            _fail(f"未注册的平台: {args.platform}（可选: {'、'.join(COOKIE_REGISTRY)}）")
        return args.platform
    print("\n选择要采集的平台:")
    keys = list(COOKIE_REGISTRY)
    for i, key in enumerate(keys, 1):
        entry = COOKIE_REGISTRY[key]
        required = "、".join(entry["required_fields"]) or "任意字段（兜底凭据）"
        print(f"  {i}. {entry['platform_name']:<14} 必需字段: {required}")
    raw = input("\n输入编号 [1-8]: ").strip()
    try:
        return keys[int(raw) - 1]
    except (ValueError, IndexError):
        _fail("无效的编号")


def _wait_for_login(context, platform_key):
    """轮询 Cookie，等待该平台必需字段齐备；无必需字段的平台等人工回车确认。"""
    entry = COOKIE_REGISTRY[platform_key]
    required = [f for f in entry["required_fields"]]
    deadline = time.time() + DEFAULT_TIMEOUT
    print(f"\n[…] 在弹出的浏览器里登录 {entry['platform_name']}（最长等待 {DEFAULT_TIMEOUT // 60} 分钟）")
    print("    登录成功后脚本会自动继续，无需其他操作。")
    while time.time() < deadline:
        cookies = {c["name"]: c["value"] for c in context.cookies(SITE_URLS[platform_key]) if c["value"]}
        if required:
            missing = [f for f in required if not cookies.get(f)]
            if not missing:
                print(f"[√] 登录态检测通过（{'、'.join(required)} 齐备）")
                return cookies
        elif cookies:
            input("\n    登录完成后按回车继续 > ")
            return cookies
        time.sleep(2)
    _fail("等待登录超时，已退出。可重新运行再试。")


def _hunt_uifid(context, page):
    """抖音特例：从网络请求与 localStorage 里嗅探 UIFID（实况动轨需要）。"""
    found = {"value": ""}

    def _on_request(request):
        if not found["value"] and "uifid=" in request.url:
            found["value"] = request.url.split("uifid=")[1].split("&")[0].split(",")[0]

    page.on("request", _on_request)
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if not found["value"]:
            found["value"] = page.evaluate(
                """() => {
                    for (let i = 0; i < localStorage.length; i++) {
                        const v = localStorage.getItem(localStorage.key(i)) || '';
                        const m = v.match(/"uifid"\\s*:\\s*"([^"]{16,})"/) || v.match(/uifid[=:]([A-Za-z0-9+/=]{16,})/);
                        if (m) return m[1];
                    }
                    return '';
                }"""
            ) or ""
    except Exception as exc:
        print(f"[!] UIFID 探测失败（不影响基础凭据）: {exc}")
    finally:
        page.remove_listener("request", _on_request)
    return found["value"]


def _collect(platform_key):
    """弹浏览器采集，返回 (cookie_str, pairs_count)。"""
    from playwright.sync_api import sync_playwright

    profile_dir = PROFILE_ROOT / platform_key
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,  # 登录必须人工参与
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()
        page.goto(SITE_URLS[platform_key], wait_until="domcontentloaded")
        cookies = _wait_for_login(browser, platform_key)
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

        if platform_key == "douyin":
            uifid = _hunt_uifid(browser, page)
            if uifid and "UIFID=" not in cookie_str:
                cookie_str += f"; UIFID={uifid}"
                print("[√] 已捕获 UIFID（实况动轨能力可用）")
            elif not uifid:
                print("[!] 未捕获 UIFID：实况动轨能力将缺失，其余能力不受影响")
        browser.close()
    return cookie_str, cookie_str.count("=")


def _validate(platform_key, cookie_str):
    result = check_cookie_fields(platform_key, cookie_str)
    if result["status"] == "ok":
        print("[√] 字段校验通过，凭据完整")
    elif result["status"] == "incomplete":
        print(f"[!] 字段校验: 缺 {', '.join(result['missing'])}")
        print(f"    平台要求: {COOKIE_REGISTRY[platform_key]['config_hint']}")
    return result["status"]


def _save_local(platform_key, cookie_str):
    entry = COOKIE_REGISTRY[platform_key]
    if not DB_PATH.exists():
        _fail(f"找不到数据库 {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    con.execute(
        f"INSERT INTO system_settings(`key`, `value`) VALUES(?, ?) "
        f"ON CONFLICT(`key`) DO UPDATE SET `value`=excluded.`value`",
        (entry["db_keys"][0], cookie_str),
    )
    con.commit()
    con.close()
    print(f"[√] 已写入本地数据库 {entry['db_keys'][0]}（保存即生效，无需重启）")


def _push_remote(platform_key, cookie_str, server, token=""):
    """一键推送到远程服务器免登录通道：/admin/api/cookies/push（令牌可选）。"""
    import requests

    server = server.rstrip("/")
    payload_data = {"platform": platform_key, "value": cookie_str}
    if token:
        payload_data["token"] = token
    try:
        r = requests.post(
            f"{server}/admin/api/cookies/push",
            data=payload_data,
            headers={"User-Agent": "cookie-collector/1.0"},
            timeout=20,
        )
    except requests.RequestException as e:
        _fail(f"无法连接 {server}: {e}")
    if r.status_code == 404:
        _fail("服务器未提供免登录推送接口，请升级服务端镜像后重试")
    if r.status_code == 401:
        _fail("推送令牌错误或缺失：服务器 .env 配置了 COOKIE_PUSH_TOKEN，需传入相同令牌")
    try:
        payload = r.json()
    except ValueError:
        _fail(f"服务器响应异常（HTTP {r.status_code}），请确认地址正确")
    if payload.get("succ"):
        print(f"[√] 已推送到 {server}（{COOKIE_REGISTRY[platform_key]['platform_name']}，状态: {payload['data']['status']}）")
    else:
        _fail(f"推送被拒绝: {payload.get('message')}")


def main():
    parser = argparse.ArgumentParser(description="Cookie 凭据采集与同步工具")
    parser.add_argument("-p", "--platform", choices=COOKIE_REGISTRY, help="跳过平台选择")
    parser.add_argument("--print", action="store_true", help="只打印 Cookie 不写库")
    parser.add_argument("--env", action="store_true", help="输出 export XXX_COOKIE=...")
    parser.add_argument("--push", metavar="URL", nargs="?", const=DEFAULT_SERVER,
                        help=f"采集后推送到指定服务器后台（不带值时默认 {DEFAULT_SERVER}）")
    parser.add_argument("--token", default="", help="推送令牌（服务器 .env 配置了 COOKIE_PUSH_TOKEN 时必填）")
    args = parser.parse_args()

    platform_key = _pick_platform(args)
    print(f"\n=== 采集 {COOKIE_REGISTRY[platform_key]['platform_name']} Cookie ===")
    _ensure_playwright()
    cookie_str, count = _collect(platform_key)
    print(f"\n[√] 采集完成，共 {count} 个字段")
    _validate(platform_key, cookie_str)

    if args.print:
        print("\n--- Cookie 字符串 ---\n" + cookie_str)
        return
    if args.env:
        env_key = COOKIE_REGISTRY[platform_key]["env_keys"][0]
        print(f'\nexport {env_key}="{cookie_str}"')
        return
    if args.push:
        token = args.token or getpass.getpass("推送令牌（未配置可回车跳过）: ").strip()
        _push_remote(platform_key, cookie_str, args.push, token)
        return

    # 全交互：选去向
    print("\n同步去向:")
    print("  1. 写入本地数据库（保存即生效）")
    print("  2. 只打印 Cookie 字符串")
    print("  3. 输出 export 环境变量")
    print("  4. 推送到远程服务器后台")
    choice = input("选择 [1-4, 默认 1]: ").strip() or "1"
    if choice == "1":
        _save_local(platform_key, cookie_str)
    elif choice == "2":
        print("\n--- Cookie 字符串 ---\n" + cookie_str)
    elif choice == "3":
        env_key = COOKIE_REGISTRY[platform_key]["env_keys"][0]
        print(f'\nexport {env_key}="{cookie_str}"')
    elif choice == "4":
        server = input(f"服务器地址（回车默认 {DEFAULT_SERVER}）: ").strip() or DEFAULT_SERVER
        token = getpass.getpass("推送令牌（未配置可回车跳过）: ").strip()
        _push_remote(platform_key, cookie_str, server, token)
    else:
        _fail("无效选择")


if __name__ == "__main__":
    main()
