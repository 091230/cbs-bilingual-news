# -*- coding: utf-8 -*-
"""本地静态服务器 + 一个只读 JSON API，比 python -m http.server 多两件事。

1. **Range 支持**。为什么不能直接用 python -m http.server：它不认 Range 请求，不管
   你要哪一段都回 200 + 整个文件。本地视频有 456MB，<video> 每次往前拖进度条都要从
   0 重下，字幕一点就卡住。这里补上 206 Partial Content，拖动才是秒开的。

2. **/api 路由**。播放列表和每期的数据从 data/cbs.db 读，不直接暴露 JSON 文件：
   - GET /api/episodes            -> {"episodes": [...]}  列表页用
   - GET /api/episodes/<slug>     -> 一期完整数据          播放页用
   视频/字幕还是走静态文件（Range 用得上）。

用 ThreadingHTTPServer 而不是单线程的：视频是持续长连接，单线程会把字幕/JSON
请求全堵在后面。也正因为多线程，db 那边是每个请求新开一个连接。

用法:
  python serve.py                     # 默认 8000 端口，服务整个仓库（有 /api）
  python serve.py 8080
  python serve.py --site              # 服务 build_site.py 生成的 dist/，与云端同构（无 /api）
  python serve.py --site --lan 8001   # 再绑 0.0.0.0，同一个 WiFi 下的手机/平板能打开

⚠️ 默认只绑 127.0.0.1，手机上打不开 —— 要真机测就得加 --lan，而且 --lan 必须配
--site：不加 --site 时服务的是整个仓库，会把 cbs.db 和各类缓存一起暴露到局域网。
"""
import json
import mimetypes
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import db

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Python 自带的 MIME 表里没有 .vtt，会回 application/octet-stream，
# 而 Chrome 只认 text/vtt —— 不补这一行，字幕轨会被浏览器直接拒收。
mimetypes.add_type("text/vtt", ".vtt")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
API_PREFIX = "/api/"


class RangeHandler(SimpleHTTPRequestHandler):
    server_version = "CBSStudy/1.0"

    # ------------------------------------------------------------------
    # /api —— 从 SQLite 读，交给父类之前先拦下来
    # ------------------------------------------------------------------
    def do_GET(self):
        if self._api():
            return
        super().do_GET()

    def do_HEAD(self):
        if self._api():
            return
        super().do_HEAD()

    def _api(self):
        """是 /api 请求就处理掉并返回 True。绝不会落到 send_head()——那边会把它
        当文件路径去 translate_path，最后回一个 HTML 的 404。"""
        path = urlparse(self.path).path
        if not path.startswith(API_PREFIX):
            return False
        self._api_response = True
        rest = path[len(API_PREFIX):].strip("/")
        try:
            if rest == "episodes":
                self._send_json({"episodes": db.list_episodes()})
            elif rest.startswith("episodes/"):
                slug = unquote(rest[len("episodes/"):])
                episode = db.get_episode(slug)
                if episode is None:
                    self._send_json({"error": "no such episode", "slug": slug}, 404)
                else:
                    self._send_json(episode)
            else:
                self._send_json({"error": "unknown endpoint", "path": path}, 404)
        except Exception as e:
            # 库还没建、或者某一期还没 ingest，都从这里出去，别让服务端 500 成 HTML
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        return True

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 必须用编码后的字节数：中文字符串的 len() 是字符数，差一倍会让连接挂住
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def end_headers(self):
        # 只给静态文件声明 Range。给 JSON 也发的话，中间的代理/缓存会以为可以拿片段。
        if not getattr(self, "_api_response", False) \
                and not getattr(self, "_range_header_sent", False):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        """有 Range 就自己处理，其余照旧交给父类。"""
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not self.headers.get("Range"):
            return super().send_head()

        m = RANGE_RE.match(self.headers["Range"].strip())
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        if not m:
            f.close()
            return super().send_head()

        size = os.fstat(f.fileno()).st_size
        a, b = m.group(1), m.group(2)
        if a == "":                       # bytes=-N 表示最后 N 字节
            if not b:
                f.close()
                return super().send_head()
            start, end = max(0, size - int(b)), size - 1
        else:
            start = int(a)
            end = min(int(b), size - 1) if b else size - 1

        if start >= size or start > end:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        self._range_left = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self._range_header_sent = True
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(self._range_left))
        self.send_header("Last-Modified", self.date_time_string(os.fstat(f.fileno()).st_mtime))
        self.end_headers()
        f.seek(start)
        return f

    def copyfile(self, source, outputfile):
        """只推 Range 要的那一段，别把整个文件灌过去。"""
        left = getattr(self, "_range_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            buf = source.read(min(256 * 1024, left))
            if not buf:
                break
            outputfile.write(buf)
            left -= len(buf)

    def log_message(self, fmt, *args):
        # 视频会刷出成百上千条 206，只记非视频请求，免得把终端淹了
        if ".mp4" not in (self.path or ""):
            super().log_message(fmt, *args)


def lan_ip():
    """本机在局域网里的地址。用 UDP 连一下就问到，不会真的发包、也不依赖 DNS。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    argv = [a for a in sys.argv[1:]]
    # --site：服务 build_site.py 生成的 dist/。那是**和云端同构**的那份目录
    # （没有 /api，数据走静态 index.json），手机上会遇到的问题只有在这里才能复现。
    site = "--site" in argv
    # --lan：绑 0.0.0.0，让同一个 WiFi 下的手机/平板能打开。必须配 --site ——
    # 不加 --site 时服务的是整个仓库，里面有 cbs.db 和各类缓存，不该往外露。
    lan = "--lan" in argv
    argv = [a for a in argv if a not in ("--site", "--lan")]
    port = int(argv[0]) if argv else 8000

    if lan and not site:
        print("--lan 得跟 --site 一起用（不然会把 db.py、cbs.db 这些也暴露到局域网）\n"
              "  python serve.py --site --lan 8001")
        return 1

    root = os.path.join(BASE_DIR, "dist") if site else BASE_DIR
    if site and not os.path.isdir(root):
        print(f"还没有 {root}\n先跑一次: python build_site.py")
        return 1

    host = "0.0.0.0" if lan else "127.0.0.1"
    handler = partial(RangeHandler, directory=root)
    srv = ThreadingHTTPServer((host, port), handler)
    base = f"http://127.0.0.1:{port}"
    print(f"服务目录: {root}" + ("   [静态站模式，与云端同构]" if site else ""))
    if not site:
        print(f"数据库  : {db.DB_PATH}")
    print(f"期次列表: {base}/index.html")
    print(f"播放页  : {base}/player.html?slug=<期号>")
    if not site:
        print(f"API     : {base}/api/episodes")
    if lan:
        ip = lan_ip()
        print(f"局域网  : http://{ip or '<本机IP>'}:{port}/index.html"
              f"   ← 手机/平板用这个（必须在同一个 WiFi 下）")
        # 第一次绑 0.0.0.0 时 Windows 会弹防火墙询问；点了"取消"就是手机一直转圈
        print("          手机打不开的话看 Windows 防火墙有没有放行 python.exe")
    print(f"（Range 已启用，Ctrl+C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
