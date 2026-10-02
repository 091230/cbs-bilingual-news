# -*- coding: utf-8 -*-
"""把本地这套东西打成一个**纯静态、和云端同构**的 dist/ 目录。

为什么要这一步：手机能不能看、云端能不能跑，取决于页面在"没有 /api、没有
serve.py 的动态路由"时还成不成立。所以这里生成的 dist/ 就是最终要传上去的那份
目录，本地用 `python serve.py --site` 打开的就是它 —— 预览过的就是部署的，
不存在"本地好好的、传上去就坏"。

目录结构（也就是 R2 上的 key 布局）::

    dist/index.html            列表页
    dist/player.html           播放页（注入了 window.STATIC_SITE = true）
    dist/index.json            期次列表（由 db.list_episodes() 生成）
    dist/assets/tailwind.css   预编译的 Tailwind
    dist/e/<slug>/subs_en.vtt
    dist/e/<slug>/subs_zh.vtt
    dist/e/<slug>/latest_cbs.json

**纯在线流模式：dist 里没有 video.mp4。** 视频直接播 CBS 的 HLS 在线流
（latest_cbs.json 里的 hls_master），本机不下 440MB、云端也不存视频 ——
dist 整个只有几 MB，随便一个免费静态托管都能装下。

用法:
  python build_site.py                  # 全量重建 dist/（含 Tailwind）
  python build_site.py --only 092426-cbs-evening-news
  python build_site.py --no-css         # 只重建 HTML/JSON，跳过 Tailwind
  python build_site.py --clean          # 先删掉 dist/ 再重建
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

import db

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EPISODES_DIR = os.path.join(BASE_DIR, "data", "episodes")
TAILWIND_EXE = os.path.join(BASE_DIR, "tools", "tailwindcss.exe")
TAILWIND_SRC = os.path.join(BASE_DIR, "assets", "tailwind.src.css")
TAILWIND_OUT = os.path.join(BASE_DIR, "assets", "tailwind.css")

HTML_FILES = ["index.html", "player.html"]
CSS_REL = os.path.join("assets", "tailwind.css")

# 这一串在 index.html / player.html 的 <head> 里原样存在，替换成 true 就切到静态模式
STATIC_FLAG_OFF = "window.STATIC_SITE = window.STATIC_SITE || false;"
STATIC_FLAG_ON = "window.STATIC_SITE = true;"

# build_site 从**磁盘上的文件**算指纹，而页面是按 URL 缓存的，所以只做"变了就换 URL"。
# ⚠️ 必须**按文件**分开算：整期共用一个 rev 的话，重洗一次字幕就会让 440MB 的
# video.mp4 跟着换 URL、缓存全部失效重下 —— 视频几乎不变，变的是字幕。
REV_LEN = 10


def rev_of(path):
    """文件内容的短指纹。分块读，别把 440MB 整个塞进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:REV_LEN]


def link_or_copy(src, dst):
    """优先硬链接（同盘、零额外空间），跨盘或文件系统不支持才退回复制。"""
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def build_css(force=False):
    """调 tools/tailwindcss.exe 重新生成 assets/tailwind.css。

    这台机器上没有 npm（node 在，但 npm 不在 PATH 上），所以用的是 Tailwind 官方
    的 standalone 单文件版。改完 HTML 一定要重跑 —— 它是**扫描源码里出现过的类名**
    才产出对应规则的，新加的类不重新扫就没有样式。
    """
    if not os.path.exists(TAILWIND_EXE):
        print(f"  ! 找不到 {TAILWIND_EXE}，跳过 CSS 生成（页面会没有样式）")
        return False
    newest_src = max([os.path.getmtime(TAILWIND_SRC), os.path.getmtime(
        os.path.join(BASE_DIR, "tailwind.config.js"))] +
        [os.path.getmtime(os.path.join(BASE_DIR, f)) for f in HTML_FILES])
    if not force and os.path.exists(TAILWIND_OUT) and os.path.getmtime(TAILWIND_OUT) > newest_src:
        print("  Tailwind 已是最新（--css-force 可强制重建）")
        return True
    cmd = [TAILWIND_EXE, "-c", os.path.join(BASE_DIR, "tailwind.config.js"),
           "-i", TAILWIND_SRC, "-o", TAILWIND_OUT, "--minify"]
    print("  生成 assets/tailwind.css ...")
    r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        print("  ! Tailwind 失败:\n" + (r.stderr or r.stdout).strip()[-800:])
        return False
    print(f"  完成 {os.path.getsize(TAILWIND_OUT) // 1024} KB")
    return True


