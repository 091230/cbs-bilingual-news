# -*- coding: utf-8 -*-
"""抓取 CBS Evening News 完整版（来源：cbsnews.com 官网），压成极轻量音频。

为什么走官网而不是 YouTube：YouTube 对 /watch 有 IP 级风控，走代理时报
"Sign in to confirm you're not a bot"，换 player_client 也绕不过；CBS 官网同一条片
源干净可用，且直接提供 audio-only 音轨（hls-audio_aac-English），下载量更小。

用法:
  python fetch_cbs_video.py                  # 自动抓官网最新一期 15 分钟以上完整版
  python fetch_cbs_video.py <cbs_url>        # 抓指定单集
  python fetch_cbs_video.py --slug 092326-cbs-evening-news   # 按期号抓（pipeline 用这个）
  python fetch_cbs_video.py --count 3        # 连续抓最近 3 期
  python fetch_cbs_video.py --slug <期号> --subs-only   # 只补源字幕（不重下视频）
  python fetch_cbs_video.py --list           # 只列出候选单集与时长，不下载
  python fetch_cbs_video.py --list-all       # 连 15 分钟以下的片段也一起列出来

输出（每期一个目录，互不覆盖）:
  data/episodes/<slug>/audio.mp3      16kHz / 32kbps / 单声道（20 分钟约 5MB）
  data/episodes/<slug>/video.mp4      本地 1080p（内嵌英文 mov_text 字幕轨）
  data/episodes/<slug>/subs_en.vtt    源字幕轨（前端 CC 用）
  data/episodes/<slug>/info.json      {slug, video_id, title, duration, ...}

依赖: yt-dlp、imageio-ffmpeg（系统 PATH 里有 ffmpeg 则优先用系统的）
必须用装了 yt-dlp 的解释器跑：Python310
"""
import argparse
import glob
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin

import cbs_subs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
EPISODES_DIR = os.path.join(DATA_DIR, "episodes")

# 下面这几个是**按期**路径，由 set_episode() 在 main() 里按 slug 重新绑定。
# 以前是单槽位固定路径（data/video.mp4、video_info.json …），每抓一期就覆盖上一期，
# 做播放列表必须先按期分目录。顺带把缓存也隔开——process_transcript.py 里的
# claude_raw.json 只按"切句数"判有效，跨期会错误复用（两期都切出 249 句就撞车，
# 白花钱还翻错内容）。
EP_DIR = None
OUT_AUDIO = None
OUT_INFO = None
OUT_VIDEO = None
OUT_SUBS = None
RAW_TEMPLATE = None


def set_episode(slug):
    """把这一期的所有输入输出路径指到 data/episodes/<slug>/ 下。"""
    global EP_DIR, OUT_AUDIO, OUT_INFO, OUT_VIDEO, OUT_SUBS, RAW_TEMPLATE
    EP_DIR = os.path.join(EPISODES_DIR, slug)
    os.makedirs(EP_DIR, exist_ok=True)
    OUT_AUDIO = os.path.join(EP_DIR, "audio.mp3")
    OUT_INFO = os.path.join(EP_DIR, "info.json")
    OUT_VIDEO = os.path.join(EP_DIR, "video.mp4")
    OUT_SUBS = os.path.join(EP_DIR, "subs_en.vtt")
    RAW_TEMPLATE = os.path.join(EP_DIR, "_raw_audio.%(ext)s")
    return EP_DIR

# ---------------------------------------------------------------------------
# 代理配置
# 本机 Clash 的 HTTP/SOCKS 混合端口是 7897（Clash Verge 默认，不是 7890）。
# 改端口就改这一行；也可以临时用环境变量覆盖：CBS_PROXY=http://127.0.0.1:7890
# 不需要代理时把 PROXY 设成 None 即可。
# ---------------------------------------------------------------------------
PROXY = os.environ.get("CBS_PROXY", "http://127.0.0.1:7897")

CBS_LIST_URL = "https://www.cbsnews.com/evening-news/full-episodes/"
CBS_HOST = "https://www.cbsnews.com"

