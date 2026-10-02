# -*- coding: utf-8 -*-
"""抖音视频批量下载：复用本机 Edge 登录态，自动选最高画质(1080p)并合并音视频。

用法: python douyin_download.py [txt路径]
  txt 每行一个抖音链接（含 modal_id），默认读 C:/Users/lenovo/Desktop/wangzhi.txt
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import imageio_ffmpeg

BASE_DIR = "D:/investment/douyin_download"
TMP_FULL = os.path.join(BASE_DIR, "_full.mp4")
TMP_AUDIO = os.path.join(BASE_DIR, "_audio.mp4")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")


def log(msg):
    print(msg, flush=True)


def read_video_ids(txt_path):
    """从 txt 提取所有 modal_id（去重，保序）。"""
    ids = []
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            m = re.search(r"modal_id=(\d+)", line)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def collect_bit_rates(obj, out):
    """递归收集所有清晰度档位 {gear_name, bit_rate, url}。"""
    if isinstance(obj, dict):
        if "gear_name" in obj:
            url = None
            pa = obj.get("play_addr") or {}
            ul = pa.get("url_list") or [] if isinstance(pa, dict) else []
            if ul:
                url = ul[0]
            out.append({"gear_name": obj.get("gear_name"),
                        "bit_rate": obj.get("bit_rate"), "url": url})
        for v in obj.values():
            collect_bit_rates(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_bit_rates(v, out)


def check_has_audio(path):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True, encoding="utf-8")
    return "Audio:" in r.stderr


def download_file(url, path, label):
    """流式下载到本地，支持大文件，避免整块读入内存。"""
    log(f"[*] 下载{label} ...")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    total = 0
    last_log = 0.0
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 206):
            raise RuntimeError(f"{label} HTTP {r.status}")
        clen = r.headers.get("Content-Length")
        clen = int(clen) if clen and clen.isdigit() else None
        with open(path, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                now = time.time()
                if now - last_log >= 3:
                    if clen:
                        log(f"    {total/1024/1024:.0f}/{clen/1024/1024:.0f} MB "
                            f"({total*100//clen}%)")
                    else:
                        log(f"    {total/1024/1024:.0f} MB")
                    last_log = now
    if clen and total != clen:
        raise RuntimeError(f"{label} 下载不完整: {total}/{clen} 字节")
    log(f"    完成 {total/1024/1024:.1f} MB")


async def download_one(context, video_id, idx, total):
    """下载单个视频，返回 True 成功 / False 失败。"""
    OUTPUT = os.path.join(BASE_DIR, f"{idx}.mp4")
    if os.path.exists(OUTPUT) and os.path.getsize(OUTPUT) > 0:
        log(f"[{idx}/{total}] {video_id} 已存在，跳过")
        return True

    video_url = f"https://www.douyin.com/video/{video_id}"
    page = await context.new_page()
    bits = []
    audio_url = None

    async def on_response(resp):
        nonlocal audio_url
        url = resp.url
        if "aweme/v1/web/aweme/detail" in url:
            try:
                collect_bit_rates(json.loads(await resp.body()), bits)
            except Exception:
                pass
        elif "media-audio" in url and audio_url is None:
            audio_url = url

    page.on("response", on_response)

    try:
        log(f"[{idx}/{total}] 打开 {video_url}")
        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"  页面加载异常(继续): {e}")

        await page.wait_for_timeout(4000)
        for sel in ["video", ".xgplayer-video", "[data-e2e='video-player'] video", "canvas"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(force=True, timeout=2000)
                    break
            except Exception:
                continue
        await page.wait_for_timeout(6000)

        seen, uniq = set(), []
        for b in bits:
            k = (b["gear_name"], b["bit_rate"], (b["url"] or "")[:60])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(b)

        log(f"  解析到 {len(uniq)} 个清晰度档位")
        if audio_url:
            # 捕获到音频轨：选 DASH 视频轨（media-video），后续与音频轨合并
            cand = [b for b in uniq if b["url"] and "1080" in (b["gear_name"] or "") and "media-video" in b["url"]]
            if not cand:
                cand = [b for b in uniq if b["url"] and "media-video" in b["url"]]
        else:
            # 未捕获到音频轨：选完整地址（muxed，自带音频），避免无声
            cand = [b for b in uniq if b["url"] and "media-video" not in b["url"]]
        if not cand:
            cand = [b for b in uniq if b["url"]]
        cand.sort(key=lambda b: ("hvc1" in b["url"], -(b["bit_rate"] or 0)))

        if not cand:
            log(f"  [!] {video_id} 没有可用地址，跳过")
            await page.close()
            return False

        best = cand[0]
        log(f"  选择: {best['gear_name']}  {best['bit_rate']/1000:.0f} kbps")

        await asyncio.to_thread(download_file, best["url"], TMP_FULL, "高清视频")

        final_output = OUTPUT
        if os.path.exists(OUTPUT):
            try:
                os.remove(OUTPUT)
            except OSError:
                final_output = os.path.join(BASE_DIR, f"{idx}_{best['gear_name']}.mp4")

        if check_has_audio(TMP_FULL):
            os.replace(TMP_FULL, final_output)
        else:
            if audio_url:
                await asyncio.to_thread(download_file, audio_url, TMP_AUDIO, "音频轨")
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                subprocess.run([ffmpeg, "-y", "-i", TMP_FULL, "-i", TMP_AUDIO,
                                "-c", "copy", "-movflags", "+faststart", final_output],
                               check=True, capture_output=True)
                os.remove(TMP_FULL)
                os.remove(TMP_AUDIO)
            else:
                os.replace(TMP_FULL, final_output)

        size = os.path.getsize(final_output) / 1024 / 1024
        log(f"  [+] 完成 {os.path.basename(final_output)} ({size:.1f} MB)")
        await page.close()
        return True
    except Exception as e:
        log(f"  [!] {video_id} 失败: {e}")
        try:
            await page.close()
        except Exception:
            pass
        return False


async def main():
    txt_path = sys.argv[1] if len(sys.argv) > 1 else "C:/Users/lenovo/Desktop/wangzhi.txt"
    ids = read_video_ids(txt_path)
    log(f"[*] 共 {len(ids)} 个视频")
    os.makedirs(BASE_DIR, exist_ok=True)

    from playwright.async_api import async_playwright

    ok = fail = 0
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="msedge", headless=False)
        except Exception as e:
            log(f"[!] Edge 启动失败: {e}  回退 Chrome...")
            browser = await p.chromium.launch(channel="chrome", headless=False)

        context = await browser.new_context(user_agent=UA)

        for i, vid in enumerate(ids, 1):
            try:
                if await download_one(context, vid, i, len(ids)):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                log(f"[{i}/{len(ids)}] {vid} 异常: {e}")
                fail += 1

        await browser.close()

    log(f"\n[=] 完成：成功 {ok}，失败 {fail}，共 {len(ids)}")


if __name__ == "__main__":
    asyncio.run(main())
