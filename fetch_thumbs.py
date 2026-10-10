# -*- coding: utf-8 -*-
"""为每期抓取 CBS og:image 封面图，存到 assets/thumbs/<slug>.jpg。"""
import os, re, subprocess, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROXY = "http://127.0.0.1:7897"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")
BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "assets", "thumbs")
os.makedirs(OUT_DIR, exist_ok=True)

SLUGS = [
    "091826-cbs-evening-news", "092326-cbs-evening-news", "092426-cbs-evening-news",
    "092526-cbs-evening-news", "092926-cbs-evening-news", "093026-cbs-evening-news",
    "100126-cbs-evening-news", "100226-cbs-evening-news",
    "100526-cbs-evening-news", "100626-cbs-evening-news", "100726-cbs-evening-news",
    "100826-cbs-evening-news", "100926-cbs-evening-news",
]

def http_get(url):
    cmd = ["curl", "-sSL", "--compressed", "--max-time", "60",
           "-H", f"User-Agent: {UA}",
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: en-US,en;q=0.9",
           "-x", PROXY, url]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout

def download(url, dest):
    cmd = ["curl", "-sSL", "--compressed", "--max-time", "60",
           "-H", f"User-Agent: {UA}", "-x", PROXY,
           "-o", dest, url]
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 1000

results = {}
for slug in SLUGS:
    out = os.path.join(OUT_DIR, slug + ".jpg")
    if os.path.exists(out) and os.path.getsize(out) > 5000:
        print(f"  = {slug} 已存在 ({os.path.getsize(out)//1024}KB)")
        results[slug] = "assets/thumbs/" + slug + ".jpg"
        continue
    url = f"https://www.cbsnews.com/video/{slug}/"
    html = http_get(url)
    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
    if not m:
        m = re.search(r'"thumbnailUrl":"([^"]+)"', html)
    if not m:
        # 试 imageSrc
        m = re.search(r'"imageSrc":"([^"]+)"', html)
    if not m:
        print(f"  ! {slug} 没找到 og:image (页面 {len(html)} 字节)")
        continue
    img = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
    print(f"  > {slug}  {img[:80]}")
    if download(img, out):
        print(f"    ok {os.path.getsize(out)//1024}KB")
        results[slug] = "assets/thumbs/" + slug + ".jpg"
    else:
        print(f"    下载失败")

print("\n--- 结果 ---")
for k, v in results.items():
    print(f'  "{k}": "{v}",')
