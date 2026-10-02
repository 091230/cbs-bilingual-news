# -*- coding: utf-8 -*-
"""把 build_site.py 生成的 dist/ 传到对象存储（Cloudflare R2，或任何 S3 兼容服务）。

**传上去的就是 localhost 上预览过的那份目录** —— 本地 `python serve.py --site`
打开的是它，云端 Worker 背后也是它，所以不存在"本地好好的、传上去就坏"。

走的是标准 S3 接口，所以 R2 / 阿里云 OSS / 腾讯云 COS / MinIO 都能用，换家只改
`R2_ENDPOINT`（和 bucket 名），脚本一行不用动。

⚠️ 凭证一律**只从环境变量读，绝不写进代码**：

    R2_ENDPOINT           https://<账号ID>.r2.cloudflarestorage.com
                          （阿里云 OSS 香港：https://oss-cn-hongkong.aliyuncs.com）
    R2_ACCESS_KEY_ID      S3 兼容的 Access Key ID
    R2_SECRET_ACCESS_KEY  对应的 Secret
    R2_BUCKET             桶名

用法:
  python sync_r2.py --dry-run            # 不联网、不需要密钥，先看要传什么
  python sync_r2.py                      # 传（远端已存在且大小一致就跳过）
  python sync_r2.py --only <期号>
  python sync_r2.py --prune              # 传完删本地视频，把 D 盘空间腾回来
  python sync_r2.py --force              # 不比对，全部重传
"""
import argparse
import concurrent.futures
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(BASE_DIR, "dist")
EPISODES_DIR = os.path.join(BASE_DIR, "data", "episodes")

# ⚠️ text/vtt 必须显式指定，和 serve.py:35 补 mimetypes 是同一个坑：
# 默认给 application/octet-stream 的话浏览器直接拒收 <track>，画面上一条字幕都没有。
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
}

# 视频/音频的 URL 里带内容指纹（?v=<rev>，见 build_site.py），等于内容寻址，
# 所以可以放心长缓存。字幕和 JSON 改得勤，短缓存，靠 ?v= 兜底。
VIDEO_LIKE = {".mp4", ".mp3", ".m4a"}
CACHE_VIDEO = "public, max-age=604800, immutable"
CACHE_TEXT = "public, max-age=300"


def guess_type(path):
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext, "application/octet-stream"), \
        (CACHE_VIDEO if ext in VIDEO_LIKE else CACHE_TEXT)


def collect(src_dir, only=None):
    """遍历 dist/，产出 (本地绝对路径, R2 key, 大小, Content-Type, Cache-Control)。

    key 一律用正斜杠 —— Windows 上 os.path.relpath 给的是反斜杠，
    直接当 key 传上去会在桶里建出一堆名字里带 "\\" 的对象。
    """
    items = []
    for root, dirs, files in os.walk(src_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            key = os.path.relpath(path, src_dir).replace(os.sep, "/")
            if only and key.startswith("e/"):
                # e/<slug>/xxx -> slug
                slug = key.split("/")[1] if len(key.split("/")) > 2 else ""
                if slug not in only:
                    continue
            ctype, cache = guess_type(path)
            items.append({"path": path, "key": key,
                          "size": os.path.getsize(path),
                          "type": ctype, "cache": cache})
    return items


def human(n):
    return f"{n / 1e6:.1f}MB" if n >= 1e6 else f"{n / 1e3:.0f}KB"


def dry_run(items):
    print(f"将上传 {len(items)} 个对象（--dry-run，未联网）：\n")
    total = 0
    for it in items:
        total += it["size"]
        print(f"  {it['key']:<46} {human(it['size']):>9}  {it['type']:<28} {it['cache']}")
    print(f"\n合计 {human(total)}")
    # 视频是大头，单独点出来，免得以为"才几百兆"就能传得飞快
    vid = sum(i["size"] for i in items if i["type"].startswith("video/"))
    if vid:
        print(f"其中视频 {human(vid)} —— 上传耗时取决于本机**上行**带宽，不是下行")


def make_client(endpoint, key_id, secret):
    try:
        import boto3
    except ImportError:
        sys.exit("缺少 boto3，先跑: python -m pip install boto3")
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",                    # R2 用 auto；OSS/COS 填实际 region
        config=Config(
            signature_version="s3v4",
            # 默认是 legacy 的 MD5 校验，某些 S3 兼容服务不接受，改成只校验必填项
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 4, "mode": "standard"},
        ),
    )


