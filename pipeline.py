# -*- coding: utf-8 -*-
"""量产编排：连续跑若干期，每期 fetch -> process 一条龙。

为什么是 subprocess 而不是 import：那两个脚本失败时会 sys.exit()，在同一个进程里
调用会把整批带崩。分开跑还顺带得到干净的单期失败隔离——一期挂了（比如 CBS 那天
没配字幕轨）不影响后面几期继续。

默认跳过本地已有 latest_cbs.json 的期，所以「补到 N 期」是幂等的：重复跑不会
重下 456MB，也不会重新花钱调 Claude。

用法:
  python pipeline.py --count 2                # 往前补 2 期（纯在线流：只下音频，跳过本地已有的）
  python pipeline.py --slug 092226-cbs-evening-news   # 只跑指定一期
  python pipeline.py --list                   # 看看哪些期本地已有、哪些还没有
  python pipeline.py --count 2 --with-video   # 显式要求同时下载本地 1080p 视频（约 440MB/期）
  python pipeline.py --count 1 --upload       # 跑完接着传云端（顺带刷 index.json）
  python pipeline.py --count 1 --upload --prune   # 传成功后删本地视频，腾 D 盘

必须用装了 yt-dlp 的解释器跑：Python310
"""
import argparse
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import db                                # noqa: E402
import fetch_cbs_video as fetch           # noqa: E402

PY = sys.executable
FETCH_SCRIPT = os.path.join(BASE_DIR, "fetch_cbs_video.py")
PROCESS_SCRIPT = os.path.join(BASE_DIR, "process_transcript.py")
BUILD_SCRIPT = os.path.join(BASE_DIR, "build_site.py")
SYNC_SCRIPT = os.path.join(BASE_DIR, "sync_r2.py")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def done_slugs():
    """本地已经跑完（有 latest_cbs.json）的期号。"""
    if not os.path.isdir(db.EPISODES_DIR):
        return set()
    return {s for s in os.listdir(db.EPISODES_DIR)
            if os.path.exists(db.episode_json_path(s))}


def pending_candidates(count):
    """列表页里最新的 count 期达标完整版，跳过本地已有的。新的在前。"""
    have = done_slugs()
    log(f"本地已有 {len(have)} 期: {', '.join(sorted(have)) or '（无）'}")
    slugs = fetch.list_episode_slugs()

    out = []
    for slug in slugs[:fetch.MAX_CANDIDATES + count + len(have)]:
        if len(out) >= count:
            break
        if slug in have:
            log(f"  跳过 {slug}（本地已有）")
            continue
        info = fetch.episode_info(slug)
        d = info["duration"]
        if d is None or not (fetch.MIN_DURATION <= d <= fetch.MAX_DURATION):
            log(f"  跳过 {slug}（{fetch.fmt_dur(d) if d else '没读到时长'}，不在 "
                f"{fetch.fmt_dur(fetch.MIN_DURATION)}~{fetch.fmt_dur(fetch.MAX_DURATION)} 内）")
            continue
        out.append(info)
    return out


def run_step(label, args):
    log(f"  → {label}")
    p = subprocess.run([PY, *args], cwd=BASE_DIR)
    return p.returncode == 0


def already_fetched(slug, no_video=False):
    """这一期的素材是不是已经下全了。

    fetch 那步会重下 456MB 视频（约 85 秒），半路挂掉重跑时没必要再来一遍——
    素材齐了就直接进翻译。要重下就删掉那一期的目录，或单独跑 fetch_cbs_video.py。
    """
    ep = os.path.join(db.EPISODES_DIR, slug)
    need = [os.path.join(ep, "info.json"), os.path.join(ep, "audio.mp3")]
    if not no_video:
        need.append(os.path.join(ep, "video.mp4"))
    return all(os.path.exists(p) for p in need)