# 完整版单集的 slug 形如 092326-cbs-evening-news（MMDDYY + 栏目名）。
# 单条新闻片段的 slug 是描述性文字（如 girl-celebrating-6th-birthday-...），靠这个能干净分开。
# 想连周末版一起收，把后面的 cbs-evening-news 换成 (cbs-evening-news|cbs-weekend-news)。
EPISODE_SLUG_RE = re.compile(r"^\d{6}-cbs-evening-news$")

# 时长窗口。官网完整版实测 19:58（"15min以上"按你说的取 900 秒）；
# 片段类最长 3~4 分钟，所以 15 分钟这条线能把两者分开。
MIN_DURATION = 15 * 60
MAX_DURATION = 70 * 60
MAX_CANDIDATES = 8   # 最多往下翻几个候选去找达标的单集

AUDIO_AR = 16000
AUDIO_BITRATE = "32k"
AUDIO_CHANNELS = 1

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")

JS_RUNTIME = None  # 由 resolve_js_runtime() 在 main() 里填
CURL = None        # 由 resolve_curl() 在 main() 里填
FFMPEG = None      # 由 resolve_ffmpeg() 在 main() 里填
FETCH_DELAY = 2    # 单集页面之间歇一下，连续猛拉会被 CBS 临时封 IP


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg):
    log(f"错误: {msg}")
    sys.exit(1)


def fmt_size(path):
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    return f"{n / 1024 / 1024:.2f} MB"


def fmt_dur(sec):
    if not sec:
        return "未知"
    sec = int(sec)
    return f"{sec // 60}分{sec % 60:02d}秒"


def resolve_curl():
    """用 curl 取页面。实测同样走代理、同样 UA，urllib 会被 CBS 回 406，curl 正常 200。"""
    exe = shutil.which("curl")
    if exe:
        return exe
    log("警告: 没找到 curl，退回 urllib（可能被 CBS 回 406）")
    return None