def remote_size(client, bucket, key):
    """远端对象大小；不存在返回 None（head_object 用异常表达"没有"）。"""
    try:
        return client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload_one(client, bucket, it):
    client.upload_file(
        it["path"], bucket, it["key"],
        ExtraArgs={"ContentType": it["type"], "CacheControl": it["cache"]},
    )
    # 440MB 的视频走 multipart，传完确认一下远端大小，别把 "--prune 删了本地却没传上去"
    # 这种事留到明天才发现
    got = remote_size(client, bucket, it["key"])
    if got != it["size"]:
        raise RuntimeError(f"远端大小 {got} != 本地 {it['size']}")
    return it


def prune_local(uploaded_keys):
    """把已经确认传上去的视频从本地删掉，腾 D 盘空间。

    ⚠️ 要删**两处**：data/episodes/<slug>/video.mp4 和 dist/ 里那个硬链接。
    硬链接只是同一份数据的另一个目录项，只删一处一个字节都回收不了。

    删完这一期的本地视频就没了 —— 本地预览那期会打不开视频（页面会自动退回
    在线流）。**R2 上的那份是唯一副本了**，所以这个开关只在"确认传上去了"之后用。
    """
    freed, seen = 0, set()
    for slug in sorted({k.split("/")[1] for k in uploaded_keys
                        if k.startswith("e/") and k.endswith("/video.mp4")}):
        for p in (os.path.join(EPISODES_DIR, slug, "video.mp4"),
                  os.path.join(DEFAULT_SRC, "e", slug, "video.mp4")):
            if not os.path.exists(p):
                continue
            st = os.stat(p)
            # 硬链接共用 (dev, inode)：两个目录项其实是同一份数据，只能算一次，
            # 否则报出来的"回收了 X GB"会翻倍
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                freed += st.st_size
            os.remove(p)
        print(f"  本地已删 {slug}/video.mp4（R2 上还在）")
    return freed


def main():
    ap = argparse.ArgumentParser(description="把 dist/ 同步到对象存储（R2 / S3 兼容）")
    ap.add_argument("--src", default=DEFAULT_SRC, help="要传的目录（默认 dist/）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不联网、不需要密钥")
    ap.add_argument("--only", action="append", metavar="SLUG", help="只传这几期（可重复）")
    ap.add_argument("--force", action="store_true", help="不比对远端，全部重传")
    ap.add_argument("--prune", action="store_true", help="传成功后删本地视频，回收磁盘")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--endpoint", help="覆盖 R2_ENDPOINT")
    ap.add_argument("--bucket", help="覆盖 R2_BUCKET")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        sys.exit(f"没有 {src}\n先跑一次: python build_site.py")

    only = set(args.only) if args.only else None
    items = collect(src, only)
    if not items:
        sys.exit(f"{src} 里没有可传的文件 —— 先跑 python build_site.py")

    if args.dry_run:
        dry_run(items)
        return 0

    endpoint = args.endpoint or os.environ.get("R2_ENDPOINT")
    bucket = args.bucket or os.environ.get("R2_BUCKET")
    key_id = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    missing = [n for n, v in (("R2_ENDPOINT", endpoint), ("R2_BUCKET", bucket),
                              ("R2_ACCESS_KEY_ID", key_id),
                              ("R2_SECRET_ACCESS_KEY", secret)) if not v]
    if missing:
        sys.exit("缺少环境变量: " + ", ".join(missing) +
                 "\n（凭证只从环境变量读，不要写进代码或命令行 —— 会留在 shell 历史里）")

    client = make_client(endpoint, key_id, secret)
    print(f"目标 {bucket} @ {endpoint}")

    todo, skipped = [], 0
    if not args.force:
        for it in items:
            try:
                if remote_size(client, bucket, it["key"]) == it["size"]:
                    skipped += 1
                    continue
            except Exception as e:
                print(f"  ! 查询 {it['key']} 失败（当作需要重传）: {e}")
            todo.append(it)
    else:
        todo = items

    print(f"待传 {len(todo)} 个，跳过 {skipped} 个（远端已有且大小一致）")
    done, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(upload_one, client, bucket, it): it for it in todo}
        for f in concurrent.futures.as_completed(futs):
            it = futs[f]
            try:
                f.result()
                done.append(it)
                print(f"  ✓ {it['key']}  {human(it['size'])}")
            except Exception as e:
                failed.append((it, e))
                print(f"  ✗ {it['key']}  {type(e).__name__}: {e}")

    print(f"\n成功 {len(done)}，失败 {len(failed)}")
    if failed:
        # 有失败就绝不 prune：那等于把本地唯一副本删了而云端没有
        print("有失败项，跳过 --prune。修好重跑（已成功的大小一致会自动跳过）。")
        return 1
    if args.prune:
        freed = prune_local({i["key"] for i in done})
        print(f"已于本地回收约 {freed / 1e9:.2f} GB（这一期以后只在云端有视频）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
