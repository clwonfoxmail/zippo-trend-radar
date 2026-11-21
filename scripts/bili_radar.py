"""
B站 Zippo 热度扫描器
- 用 wbi 签名调用 B站公开搜索接口（低频，每日 1~2 次）
- 按「播放量 + 发布时间」筛出真正有热度的内容
- 去重后输出 markdown，可推送到企业微信群机器人

用法:
    python bili_radar.py            # 只打印
    python bili_radar.py --send     # 打印并推送（读 WEWORK_WEBHOOK_URL）
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── 配置区 ──────────────────────────────────────────────
KEYWORDS = [
    "Zippo",
    "煤油打火机",
    "打火机改装",
    "铜打火机",
    "EDC打火机",
    "打火机 手工",
]
MIN_PLAY = 2000          # 播放量门槛（热度下限）
DAYS_BACK = 14           # 只看最近多少天
TOP_N = 6                # 最多推送几条
MUST_RE = re.compile(r"zippo|打火机|煤油机|火机|edc", re.I)
NOISE_RE = re.compile(r"剃须|刀片|眉刀|游戏|抽卡|表情包|香烟|电子烟|戒烟", re.I)
# ────────────────────────────────────────────────────────


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _wbi_keys(session: requests.Session):
    session.get("https://www.bilibili.com/", headers={"User-Agent": UA}, timeout=15)
    r = session.get("https://api.bilibili.com/x/web-interface/nav",
                    headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                    timeout=15)
    d = r.json()["data"]["wbi_img"]
    return (d["img_url"].rsplit("/", 1)[-1].split(".")[0],
            d["sub_url"].rsplit("/", 1)[-1].split(".")[0])


def _sign(params: dict, img_key: str, sub_key: str) -> dict:
    mk = _mixin_key(img_key + sub_key)
    p = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    p["w_rid"] = hashlib.md5((urllib.parse.urlencode(p) + mk).encode()).hexdigest()
    return p


def search(keyword: str, session: requests.Session, keys, page: int = 1):
    params = {"search_type": "video", "keyword": keyword, "page": page,
              "order": "pubdate"}
    r = session.get("https://api.bilibili.com/x/web-interface/wbi/search/type",
                    params=_sign(params, *keys),
                    headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                    timeout=20)
    data = r.json().get("data") or {}
    return data.get("result") or []


def clean(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t).replace("&quot;", '"').strip()


def collect() -> list:
    s = requests.Session()
    keys = _wbi_keys(s)
    cutoff = datetime.now() - timedelta(days=DAYS_BACK)
    seen, out = set(), []
    for kw in KEYWORDS:
        try:
            items = search(kw, s, keys)
        except Exception as e:
            print(f"[warn] {kw}: {e}", file=sys.stderr)
            continue
        print(f"[info] {kw} -> {len(items)} 条")
        for it in items:
            if it.get("type") != "video":
                continue
            bvid = it.get("bvid")
            if not bvid or bvid in seen:
                continue
            title = clean(it.get("title", ""))
            if not MUST_RE.search(title) or NOISE_RE.search(title):
                continue
            try:
                play = int(it.get("play") or 0)
                pub = datetime.fromtimestamp(int(it.get("pubdate") or 0))
            except (ValueError, OSError):
                continue
            if play < MIN_PLAY or pub < cutoff:
                continue
            seen.add(bvid)
            out.append({
                "title": title,
                "play": play,
                "danmaku": int(it.get("video_review") or 0),
                "author": it.get("author", ""),
                "pub": pub.strftime("%m-%d"),
                "url": f"https://www.bilibili.com/video/{bvid}",
            })
        time.sleep(1.2)
    out.sort(key=lambda x: x["play"], reverse=True)
    return out[:TOP_N]


def render(items: list) -> str:
    if not items:
        return ""
    lines = [f"## 📺 B站 Zippo 热度（近 {DAYS_BACK} 天，播放 >{MIN_PLAY}）", ""]
    for i, it in enumerate(items, 1):
        lines.append(f"**{i}. [{it['title']}]({it['url']})**")
        lines.append(f"> {it['play']:,} 播放 · {it['danmaku']} 弹幕 · {it['author']} · {it['pub']}")
    return "\n".join(lines)


def send_md(md: str):
    url = os.environ.get("WEWORK_WEBHOOK_URL", "").strip()
    if not url:
        print("[skip] 未设置 WEWORK_WEBHOOK_URL", file=sys.stderr)
        return
    r = requests.post(url, json={"msgtype": "markdown",
                                 "markdown": {"content": md}}, timeout=20)
    print("[send]", r.json())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    items = collect()
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        md = render(items) or f"（近 {DAYS_BACK} 天没有播放量 >{MIN_PLAY} 的 Zippo 相关内容）"
        print(md)
        if a.send:
            send_md(md)