def http_get(url, attempts=3, soft=False):
    """取网页，失败退避重试。soft=True 时失败返回 None 而不是直接退出。"""
    last = None
    for i in range(attempts):
        if CURL:
            cmd = [CURL, "-sSL", "--compressed", "--max-time", "60",
                   "-H", f"User-Agent: {UA}",
                   "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "-H", "Accept-Language: en-US,en;q=0.9"]
            if PROXY:
                cmd += ["-x", PROXY]
            cmd.append(url)
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if p.returncode == 0 and p.stdout:
                return p.stdout
            last = (p.stderr or "").strip()[:200] or f"curl 退出码 {p.returncode}"
        else:
            handlers = []
            if PROXY:
                handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            try:
                with opener.open(req, timeout=45) as r:
                    return r.read().decode("utf-8", errors="replace")
            except Exception as e:
                last = e

        if i < attempts - 1:
            wait = 4 * (2 ** i)
            log(f"抓取失败（{last}），{wait} 秒后重试…")
            time.sleep(wait)

    if soft:
        return None
    die(f"抓取失败 {url}: {last}")


def parse_iso_duration(s):
    """PT0H19M58S -> 1198"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + sec


def list_episode_slugs():
    """从完整版列表页拿所有单集 slug，保持页面顺序（最新的在前）。"""
    log(f"抓列表页: {CBS_LIST_URL}")
    html = http_get(CBS_LIST_URL)
    # 页面里的 URL 是 JSON 转义过的（https:\/\/...\/video\/slug\/），先还原
    flat = html.replace("\\/", "/")
    slugs, seen = [], set()
    for m in re.finditer(r"/video/([a-z0-9\-]+)/", flat):
        s = m.group(1)
        if EPISODE_SLUG_RE.match(s) and s not in seen:
            seen.add(s)
            slugs.append(s)
    log(f"列表页共找到 {len(slugs)} 期完整版")
    return slugs


def episode_info(slug, delay=True):
    """抓单集页面，解析标题与时长。"""
    url = f"{CBS_HOST}/video/{slug}/"
    if delay:
        time.sleep(FETCH_DELAY)
    html = http_get(url)

    title = None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        title = m.group(1)
    if not title:
        m = re.search(r'"name":"([^"]{4,120})"', html)
        title = m.group(1) if m else slug

    duration = None
    m = re.search(r'"duration":"(PT[^"]+)"', html)   # JSON-LD，最可靠
    if m:
        duration = parse_iso_duration(m.group(1))
    if not duration:
        m = re.search(r'"duration":(\d+)', html)
        if m:
            duration = int(m.group(1))

    m = re.search(r'"id":"([0-9a-f\-]{36})"', html)
    m2 = re.search(r'"uploadDate":"([^"]+)"', html)
    return {
        "slug": slug,
        "video_id": m.group(1) if m else None,
        "title": title,
        "duration": duration,
        "upload_date": m2.group(1) if m2 else None,
        "channel": "CBS Evening News",
        "webpage_url": url,
    }


def pick_episodes(count):
    """按列表顺序往下找 count 期 15 分钟以上的完整版。"""
    slugs = list_episode_slugs()
    if not slugs:
        die("列表页里没解析到任何完整版单集，官网结构可能改了")

    picked = []
    for slug in slugs[:MAX_CANDIDATES + count]:
        if len(picked) >= count:
            break
        info = episode_info(slug)
        d = info["duration"]
        if d is None:
            log(f"跳过 {slug}（页面里没读到时长）")
            continue
        if d < MIN_DURATION:
            log(f"跳过 {slug}（{fmt_dur(d)}，不足 {fmt_dur(MIN_DURATION)}）")
            continue
        if d > MAX_DURATION:
            log(f"跳过 {slug}（{fmt_dur(d)}，超过 {fmt_dur(MAX_DURATION)}，像直播回放）")
            continue
        picked.append(info)
    if len(picked) < count:
        die(f"只找到 {len(picked)} 期 {fmt_dur(MIN_DURATION)} 以上的完整版，凑不齐 {count} 期")
    return picked


def pick_episode():
    return pick_episodes(1)[0]


def resolve_ffmpeg():
    """优先用系统 PATH 里的 ffmpeg，否则退回 imageio-ffmpeg 自带的二进制。"""
    global FFMPEG
    exe = shutil.which("ffmpeg")
    if exe:
        log(f"使用系统 ffmpeg: {exe}")
    else:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            log(f"使用 imageio-ffmpeg 自带 ffmpeg: {exe}")
        except ImportError:
            die("找不到 ffmpeg，也没有装 imageio-ffmpeg。请 pip install imageio-ffmpeg 或把 ffmpeg 加进 PATH。")
    FFMPEG = exe
    return exe


def resolve_js_runtime():
    """yt-dlp 2025 起解析需要 JS 运行时（默认只认 deno）。本机装的是 node。"""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            log(f"JS 运行时: {name}")
            return name
    log("警告: 没找到 deno/node/bun，解析可能受影响，建议装 node")
    return None


def ytdlp_cmd(args):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-color", "--ignore-config"]
    if PROXY:
        cmd += ["--proxy", PROXY]
    if JS_RUNTIME:
        cmd += ["--js-runtimes", JS_RUNTIME]
    if FFMPEG:
        # 不指定的话 yt-dlp 找不到 PATH 外的 ffmpeg，HLS 下载会报 malformed AAC timestamps
        cmd += ["--ffmpeg-location", FFMPEG]
    return cmd + args


def ytdlp_env():
    env = os.environ.copy()
    if PROXY:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = env["ALL_PROXY"] = PROXY
    return env


def run_ytdlp(args, attempts=3):
    """跑一条 yt-dlp 命令，代理通过 --proxy 和环境变量双份注入，带退避重试。"""
    cmd = ytdlp_cmd(args)
    env = ytdlp_env()

    last = ""
    for i in range(attempts):
        p = subprocess.run(cmd, text=True, encoding="utf-8",
                           errors="replace", env=env)
        if p.returncode == 0:
            return
        last = (p.stderr or "").strip()
        if i < attempts - 1:
            wait = 5 * (2 ** i)
            log(f"yt-dlp 失败，{wait} 秒后重试（第 {i + 2}/{attempts} 次）…")
            time.sleep(wait)

    die("yt-dlp 失败:\n  " + "\n  ".join(last.splitlines()[-6:]))


def download_audio(info):
    log(f"下载纯音频轨: {info['title']}")
    for f in glob_raw():
        os.remove(f)
    run_ytdlp([
        # CBS 的 HLS 里有独立的 audio-only 轨，优先取它，避免下整段视频
        "-f", "hls-audio_aac-English/bestaudio/best",
        "--no-playlist", "-o", RAW_TEMPLATE,
        "--newline", "--progress", info["webpage_url"],
    ])
    files = glob_raw()
    if not files:
        die("音频下载完成但找不到输出文件")
    raw = max(files, key=os.path.getsize)
    log(f"原始音频: {os.path.basename(raw)}  {fmt_size(raw)}")
    return raw


def glob_raw():
    import glob
    return [f for f in glob.glob(RAW_TEMPLATE.replace("%(ext)s", "*"))
            if not f.endswith(".part")]


def resolve_hls(info):
    """取 HLS master manifest 和字幕轨地址 -> (master_url, subs_url)。

    yt-dlp 列出的每个 format 都在 `.../<id>_hls/<rendition>/stream.m3u8` 下，
    往上退两级就是同一套流的 master.m3u8 —— 里面才有各档画质和独立音轨组，
    直接拿 rendition 的 m3u8 喂播放器会只有画面没声音。

    字幕不用另外找：master 里就有一条 `#EXT-X-MEDIA:TYPE=SUBTITLES`，指向 CBS
    自己封好的 WebVTT。有它就不用跑语音识别了 —— 官方听打稿，文字和时间轴都更准。
    """
    try:
        p = subprocess.run(
            ytdlp_cmd(["--dump-json", "--no-warnings", info["webpage_url"]]),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=ytdlp_env(), timeout=180)
        if p.returncode != 0:
            log("警告: 取 HLS 流地址失败，前端播放器会没有视频源")
            return None, None
        data = json.loads(p.stdout)
    except (subprocess.SubprocessError, ValueError) as e:
        log(f"警告: 解析 HLS 流地址失败（{e}），前端播放器会没有视频源")
        return None, None

    master = None
    for f in data.get("formats") or []:
        url = f.get("url") or ""
        if "m3u8" in (f.get("protocol") or "") and url.endswith("/stream.m3u8"):
            master = url.rsplit("/", 2)[0] + "/master.m3u8"
            break
    if not master:
        log("警告: 没在格式列表里找到 HLS 流")
        return None, None

    subs = None
    text = http_get(master, soft=True)
    if text:
        m = re.search(r'#EXT-X-MEDIA:TYPE=SUBTITLES[^\n]*?URI="([^"]+)"', text)
        if m:
            subs = urljoin(master, m.group(1))
        else:
            log("警告: master 里没有字幕轨（这期可能没配），只能退回语音识别")
    else:
        log("警告: 读不到 master manifest，字幕轨地址未知")
    return master, subs


def download_video(info):
    """把整期视频下到本地，给前端播放器用。

    为什么要下到本地：到 CBS CDN 的单条 TCP 连接只有约 1.2 Mbps（RTT 限制，
    不是带宽不够），而 1080p 要 3.19 Mbps。hls.js 是串行下分片的，所以在线播放
    永远卡在 432p 左右；单连接测 1.19、4 并发测 3.40，说明瓶颈在连接数。
    所以这里开 --concurrent-fragments 4 多连接下载，实测能到 2.8~3.4 Mbps。
    下到本地还有个额外好处：拖动进度条是秒开的（serve.py 带了 Range 支持）。
    """
    os.makedirs(EP_DIR, exist_ok=True)
    tmp = os.path.join(EP_DIR, "_video.%(ext)s")
    for f in _glob_video_tmp():
        os.remove(f)

    log("下载 1080p 视频到本地（4 路并发，约 20 分钟 / 456MB）…")
    t0 = time.time()
    run_ytdlp([
        "-f", "bv*+ba/b", "-S", "res:1080,fps",
        "--concurrent-fragments", "4",
        "--merge-output-format", "mp4",
        "--force-overwrites", "--no-playlist",
        "-o", tmp, "--newline", "--progress",
        info["webpage_url"],
    ])

    got = _glob_video_tmp()
    if not got:
        log("警告: 视频下载完成但找不到输出文件")
        return None
    src = max(got, key=os.path.getsize)
    os.replace(src, OUT_VIDEO)
    log(f"视频就绪: {OUT_VIDEO}  {fmt_size(OUT_VIDEO)}  用时 {time.time() - t0:.0f} 秒")
    return OUT_VIDEO


def _glob_video_tmp():
    import glob
    return [f for f in glob.glob(os.path.join(EP_DIR, "_video.*"))
            if not f.endswith(".part")]


def fetch_subs(info):
    """把源字幕轨单独存一份成 data/episodes/<slug>/subs_en.vtt。

    以前只取 `bv*+ba`，等于把 CBS 自带的字幕轨丢了——下下来的视频没有字幕，
    前端也没得显示。现在顺着 master.m3u8 里那条字幕轨直接拉分片（见 cbs_subs），
    拉下来既是前端 <track> 的 CC，也嵌进 mp4 给 VLC 那类播放器用。

    这里**不能**用 `yt-dlp --write-subs`：yt-dlp 的 CBS 提取器有时列不出字幕轨
    （09-24 那期就报了 "There are no subtitles for the requested languages"），
    于是那一期静默地没字幕；而 master 里的字幕轨地址是稳定在的。

    落盘的是**合并成句子级**的版本，不是广播显示级的原始分条：原始分条是滚动式的，
    同一句会连着显示两三遍（09-24 有 131/401 句带重复），跟读时很干扰；句子级还
    正好和中文轨同一套时间轴，中英同开不会一行在句中错位。
    """
    subs_url = info.get("hls_subs")
    if not subs_url:
        log("警告: 这期取不到字幕轨地址，视频将不带字幕（前端也少一条 CC）")
        return None

    log("下载源字幕轨 …")
    t0 = time.time()
    try:
        cues = cbs_subs.fetch_cues(subs_url)
    except (OSError, ValueError, http.client.HTTPException) as e:
        log(f"警告: 源字幕取用失败（{type(e).__name__}: {e}），视频将不带字幕")
        return None

    rows = cbs_subs.merge_cues(cues)
    with open(OUT_SUBS, "w", encoding="utf-8", newline="\n") as f:
        # 英文轨贴顶（见 cbs_subs.CUE_TOP），中文轨不写、留在默认的底部
        f.write(cbs_subs.build_vtt(rows, settings=cbs_subs.CUE_TOP))
    log(f"源字幕就绪: {os.path.basename(OUT_SUBS)}  {fmt_size(OUT_SUBS)}"
        f"（{len(cues)} 条 cue 合为 {len(rows)} 句，用时 {time.time() - t0:.1f} 秒）")
    return OUT_SUBS


def embed_subs(video):
    """把这一期的 subs_en.vtt 作为软字幕轨嵌进 mp4（mov_text，可开关）。

    -map 写死了 `0:v / 0:a / 1:0`：只搬原视频的画面和声音，源文件里原有的字幕轨
    一概不要。所以对同一期重复跑是安全的——不会一条条越嵌越多。

    纯流复制，不重编码，456MB 大概几秒。mp4 里嵌的是 mov_text，浏览器不认，
    所以前端另挂了一条 <track> 指同一个 vtt。
    """
    if not os.path.exists(OUT_SUBS):
        return False
    tmp = os.path.join(EP_DIR, "_embed.mp4")
    log("把字幕嵌进视频 …")
    p = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video, "-i", OUT_SUBS,
        "-map", "0:v", "-map", "0:a", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
        "-disposition:s:0", "default", "-movflags", "+faststart",
        tmp,
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        log("警告: 字幕嵌入失败，视频仍可用（前端那条 <track> 不受影响）: "
            + (p.stderr or "").strip()[:300])
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, video)
    return True


def compress(raw, ffmpeg):
    """转码成 16kHz / 32kbps 单声道 MP3。20 分钟约 5MB。"""
    log(f"压缩转码 -> {AUDIO_AR // 1000}kHz / {AUDIO_BITRATE} / 单声道 MP3 …")
    p = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-i", raw, "-vn",
         "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_AR),
         "-b:a", AUDIO_BITRATE, "-codec:a", "libmp3lame",
         OUT_AUDIO],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not os.path.exists(OUT_AUDIO):
        die("ffmpeg 转码失败: " + (p.stderr or "").strip()[:500])


def fetch_one(info, ffmpeg, no_video=False):
    """抓一期：音频 -> 本地 1080p 视频 -> 源字幕 -> 嵌字幕 -> 写 info.json。"""
    set_episode(info["slug"])
    log(f"本期目录: {EP_DIR}")
    info["hls_master"], info["hls_subs"] = resolve_hls(info)
    if info["hls_master"]:
        log(f"HLS 流地址: {info['hls_master']}")
    if info["hls_subs"]:
        log(f"字幕轨: {info['hls_subs']}（有它就不用跑本地语音识别了）")

    t0 = time.time()
    raw = download_audio(info)
    compress(raw, ffmpeg)

    os.remove(raw)
    log(f"已清理原始音频 {os.path.basename(raw)}")

    info["audio_file"] = os.path.basename(OUT_AUDIO)
    info["audio_size_mb"] = round(os.path.getsize(OUT_AUDIO) / 1024 / 1024, 2)
    info["audio_format"] = f"{AUDIO_AR}Hz {AUDIO_BITRATE} mono mp3"

    # 视频下载是这一步的大头（约 20 分钟）。失败就退到在线流，不影响已经拿到的音频。
    if no_video:
        info["local_video"] = None
        log("按 --no-video 跳过本地视频，前端会走在线流")
    else:
        try:
            path = download_video(info)
            info["local_video"] = os.path.basename(path) if path else None
            info["video_size_mb"] = (round(os.path.getsize(path) / 1024 / 1024, 1)
                                     if path else None)
            # 字幕单独取一份再嵌回去：只下 bv*+ba 会把源字幕轨丢掉
            if path:
                try:
                    if fetch_subs(info):
                        info["local_subs"] = os.path.basename(OUT_SUBS)
                        embed_subs(path)
                except SystemExit:
                    log("警告: 源字幕没取到，视频将不带字幕（前端也少一条 CC）")
        except SystemExit:
            log("警告: 视频下载失败，前端会退回在线流播放")
            info["local_video"] = None

    with open(OUT_INFO, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    log("完成 —— 耗时 %.1f 秒" % (time.time() - t0))
    print(f"  视频 ID : {info['video_id']}")
    print(f"  标题    : {info['title']}")
    print(f"  时长    : {fmt_dur(info['duration'])}")
    print(f"  音频    : {OUT_AUDIO}  ({info['audio_size_mb']} MB)")
    print(f"  视频流  : {info.get('hls_master') or '（没取到，前端播放器会没画面）'}")
    print(f"  字幕轨  : {info.get('hls_subs') or '（没取到，只能跑本地语音识别）'}")
    if info.get("local_video"):
        print(f"  本地视频: {info['local_video']}  ({info.get('video_size_mb')} MB)")
    if info.get("local_subs"):
        print(f"  本地字幕: {info['local_subs']}  (已嵌进 mp4 + 前端 CC)")
    print(f"  元数据  : {OUT_INFO}")


def main():
    ap = argparse.ArgumentParser(description="抓 CBS Evening News 完整版并压成轻量音频")
    ap.add_argument("url", nargs="?", help="CBS 单集链接；不传则自动抓最新一期")
    ap.add_argument("--slug", help="直接指定期号（如 092326-cbs-evening-news），跳过列表页挑选")
    ap.add_argument("--count", type=int, default=1, help="连续抓最近 N 期（做播放列表用）")
    ap.add_argument("--list", action="store_true", help="只列出达标的候选单集，不下载")
    ap.add_argument("--list-all", action="store_true", help="列出全部候选单集（含 15 分钟以下的片段）")
    ap.add_argument("--no-video", action="store_true",
                    help="只下音频、不下本地视频（省 456MB 和约 20 分钟；前端会退回在线流）")
    ap.add_argument("--subs-only", action="store_true",
                    help="只补源字幕并重新嵌进已有的 mp4，不重下视频（配合 --slug）")
    args = ap.parse_args()

    global JS_RUNTIME, CURL, FFMPEG
    os.makedirs(EPISODES_DIR, exist_ok=True)
    log(f"输出目录: {EPISODES_DIR}")
    log(f"代理: {PROXY or '未启用'}")
    JS_RUNTIME = resolve_js_runtime()
    CURL = resolve_curl()

    # 补字幕：某一期抓的时候字幕没取到（旧版本走 yt-dlp 会这样），事后不用重下
    # 438MB 视频，读 info.json 里的字幕轨地址补一份，再重新嵌进已有的 mp4。
    if args.subs_only:
        if not args.slug:
            die("--subs-only 要指定期号：--slug 092426-cbs-evening-news")
        set_episode(args.slug)
        if not os.path.exists(OUT_INFO):
            die(f"{OUT_INFO} 不存在 —— 这一期还没抓过，去掉 --subs-only 跑一次完整抓取")
        with open(OUT_INFO, encoding="utf-8") as f:
            info = json.load(f)
        if not info.get("hls_subs"):
            die(f"{OUT_INFO} 里没有 hls_subs 地址，补不了字幕")
        if not fetch_subs(info):
            sys.exit(1)
        # 得写回 info.json：前端英文轨和后端 merge() 都读这里的 local_subs，
        # 只把文件补到磁盘上而不记这一笔，播放页照样少一条 CC。
        info["local_subs"] = os.path.basename(OUT_SUBS)
        with open(OUT_INFO, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        if os.path.exists(OUT_VIDEO):
            resolve_ffmpeg()
            embed_subs(OUT_VIDEO)
            print(f"  本地字幕: {os.path.basename(OUT_SUBS)}  (已嵌进 {os.path.basename(OUT_VIDEO)})")
        else:
            print(f"  本地字幕: {os.path.basename(OUT_SUBS)}  (没有本地视频，跳过嵌入)")
        return

    if args.list or args.list_all:
        for slug in list_episode_slugs()[:MAX_CANDIDATES]:
            info = episode_info(slug)
            d = info["duration"]
            ok = d is not None and MIN_DURATION <= d <= MAX_DURATION
            if args.list and not ok:
                continue
            print(f"  {'★' if ok else ' '} {fmt_dur(d):>10}  {info['title']}")
        return

    if args.slug:
        infos = [episode_info(args.slug, delay=False)]
    elif args.url:
        m = re.search(r"/video/([a-z0-9\-]+)", args.url)
        slug = m.group(1) if m else args.url
        infos = [episode_info(slug)]
        if infos[0]["duration"] and infos[0]["duration"] < MIN_DURATION:
            log(f"提示: 这条只有 {fmt_dur(infos[0]['duration'])}，不足 15 分钟，仍按你的指定继续")
    else:
        infos = pick_episodes(max(1, args.count))
        for info in infos:
            log(f"选中 {info['slug']}  {info['title']}（{fmt_dur(info['duration'])}）")

    ffmpeg = resolve_ffmpeg()
    for i, info in enumerate(infos, 1):
        if len(infos) > 1:
            log(f"===== 第 {i}/{len(infos)} 期: {info['slug']} =====")
            if i > 1:
                time.sleep(FETCH_DELAY)
        fetch_one(info, ffmpeg, no_video=args.no_video)


if __name__ == "__main__":
    main()
