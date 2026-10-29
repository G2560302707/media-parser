"""Simulate the iOS shortcut against the repo test set.

Calls the same parse contract the shortcut uses, then probes each media URL
the way Shortcuts' downloadURL would (iPhone Safari UA, optional Referer).
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1"
)

REFERER = {
    "抖音": "https://www.douyin.com/",
    "小红书": "https://www.xiaohongshu.com/",
    "哔哩哔哩": "https://www.bilibili.com/",
}

CASES = [
    (
        "抖音·视频",
        "9.25 复制打开抖音，看看【Moessif的作品】skill现已上线 Github搜索Moessif... https://v.douyin.com/jL1gk2Mu-GM/ X@M.WZ 06/25 :0pm anQ:/ 试试这个视频行不行，",
    ),
    (
        "抖音·图文",
        "6.43 复制打开抖音，看看【比个叶✌️（主页取图）的图文作品】快来找找你家女主吧～# 手机壁纸 # 壁纸 # i... https://v.douyin.com/lhXqmKU0PsM/ AGv:/ 03/09 :5pm g@B.GV ",
    ),
    (
        "抖音·动图",
        "5.35 复制打开抖音，看看【数学王小明（高考146）的图文作品】甩一张你的实况吧# 实况live动图 # live... https://v.douyin.com/ZtiyRAA2JLs/ :1pm S@Y.MJ JIi:/ 10/02 ",
    ),
    (
        "小红书·视频",
        "https://xhslink.cn/o/4ymF8iceAnT 前往【小红书】看看详情吧~",
    ),
    (
        "小红书·图文",
        "蓝色大肥鱼 蓝色大肥鱼拼豆图纸 https://xhslink.cn/o/2a2VE6z39N8 复制口令，直达【小红书】阅读好文~",
    ),
    (
        "小红书·动图",
        "男朋友装酷就这死出～ https://xhslink.cn/o/3GZxZcIYFuA 先存好口令，再去【小红书】解锁这篇笔记~",
    ),
    (
        "哔哩哔哩·视频",
        "https://b23.tv/4JtglDc",
    ),
]


def shortcut_plan(data):
    items = []
    videos = data.get("video_list") or []
    if isinstance(videos, list) and videos:
        for url in videos:
            if url:
                items.append(("video", url))
    elif data.get("video_url"):
        items.append(("video", data["video_url"]))
    for entry in data.get("image_list") or []:
        if isinstance(entry, dict):
            if entry.get("live_photo_url"):
                items.append(("live", entry["live_photo_url"]))
            elif entry.get("url"):
                items.append(("photo", entry["url"]))
        elif entry:
            items.append(("photo", entry))
    if data.get("audio_url"):
        items.append(("audio", data["audio_url"]))
    return items


def probe(url, referer):
    headers = {"User-Agent": IPHONE_UA}
    result = {"bare": None, "referer": None}
    try:
        resp = requests.get(url, headers=headers, timeout=12, stream=True, allow_redirects=True)
        chunk = next(resp.iter_content(2048), b"")
        result["bare"] = (resp.status_code, len(chunk), (resp.headers.get("Content-Type") or "")[:40])
        resp.close()
    except Exception as exc:
        result["bare"] = f"ERR {exc.__class__.__name__}"
    try:
        headers2 = dict(headers)
        if referer:
            headers2["Referer"] = referer
        resp = requests.get(url, headers=headers2, timeout=12, stream=True, allow_redirects=True)
        chunk = next(resp.iter_content(2048), b"")
        result["referer"] = (resp.status_code, len(chunk), (resp.headers.get("Content-Type") or "")[:40])
        resp.close()
    except Exception as extra:
        result["referer"] = f"ERR {extra.__class__.__name__}"
    return result


def ok_status(entry):
    if not isinstance(entry, tuple):
        return False
    return entry[0] == 200 and entry[1] > 0


def parse(base, key, text, timeout=60):
    kwargs = dict(
        json={"text": text},
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if base.startswith("https://"):
        kwargs["verify"] = False
    return requests.post(f"{base}/api/v1/parse", **kwargs)


def main():
    key = os.environ.get("MP_API_KEY", "").strip()
    if not key:
        print("set MP_API_KEY")
        sys.exit(2)
    base = os.environ.get("MP_BASE", "http://127.0.0.1:8765").rstrip("/")

    print(f"== shortcut contract  {base}/api/v1/parse  key={key[:6]}… ==")
    no_key = requests.post(
        f"{base}/api/v1/parse",
        json={"text": "https://v.douyin.com/abc/"},
        timeout=8,
        verify=not base.startswith("https://"),
    )
    print("no-key", no_key.status_code, no_key.json().get("error_code"), no_key.json().get("retdesc"))
    bad = parse(base, "mp-this-is-not-a-real-key-xx", "https://v.douyin.com/abc/")
    print("bad-key", bad.status_code, bad.json().get("error_code"), bad.json().get("retdesc"))
    empty = parse(base, key, "hello no url here")
    print("no-url", empty.status_code, empty.json().get("error_code"), empty.json().get("retdesc"))

    rows = []
    for name, text in CASES:
        started = time.monotonic()
        response = parse(base, key, text)
        payload = response.json() if response.content else {}
        elapsed = time.monotonic() - started
        data = payload.get("data") if payload.get("succ") else None
        plan = shortcut_plan(data or {})
        platform = (data or {}).get("platform") or "?"
        referer = REFERER.get(platform)
        print(f"\n== {name}  {elapsed:.1f}s  HTTP {response.status_code} succ={payload.get('succ')} {payload.get('error_code') or ''} ==")
        if not data:
            print("  ", payload.get("retdesc"))
            rows.append((name, False, 0, 0, payload.get("error_code") or payload.get("retdesc")))
            continue
        kinds = {}
        for kind, _url in plan:
            kinds[kind] = kinds.get(kind, 0) + 1
        print("  platform", platform, "author", (data.get("author") or {}).get("nickname"), "title", (data.get("title") or "")[:40])
        print("  shortcut would save", kinds or {"nothing": 0})
        image_list = data.get("image_list") or []
        if image_list:
            sample = image_list[0]
            print("  image_list[0] type", type(sample).__name__, "keys" if isinstance(sample, dict) else "", list(sample)[:4] if isinstance(sample, dict) else "")
        bare_ok = 0
        ref_ok = 0
        for kind, url in plan[:8]:
            probed = probe(url, referer)
            if ok_status(probed["bare"]):
                bare_ok += 1
            if ok_status(probed["referer"]):
                ref_ok += 1
            print(f"  {kind:5} bare={probed['bare']}  +referer={probed['referer']}")
        more = max(0, len(plan) - 8)
        if more:
            print(f"  ... {more} more URLs not probed")
        rows.append((name, True, len(plan), bare_ok, ref_ok, kinds))

    print("\n== shortcut save summary ==")
    for row in rows:
        print(" ", row)

    out = os.path.join(ROOT, "shortcuts", "testset-results.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"rows": rows}, handle, ensure_ascii=False, indent=2)
    print("wrote", out)


if __name__ == "__main__":
    main()