def build_index_json(out_dir, slugs):
    """期次列表。直接复用 db.list_episodes()（db.py:249），别另写一份查询。"""
    rows = db.list_episodes()
    if slugs is not None:
        rows = [r for r in rows if r["slug"] in slugs]
    path = os.path.join(out_dir, "index.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"episodes": rows}, f, ensure_ascii=False, indent=1)
    return rows


def build_episode(slug, out_dir):
    """一期：两条字幕 + 一份"带指纹的 latest_cbs.json"。

    纯在线流：**不复制 video.mp4**。latest_cbs.json 里也不写 local_video，
    页面看到没有 local_video 就直接走 hls_master 在线流（player.html 的
    initPlayer 逻辑），不会先请求一个 404 再回退。
    """
    src_dir = os.path.join(EPISODES_DIR, slug)
    dst_dir = os.path.join(out_dir, "e", slug)
    os.makedirs(dst_dir, exist_ok=True)

    meta_path = os.path.join(src_dir, "latest_cbs.json")
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, encoding="utf-8") as f:
        payload = json.load(f)

    video = payload.setdefault("video", {})
    # 纯在线流：静态站没有本地视频文件，必须删掉 local_video 字段
    # （db 里可能还存着 video.mp4 文件名，那是本地 serve 模式用的）。
    video.pop("local_video", None)
    # local_subs_zh 原来不在 JSON 里（db.py:150 是写死的），静态站自己补上。
    video["local_subs"] = "subs_en.vtt"
    video["local_subs_zh"] = "subs_zh.vtt"
    payload["slug"] = slug

    copied, revs, missing = [], {}, []
    for field, fname in (("subs_en", "subs_en.vtt"),
                         ("subs_zh", "subs_zh.vtt")):
        s = os.path.join(src_dir, fname)
        d = os.path.join(dst_dir, fname)
        if not os.path.exists(s):
            missing.append(fname)
            continue
        copied.append(link_or_copy(s, d))
        revs[field] = rev_of(s)

    payload["revs"] = revs
    with open(os.path.join(dst_dir, "latest_cbs.json"), "w", encoding="utf-8") as f:
        # 缩进会把这几个文件从 ~150KB 吹到 ~400KB，而它们是每次播放都要下的
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return {"bytes": sum(os.path.getsize(os.path.join(dst_dir, n))
                         for n in os.listdir(dst_dir)),
            "revs": revs, "linked": copied.count("link"),
            "copied": copied.count("copy"), "missing": missing}


def main():
    ap = argparse.ArgumentParser(description="生成静态站 dist/（与云端同构）")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "dist"))
    ap.add_argument("--only", action="append", metavar="SLUG",
                    help="只构建这几期（可重复），目录结构仍按全站写")
    ap.add_argument("--no-css", action="store_true", help="跳过 Tailwind 生成")
    ap.add_argument("--css-force", action="store_true", help="强制重建 Tailwind")
    ap.add_argument("--clean", action="store_true", help="先删掉输出目录")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    if args.clean and os.path.isdir(out_dir):
        # 只删自己生成的那几个条目，别把用户往 dist/ 里放的东西一起端了
        for name in ("index.html", "player.html", "index.json", "assets", "e"):
            p = os.path.join(out_dir, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
    os.makedirs(out_dir, exist_ok=True)

    if not args.no_css:
        build_css(force=args.css_force)

    # ---- HTML：复制 + 把静态开关打开
    for name in HTML_FILES:
        with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
            html = f.read()
        if STATIC_FLAG_OFF not in html:
            print(f"  ! {name} 里找不到 STATIC_SITE 开关，静态模式下它会去请求 /api 而失败")
        html = html.replace(STATIC_FLAG_OFF, STATIC_FLAG_ON)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(html)

    # ---- assets
    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    if os.path.exists(TAILWIND_OUT):
        shutil.copy2(TAILWIND_OUT, os.path.join(assets_dir, "tailwind.css"))
    else:
        print("  ! assets/tailwind.css 不存在，先跑一次不带 --no-css 的构建")

    # ---- 每期
    slugs = None
    if args.only:
        slugs = set(args.only)
        bad = [s for s in slugs if not os.path.isdir(os.path.join(EPISODES_DIR, s))]
        for s in bad:
            print(f"  ! 没有这一期: {s}")
            return 1

    total = 0
    built = []
    for slug in sorted(os.listdir(EPISODES_DIR)) if os.path.isdir(EPISODES_DIR) else []:
        if slugs is not None and slug not in slugs:
            continue
        info = build_episode(slug, out_dir)
        if info is None:
            continue
        built.append(slug)
        total += info["bytes"]
        note = ""
        if info["missing"]:
            note = "  缺: " + ",".join(info["missing"])
        print(f"  {slug}  {info['bytes'] / 1e6:.0f}MB  "
              f"硬链接{info['linked']}/复制{info['copied']}{note}")

    rows = build_index_json(out_dir, set(built) if slugs is not None else None)
    print(f"\n构建完成: {out_dir}")
    print(f"  期次 {len(rows)} 条 -> index.json")
    print(f"  实际占用 {total / 1e6:.2f} MB（纯在线流，不含视频文件）")
    print(f"  本地预览: python serve.py --site")
    print(f"  上传云端: python sync_r2.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