def run_episode(info, no_video=False):
    slug = info["slug"]
    log(f"===== {slug}  {info['title']}（{fetch.fmt_dur(info['duration'])}）=====")

    t0 = time.time()
    if already_fetched(slug, no_video):
        log("  素材已在本地，跳过抓取")
    else:
        fetch_args = [FETCH_SCRIPT, "--slug", slug]
        if no_video:
            fetch_args.append("--no-video")
        label = "抓取（音频 + 源字幕）" if no_video else "抓取（音频 + 本地 1080p 视频 + 源字幕）"
        if not run_step(label, fetch_args):
            log(f"  ✗ {slug} 抓取失败，跳过这一期（后面几期继续）")
            _record(slug, "fetch", False, "fetch_cbs_video.py 退出码非 0")
            return False
        _record(slug, "fetch", True, None)

    if not run_step("字幕 + Claude 翻译 + 入库", [PROCESS_SCRIPT, "--slug", slug]):
        log(f"  ✗ {slug} 处理失败")
        return False

    log(f"  ✓ {slug} 完成，用时 {time.time() - t0:.0f} 秒")
    return True


def _record(slug, stage, ok, note):
    try:
        conn = db.connect()
        try:
            db.init_schema(conn)
            db.record_run(conn, slug, stage, ok=ok, note=note)
        finally:
            conn.close()
    except Exception as e:
        log(f"  （记 runs 流水失败，不影响主流程: {e}）")


def main():
    ap = argparse.ArgumentParser(description="连续跑若干期：fetch -> process -> 入库")
    ap.add_argument("--count", type=int, default=1, help="往前补几期（跳过本地已有的）")
    ap.add_argument("--slug", action="append", help="只跑指定期号，可重复传")
    ap.add_argument("--with-video", action="store_true",
                    help="同时下载本地 1080p 视频（默认纯在线流：只下音频，前端播 CBS 在线流）")
    ap.add_argument("--list", action="store_true", help="列出列表页的期次，标出本地已有")
    ap.add_argument("--upload", action="store_true",
                    help="跑完重建 dist/ 并传到对象存储（需要 R2_* 环境变量）")
    ap.add_argument("--prune", action="store_true",
                    help="配合 --upload：传成功后删本地视频回收 D 盘（删了本地就只剩云端一份）")
    args = ap.parse_args()
    if args.prune and not args.upload:
        ap.error("--prune 只在 --upload 成功之后才有意义，单独加它什么也不会发生")
    no_video = not args.with_video

    # fetch 模块里的 CURL/JS_RUNTIME 平时由它自己的 main() 填。这里借它的
    # episode_info() 抓单集页面，得先把这两个填上——CURL 为空会退回 urllib，
    # 而 CBS 对 urllib 会回 406。
    fetch.CURL = fetch.resolve_curl()
    fetch.JS_RUNTIME = fetch.resolve_js_runtime()

    if args.list:
        have = done_slugs()
        for slug in fetch.list_episode_slugs()[:fetch.MAX_CANDIDATES + 10]:
            info = fetch.episode_info(slug)
            d = info["duration"]
            ok = d is not None and fetch.MIN_DURATION <= d <= fetch.MAX_DURATION
            mark = "已跑" if slug in have else ("可跑" if ok else "  ")
            print(f"  [{mark}] {fetch.fmt_dur(d):>10}  {slug}")
        return

    if args.slug:
        infos = [fetch.episode_info(s) for s in args.slug]
    else:
        infos = pending_candidates(max(1, args.count))
        if not infos:
            log("列表页里没有需要补的期，收工")
            return

    log(f"准备跑 {len(infos)} 期: {', '.join(i['slug'] for i in infos)}"
        + ("（纯在线流，不下本地视频）" if no_video else ""))
    ok_n = 0
    for info in infos:
        if run_episode(info, no_video=no_video):
            ok_n += 1
    log(f"全部结束：成功 {ok_n}/{len(infos)} 期")

    if args.upload:
        if ok_n == 0:
            log("没有新跑完的期，跳上传")
        elif not run_step("生成静态站 dist/（含新的 index.json）", [BUILD_SCRIPT]):
            # 本地那一期已经跑完并入库了，只是没传上去，别让这一步把成果说成失败
            log("✗ 生成 dist/ 失败，跳上传（本地这期是好的，修好单独跑 build_site.py）")
            sys.exit(1)
        else:
            up = [SYNC_SCRIPT] + (["--prune"] if args.prune else [])
            if not run_step("上传到对象存储", up):
                log("✗ 上传失败（多半是 R2_* 凭证没配）—— 本地素材都在，"
                    "配好后单独重跑: python sync_r2.py")
                sys.exit(1)

    if ok_n < len(infos):
        sys.exit(1)


if __name__ == "__main__":
    main()
