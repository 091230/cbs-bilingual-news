# -*- coding: utf-8 -*-
"""从已部署的 GitHub Pages 拉回缺失期次的 latest_cbs.json + 字幕 vtt。"""
import os, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")

BASE = r"D:\investment\douyin_download\data\episodes"
LIVE = "https://091230.github.io/cbs-bilingual-news"
SLUGS = ["092526-cbs-evening-news", "092926-cbs-evening-news",
         "093026-cbs-evening-news", "100226-cbs-evening-news"]

proxy = urllib.request.ProxyHandler({"https": "http://127.0.0.1:7897",
                                     "http": "http://127.0.0.1:7897"})
opener = urllib.request.build_opener(proxy)

for slug in SLUGS:
    d = os.path.join(BASE, slug)
    os.makedirs(d, exist_ok=True)
    for fname in ("latest_cbs.json", "subs_en.vtt", "subs_zh.vtt"):
        url = f"{LIVE}/e/{slug}/{fname}"
        dest = os.path.join(d, fname)
        try:
            with opener.open(url, timeout=30) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            print(f"  ok {slug}/{fname}  {len(data)//1024}KB")
        except Exception as e:
            print(f"  ! {slug}/{fname}: {e}")
