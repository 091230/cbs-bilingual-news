# -*- coding: utf-8 -*-
"""把英文原稿转成中英对照字幕 + 重点词汇，导出 data/latest_cbs.json。

流程:
  1. 取英文分句：优先用 CBS 源字幕轨（官方听打稿），没有才跑本地 faster-whisper
  2. Claude 分块翻译 + 提炼词汇（结构化 JSON 输出）-> 走 DeepSeek 官方 Anthropic 端点
  3. 汇总去重，选出 15~20 个 B2~C2 词汇
  4. 与 video_info.json 合并 -> data/latest_cbs.json

为什么优先用源字幕：CBS 的 HLS master 里自带一条 WebVTT 字幕轨（官方听打稿），
200 个分片共约 240KB，20 秒拉完；换成 whisper 要在这台 6 核机器上跑十几分钟 CPU，
而且识别出来的文本有错字。源字幕的文字和时间轴都比识别准，唯一要自己做的是把
CBS 的短 cue 合并成句子级（它的 cue 是按显示行切的，经常一句跨四五条 cue）。

用法:
  python process_transcript.py --slug 092326-cbs-evening-news   # 处理指定一期
  python process_transcript.py                    # data/episodes 下只有一期时可省略 --slug
  python process_transcript.py --limit 3          # 只跑前 3 块（调试用，省额度）
  python process_transcript.py --force            # 忽略取词缓存，重新取（只花时间）
  python process_transcript.py --force-claude     # 忽略 Claude 缓存重新调（重新花钱）
  python process_transcript.py --whisper          # 强制走本地语音识别，不用源字幕
  python process_transcript.py --whisper-model medium.en   # 换更大的识别模型

依赖: faster-whisper（仅回退路径需要）、anthropic
注意: 走 whisper 回退路径时，首次运行需要 models/faster-whisper-base.en/ 里有模型文件
      （约 140MB）。缺的话脚本会把 curl 命令打出来，照抄即可。
必须用装了依赖的解释器跑（和 fetch_cbs_video.py 一样的坑）:
  C:/Users/lenovo/AppData/Local/Programs/Python/Python310/python.exe
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
from datetime import datetime

import cbs_subs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 必须在 import faster_whisper / huggingface_hub 之前设好，它们是在导入时读这个变量的。
# HuggingFace 直连被墙，走国内镜像；镜像挂了就把它设成空、改用代理：
#   set HTTPS_PROXY=http://127.0.0.1:7897
HF_MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
if HF_MIRROR:
    os.environ["HF_ENDPOINT"] = HF_MIRROR

try:
    import anthropic
except ImportError:
    sys.exit("缺少依赖 anthropic，先装：\n"
             "  C:/Users/lenovo/AppData/Local/Programs/Python/Python310/python.exe "
             "-m pip install anthropic")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
EPISODES_DIR = os.path.join(DATA_DIR, "episodes")

# 下面这几个是**按期**路径，由 set_episode() 在 main() 里按 slug 重新绑定。
# 缓存必须跟着期走：claude_raw.json 只按 segment_count + 模型名判有效，不认是哪一期，
# 两期都切出 249 句就会错误复用译文——白花钱，而且中文字幕整篇对错内容。
EP_DIR = None
AUDIO_FILE = None
INFO_FILE = None
OUT_FILE = None
CACHE_WHISPER = None
CACHE_CAPTIONS = None
CACHE_CLAUDE = None


def set_episode(slug):
    """把这一期的所有输入输出路径指到 data/episodes/<slug>/ 下。"""
    global EP_DIR, AUDIO_FILE, INFO_FILE, OUT_FILE
    global CACHE_WHISPER, CACHE_CAPTIONS, CACHE_CLAUDE
    EP_DIR = os.path.join(EPISODES_DIR, slug)
    AUDIO_FILE = os.path.join(EP_DIR, "audio.mp3")
    INFO_FILE = os.path.join(EP_DIR, "info.json")
    OUT_FILE = os.path.join(EP_DIR, "latest_cbs.json")
    CACHE_WHISPER = os.path.join(EP_DIR, "whisper_raw.json")
    CACHE_CAPTIONS = os.path.join(EP_DIR, "captions_raw.json")
    CACHE_CLAUDE = os.path.join(EP_DIR, "claude_raw.json")
    return EP_DIR


def list_episode_slugs():
    """data/episodes/ 下已有的期号，新的在前。"""
    if not os.path.isdir(EPISODES_DIR):
        return []
    return sorted((d for d in os.listdir(EPISODES_DIR)
                   if os.path.isdir(os.path.join(EPISODES_DIR, d))), reverse=True)

# ---------------------------------------------------------------------------
# Claude 配置（走 DeepSeek 官方的 Anthropic 兼容端点）
#
# 坑：Anthropic SDK 是它自己往 base_url 后面拼 /v1/messages 的，所以这里给
# `https://api.deepseek.com/anthropic`（不带 /v1），最终请求才是
# `https://api.deepseek.com/anthropic/v1/messages`。base_url 若以 /v1 结尾，
# 下面的 anthropic_base() 会把 /v1 剥掉，否则会变成 /v1/v1/messages。
#
# key 不用写进代码，按这个顺序找：RELAY_API_KEY 环境变量 -> ANTHROPIC_AUTH_TOKEN
# 环境变量 -> ~/.claude/settings.json（CC Switch 写的那份）。跟着 CC Switch 走就不用
# 两处维护同一个密钥；想用别的账号，设 RELAY_API_KEY 覆盖即可。
# ---------------------------------------------------------------------------
DEFAULT_API_BASE_URL = "https://api.deepseek.com/anthropic"

# 关键：这个别名映射到 deepseek-v4-pro。写成 claude-sonnet-5 会被映射到
# deepseek-flash（弱一档），翻译质量差不少。
CLAUDE_MODEL = os.environ.get("RELAY_MODEL", "claude-opus-5")

# 本地转写模型。默认指向下面这个本地目录（模型是 curl 从 hf-mirror 拉的，见 README）。
# 也可以直接写 HuggingFace 名字（如 "small.en"）让它自己下 —— 但国内下 484MB 要一个多小时，
# 而且 huggingface_hub 的 Xet 通道在这台机器上会卡死，不推荐。
# base.en 在 6 核 CPU 上处理 20 分钟音频约 5~8 分钟。想更准可以换 small.en / medium.en。
LOCAL_MODEL_DIR = os.path.join(BASE_DIR, "models", "faster-whisper-base.en")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", LOCAL_MODEL_DIR)

CHUNK_SIZE = 60          # 每个 Claude 请求塞多少条字幕。爆 max_tokens 就调小
# 实测每块输出在 9k~15k tokens 之间浮动（DeepSeek 的 thinking 也算在这里），给足余量：
# 一旦被截断就 die，而缓存是整轮跑完才写的，等于白付一场。端点接受 64000。
# 这个量级必须走流式，非流式 SDK 会直接抛 ValueError。
MAX_TOKENS = 40000
VOCAB_MIN, VOCAB_MAX = 15, 20

SYSTEM = (
    "你是给中国英语学习者做新闻精听的助手。CBS Evening News 的英文原稿会分块发给你，"
    "你做两件事：把每句翻译成自然、口语化的中文；挑出 B2~C2 级别的重点词汇。"
    "有的期次拿到的是广播字幕原稿（整句大写、夹着 >> 之类的换人标记），按普通英文"
    "理解就行，译文不要跟着大写或加标记符号。"
    "只输出 JSON，不要任何解释文字，不要 markdown 代码块。"
)

# ---------------------------------------------------------------------------
# 结构化输出的 schema —— 注意：这是**尽力而为，不是保证**。
#
# 实测（2026-09-24）DeepSeek 那条端点会收下 output_config 却完全不执行 schema：
# 不报 400（所以下面的降级分支不会触发），也不约束输出形状——要求返回对象时它照样
# 能给你一个裸数组。所以下面每个解析点都用 as_* / norm_* 兜住各种形状，
# 不能假设拿到的一定是 schema 描述的结构。
# ---------------------------------------------------------------------------
VOCAB_ITEM = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "level": {"type": "string", "enum": ["B2", "C1", "C2"]},
        "meaning": {"type": "string"},
    },
    "required": ["word", "level", "meaning"],
    "additionalProperties": False,
}

CHUNK_SCHEMA = {
    "type": "object",
    "properties": {
        "subtitles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "zh": {"type": "string"}},
                "required": ["index", "zh"],
                "additionalProperties": False,
            },
        },
        "vocabulary": {"type": "array", "items": VOCAB_ITEM},
    },
    "required": ["subtitles", "vocabulary"],
    "additionalProperties": False,
}

VOCAB_SCHEMA = {
    "type": "object",
    "properties": {"vocabulary": {"type": "array", "items": VOCAB_ITEM}},
    "required": ["vocabulary"],
    "additionalProperties": False,
}


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


def anthropic_base(base):
    """Anthropic SDK 自己拼 /v1/messages，所以 base 结尾若是 /v1 要先剥掉。"""
    b = base.rstrip("/")
    return b[:-3].rstrip("/") if b.endswith("/v1") else b


def resolve_claude_config():
    """base_url 和 key 都优先环境变量，其次 CC Switch 的 settings.json。"""
    cfg = {}
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
            cfg = json.load(f).get("env") or {}
    except (OSError, ValueError):
        pass

    base = (os.environ.get("RELAY_API_BASE_URL") or cfg.get("ANTHROPIC_BASE_URL")
            or DEFAULT_API_BASE_URL)
    for name in ("RELAY_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(name):
            return base, os.environ[name], f"环境变量 {name}"
    if cfg.get("ANTHROPIC_AUTH_TOKEN"):
        return base, cfg["ANTHROPIC_AUTH_TOKEN"], "~/.claude/settings.json"
    return base, "", ""


def loads_loose(text):
    """模型偶尔会裹 ``` 或加句前言，尽量把 JSON 对象抠出来。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            return json.loads(s[i:j + 1])
        raise


def _pick_list(data, keys, marker):
    """从模型返回里抠出目标列表，容忍各种形状。

    因为端点不执行 schema，模型可能给 {"vocabulary":[...]}、也可能直接给裸数组，
    还可能换个键名，或者把键名写成中文。按 key 名找；找不到再退两步：
    整个就是数组，或 dict 里只有一个数组值、且元素带特征字段。
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        # 多个数组值时按元素特征挑（chunk 是 {"subtitles":[带 index], "vocabulary":[带 word]}）
        for v in lists:
            if v and isinstance(v[0], dict) and marker in v[0]:
                return v
    return []


def as_subtitles(data):
    return [x for x in _pick_list(data, ("subtitles", "subtitle", "字幕", "items"), "index")
            if isinstance(x, dict) and "index" in x]


def as_vocabulary(data):
    return _pick_list(data, ("vocabulary", "words", "vocab", "词汇", "items"), "word")


def norm_subtitles(raw):
    """-> {index: zh}。容忍 index 是字符串、zh 用别的键名。"""
    out = {}
    for x in raw:
        if not isinstance(x, dict):
            continue
        idx = x.get("index")
        if isinstance(idx, str) and idx.strip().lstrip("-").isdigit():
            idx = int(idx.strip())
        zh = x.get("zh") or x.get("中文") or x.get("translation") or x.get("text")
        if isinstance(idx, int) and isinstance(zh, str) and zh.strip():
            out[idx] = zh.strip()
    return out


def norm_vocabulary(raw):
    """-> [{word, level, meaning}]，顺带跨块去重（大小写不敏感）。"""
    out, seen = [], set()
    for x in raw:
        if not isinstance(x, dict):
            continue
        w = x.get("word") or x.get("term") or x.get("词汇") or x.get("单词")
        if not isinstance(w, str) or not w.strip():
            continue
        if w.strip().lower() in seen:
            continue
        seen.add(w.strip().lower())
        lv = str(x.get("level") or x.get("级别") or "").strip().upper()
        if lv not in ("B2", "C1", "C2"):
            lv = "C1"
        m = x.get("meaning") or x.get("释义") or x.get("意思") or x.get("翻译")
        out.append({"word": w.strip(), "level": lv,
                    "meaning": m.strip() if isinstance(m, str) else ""})
    return out


# ---------------------------------------------------------------------------
# CBS 源字幕（优先路径）
#
# master.m3u8 里那条 `#EXT-X-MEDIA:TYPE=SUBTITLES` 指向 subs_en.m3u8，是一个
# 200 段的 HLS WebVTT 播放列表。分片里的时间戳是**全片绝对时间**（第 50 片就从
# 5:00 开始），所以合并的时候不用自己算偏移。
#
# 取分片 / 解析 cue / 合并成句子级都在 cbs_subs.py —— fetch_cbs_video.py 要的是
# 同一份数据（落成 subs_en.vtt 当英文 CC），两边各写一套出过事：fetch 那边原来
# 走 yt-dlp --write-subs，而它有时列不出 CBS 的字幕轨。
# ---------------------------------------------------------------------------
def fetch_captions(subs_url):
    """拉完整条字幕轨 -> 和 whisper 同构的 {duration, language, segments}。

    segments 的形状故意做得和 transcribe() 一样，下游的分块翻译/合并/导出
    一行都不用改。
    """
    log(f"取 CBS 源字幕: {subs_url}")
    t0 = time.time()
    cues = cbs_subs.fetch_cues(subs_url)
    rows = cbs_subs.merge_cues(cues)
    segments = [{"index": i, "start": round(s, 2), "end": round(e, 2), "en": t}
                for i, (s, e, t) in enumerate(rows)]
    log(f"  源字幕完成：{len(cues)} 条 cue -> {len(segments)} 句，"
        f"到 {segments[-1]['end'] / 60:.1f} 分钟，用时 {time.time() - t0:.1f} 秒")
    return {"duration": segments[-1]["end"], "language": "en",
            "segments": segments, "source": "cbs-captions", "subs_url": subs_url,
            "cleaner": cbs_subs.CACHE_VERSION}


def get_captions(info, force=False):
    """源字幕也落盘缓存：字幕轨偶尔会整条取不到，留个底省得重来。"""
    subs_url = (info or {}).get("hls_subs")
    if not subs_url:
        raise ValueError("video_info.json 里没有 hls_subs，重跑一次 fetch_cbs_video.py")

    if os.path.exists(CACHE_CAPTIONS) and not force:
        try:
            with open(CACHE_CAPTIONS, encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("subs_url") == subs_url and cached.get("segments")
                    and cached.get("cleaner") == cbs_subs.CACHE_VERSION):
                log(f"复用源字幕缓存 {CACHE_CAPTIONS}（{len(cached['segments'])} 句）；"
                    f"想重取加 --force")
                return cached
            log("源字幕缓存是别的片子的，或者清洗规则改过了，重新取")
        except (OSError, ValueError):
            log("源字幕缓存读不了，重新取")

    result = fetch_captions(subs_url)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_CAPTIONS, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"  缓存到 {CACHE_CAPTIONS}")
    return result


# ---------------------------------------------------------------------------
# 本地 Whisper 转写（回退路径）
# ---------------------------------------------------------------------------
def transcribe(audio_path, model_size):
    """用 faster-whisper 在本机转写。不联网（除了首次下模型），不产生 API 费用。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("没装 faster-whisper，先跑：\n"
            "  C:/Users/lenovo/AppData/Local/Programs/Python/Python310/python.exe "
            "-m pip install faster-whisper")

    if os.path.sep in model_size and not os.path.isdir(model_size):
        die(f"本地模型目录不存在: {model_size}\n"
            "  首次使用要先把模型拉下来（约 140MB，走 hf-mirror；别用裸名字让它自己下，Xet 会卡死）：\n"
            f'    mkdir -p "{model_size}"\n'
            "    for f in config.json tokenizer.json vocabulary.txt model.bin; do\n"
            f'      curl -L -C - -o "{model_size}/$f" \\\n'
            "        https://hf-mirror.com/Systran/faster-whisper-base.en/resolve/main/$f\n"
            "    done")

    log(f"加载 Whisper 模型 {model_size} …")
    t0 = time.time()
    # int8 量化：CPU 上快很多，内存占用也小（这台机器只有 5.8G）
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    log(f"  模型就绪，用时 {time.time() - t0:.1f} 秒")

    log(f"开始转写 {os.path.basename(audio_path)}（{fmt_size(audio_path)}），CPU 6 核 …")
    t0 = time.time()
    seg_iter, info = model.transcribe(
        audio_path,
        language="en",
        beam_size=5,
        vad_filter=True,                 # 跳过静音段，减少空转和幻觉
        condition_on_previous_text=False,  # 长音频上防重复循环
    )

    out = []
    for s in seg_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        out.append({
            "index": len(out),
            "start": round(float(s.start), 2),
            "end": round(float(s.end), 2),
            "en": text,
        })
        if len(out) % 40 == 0:
            log(f"  已转写 {len(out)} 句，到 {s.end / 60:.1f} 分钟…")

    dur = getattr(info, "duration", None)
    log(f"  转写完成：{len(out)} 句，用时 {time.time() - t0:.1f} 秒"
        f"（音频 {dur or '?'} 秒，语种 {getattr(info, 'language', '?')}）")
    return {"duration": dur, "language": getattr(info, "language", None),
            "segments": out, "source": "faster-whisper"}


def get_transcript(audio_path, model_size, force=False):
    """转写结果落盘缓存 —— 调 Claude 那部分要反复改，省得每次重跑转写。"""
    if os.path.exists(CACHE_WHISPER) and not force:
        try:
            with open(CACHE_WHISPER, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("segments") and cached.get("model") == model_size:
                log(f"复用转写缓存 {CACHE_WHISPER}（{len(cached['segments'])} 句，"
                    f"模型 {model_size}）；想重跑加 --force")
                return cached
            log("缓存是别的模型转的，重跑")
        except (OSError, ValueError):
            log("转写缓存读不了，重新跑")

    result = transcribe(audio_path, model_size)
    result["model"] = model_size
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_WHISPER, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"  缓存到 {CACHE_WHISPER}")
    return result


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
def run_stream(client, kwargs):
    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "max_tokens":
        die(f"Claude 输出被 max_tokens={MAX_TOKENS} 截断了，把 CHUNK_SIZE 调小再跑")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    if not text.strip():
        die("Claude 没返回文本内容（stop_reason=%s）" % msg.stop_reason)
    return loads_loose(text), msg


def call_claude(client, prompt, schema):
    """一次结构化输出调用。"""
    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        return run_stream(client, dict(
            kwargs,
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": schema}},
        ))
    except anthropic.BadRequestError as e:
        # 有些中转不透传 output_config。提示词里本来就写了"只输出 JSON"，
        # 去掉这个参数再试一次，靠 loads_loose 兜底解析。
        log(f"  端点不接受 output_config（{e.message[:70]}…），退回提示词约束 JSON")
        return run_stream(client, kwargs)


# 这一期翻译花掉的 token。以前只 log 到 stdout，跑完就没了 —— 一期一两万 token
# 看着不多，量产几百期要花多少钱就完全没依据。落进 claude_raw.json 才有账可算。
# 只记 token 不记钱：价格随端点/时段变，记 token 是永不过期的那一半。
USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def add_usage(usage):
    USAGE["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
    USAGE["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
    USAGE["calls"] += 1


def translate_chunk(client, chunk, i, total_chunks):
    lines = "\n".join(f'{s["index"]}. {s["en"]}' for s in chunk)
    prompt = (
        f"这是本期共 {total_chunks} 块里的第 {i} 块，含 {len(chunk)} 句。\n\n"
        f"【本块英文字幕】\n{lines}\n\n"
        "【要求】（下面用方括号编号，别和字幕的行号搞混）\n"
        f"〔1〕subtitles：必须覆盖上面全部 {len(chunk)} 个序号，一条都不能漏、不能合并。"
        "index 原样照抄，zh 是这句的中文翻译（口语化、听着自然，不要逐字硬译）。"
        "不要输出时间轴，时间轴由程序用 Whisper 的原始数据回填。\n"
        "〔2〕vocabulary：从这一块挑 3~6 个 B2~C2 级别的词汇或固定搭配，"
        "跳过 the/of 这类基础词和人名地名机构名。word 用词条原形，"
        "level 给 B2/C1/C2，meaning 给简洁中文释义（可带词性）。\n"
        "只输出 JSON。"
    )
    data, msg = call_claude(client, prompt, CHUNK_SCHEMA)
    zh = norm_subtitles(as_subtitles(data))
    vocab = norm_vocabulary(as_vocabulary(data))
    usage = msg.usage
    add_usage(usage)
    missed = len(chunk) - len(zh)
    log(f"  第 {i}/{total_chunks} 块完成：{len(zh)}/{len(chunk)} 句译文、{len(vocab)} 个词"
        + (f"  ⚠ 漏 {missed} 句" if missed else "")
        + f"  (in {usage.input_tokens} / out {usage.output_tokens} tokens)")
    return zh, vocab


def select_vocabulary(client, candidates):
    """候选词去重后超过 VOCAB_MAX 时，让 Claude 挑最终的 15~20 个。"""
    prompt = (
        "下面是 CBS Evening News 本期各段落筛出来的候选词汇（已去重）：\n"
        f"{json.dumps(candidates, ensure_ascii=False)}\n\n"
        f"请挑出最适合中国英语学习者精读的 {VOCAB_MIN}~{VOCAB_MAX} 个："
        "覆盖全片而非集中在某几段，难度以 C1 为主、少量 B2 和 C2，"
        "合并重复项和近义项（只留最值得学的那条），给出最终列表。\n"
        '严格按 {"vocabulary": [{"word": "...", "level": "C1", "meaning": "..."}]} '
        "这个形状输出，外面那层对象别省（之前有几次直接给了个裸数组）。只输出 JSON。"
    )
    data, msg = call_claude(client, prompt, VOCAB_SCHEMA)
    add_usage(msg.usage)
    vocab = norm_vocabulary(as_vocabulary(data))
    log(f"  词汇精选完成：{len(candidates)} 个候选 -> {len(vocab)} 个最终词条  "
        f"(out {msg.usage.output_tokens} tokens)")
    return vocab


def source_hash(segments):
    """英文原稿的指纹。

    缓存光看模型名 + 句数不够：清洗规则一改，句数可能一模一样而句句都变了，那样会
    静默复用旧译文——白花钱还是小事，中文字幕整篇对不上原文才是大事。
    """
    h = hashlib.sha256()
    for s in segments:
        h.update((s.get("en") or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def analyze(client, chunks, seg_count, force=False, limited=False):
    """跑完 Claude 那半边（分块翻译 + 词汇精选）-> (zh_by_index, vocabulary)。

    转写重跑只花时间，这半边重跑花的是钱，所以单独存一份缓存、默认复用。
    要重跑用 --force-claude —— 别用 --force 顺手把钱也重花了。
    """
    fingerprint = source_hash([s for c in chunks for s in c])
    if os.path.exists(CACHE_CLAUDE) and not force and not limited:
        try:
            with open(CACHE_CLAUDE, encoding="utf-8") as f:
                c = json.load(f)
            if (c.get("claude_model") == CLAUDE_MODEL
                    and c.get("segment_count") == seg_count
                    and c.get("source_hash") == fingerprint):
                log(f"复用 Claude 缓存（{len(c['zh_by_index'])} 句译文、"
                    f"{len(c['vocabulary'])} 个词条）；想重跑加 --force-claude")
                return {int(k): v for k, v in c["zh_by_index"].items()}, c["vocabulary"]
            log(f"Claude 缓存对不上（模型 {c.get('claude_model')} / "
                f"{c.get('segment_count')} 句 / 原稿指纹 {c.get('source_hash')}），重新调")
        except (OSError, ValueError, KeyError):
            log("Claude 缓存读不了，重新调")

    log(f"切 {len(chunks)} 块（每块 {CHUNK_SIZE} 句），逐块送 Claude …")
    USAGE.update(input_tokens=0, output_tokens=0, calls=0)   # 复用缓存时不重置，那次没花钱
    zh_by_index, candidates = {}, []
    for i, chunk in enumerate(chunks, 1):
        zh, vocab = translate_chunk(client, chunk, i, len(chunks))
        zh_by_index.update(zh)
        candidates.extend(vocab)

    uniq = norm_vocabulary(candidates)
    log(f"候选词汇 {len(candidates)} 个 -> 去重 {len(uniq)} 个")
    if len(uniq) > VOCAB_MAX:
        vocabulary = select_vocabulary(client, uniq)
        if not vocabulary:
            log(f"警告: 词汇精选没给出词条（端点不执行 schema，模型可能返回了意外形状），"
                f"退回按出现顺序取前 {VOCAB_MAX} 个候选")
            vocabulary = uniq[:VOCAB_MAX]
    else:
        if len(uniq) < VOCAB_MIN:
            log(f"警告: 去重后只有 {len(uniq)} 个词，不足 {VOCAB_MIN} 个")
        vocabulary = uniq

    log(f"这一期翻译共 {USAGE['calls']} 次调用、"
        f"in {USAGE['input_tokens']} / out {USAGE['output_tokens']} tokens")

    if not limited:
        with open(CACHE_CLAUDE, "w", encoding="utf-8") as f:
            json.dump({"claude_model": CLAUDE_MODEL, "segment_count": seg_count,
                       "source_hash": fingerprint,
                       "zh_by_index": zh_by_index, "candidates": candidates,
                       "vocabulary": vocabulary, "usage": USAGE},
                      f, ensure_ascii=False, indent=2)
        log(f"  缓存到 {CACHE_CLAUDE}")
    return zh_by_index, vocabulary


# ---------------------------------------------------------------------------
# 组装导出
# ---------------------------------------------------------------------------
def merge(slug, segments, zh_by_index, vocabulary, video_info, transcript):
    subtitles = []
    missing = 0
    for s in segments:
        zh = zh_by_index.get(s["index"])
        if not zh:
            missing += 1
        subtitles.append({
            "index": s["index"],
            "start": s["start"],
            "end": s["end"],
            "en": s["en"],
            "zh": zh or "",
        })
    if missing:
        log(f"警告: 有 {missing} 句没拿到译文（模型漏掉了序号），已留空")

    source = transcript.get("source") or "unknown"
    return {
        "slug": slug,
        "video": video_info,
        "vocabulary": vocabulary,
        "subtitles": subtitles,
        "meta": {
            "slug": slug,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "claude_model": CLAUDE_MODEL,
            # 留个印记，以后看这个 json 能知道英文是官方字幕还是机器听出来的
            "transcript_source": source,
            "transcript_detail": (transcript.get("subs_url") if source == "cbs-captions"
                                  else transcript.get("model")) or "",
            "segment_count": len(subtitles),
            "audio_duration": transcript.get("duration"),
        },
    }


# ---------------------------------------------------------------------------
# 画面内中文字幕轨
# ---------------------------------------------------------------------------
def write_zh_vtt(path, subtitles):
    """用句子级译文生成一条 VTT，给 <video> 上的中文字幕轨用。

    文本裹一层 <c.zh>：::cue 的选择器只能拿到元素/类，拿不到"这是第几条轨"，
    靠这个类名才能只给中文字幕加样式（`video::cue(.zh)`），不影响英文字幕。
    样式建议放在 HTML 的 <style> 里，这里只管内容。

    时间轴和英文轨**完全一致**（都由 cbs_subs.merge_cues 出来，同一套 start/end），
    所以中英同开时不会一行在句中切换。

    不写 cue settings，中文就落在浏览器默认的底部；英文轨那边是贴顶的
    （`cbs_subs.CUE_TOP`）。位置只能写进 VTT，::cue 管不到，改的话看 cbs_subs 里
    那段说明——**别用百分比**。
    """
    lines = ["WEBVTT", ""]
    for s in subtitles:
        zh = (s.get("zh") or "").strip()
        if not zh:
            continue
        lines.append(f"{s['index'] + 1}")
        lines.append(f"{cbs_subs.vtt_time(s['start'])} --> {cbs_subs.vtt_time(s['end'])}")
        lines.append(f"<c.zh>{cbs_subs.vtt_escape(zh)}</c>")
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path


def main():
    ap = argparse.ArgumentParser(
        description="CBS 源字幕/本地 Whisper + Claude 翻译，导出 data/episodes/<slug>/latest_cbs.json")
    ap.add_argument("--slug", help="期号（data/episodes/ 下的目录名）。只有一期时可以省略")
    ap.add_argument("--force", action="store_true", help="忽略取词缓存，重新取（只花时间）")
    ap.add_argument("--force-claude", action="store_true",
                    help="忽略 Claude 缓存重新调（会重新花钱，慎用）")
    ap.add_argument("--limit", type=int, help="只处理前 N 块（调试用，省额度；不写 Claude 缓存）")
    ap.add_argument("--whisper", action="store_true",
                    help="强制走本地语音识别，不用 CBS 源字幕")
    ap.add_argument("--whisper-model", default=WHISPER_MODEL,
                    help=f"本地识别模型，默认 {WHISPER_MODEL}")
    args = ap.parse_args()

    slug = args.slug
    if not slug:
        available = list_episode_slugs()
        if len(available) == 1:
            slug = available[0]
            log(f"没指定 --slug，data/episodes 下只有一期，就用 {slug}")
        elif available:
            die("data/episodes 下有多期，得用 --slug 指定哪一期：\n  "
                + "\n  ".join(available))
        else:
            die("data/episodes 下还没有任何一期，先跑 fetch_cbs_video.py")
    elif not os.path.isdir(os.path.join(EPISODES_DIR, slug)):
        die(f"data/episodes/{slug} 不存在。现有期号：\n  "
            + ("\n  ".join(list_episode_slugs()) or "（一个都没有，先跑 fetch_cbs_video.py）"))
    set_episode(slug)

    base_url, api_key, key_src = resolve_claude_config()
    if not api_key:
        die("找不到 API key。设 RELAY_API_KEY 环境变量，或在 ~/.claude/settings.json 里配好：\n"
            "  set RELAY_API_KEY=sk-xxx")

    if not os.path.exists(INFO_FILE):
        die(f"找不到 {INFO_FILE}，先跑 fetch_cbs_video.py 抓一期")
    with open(INFO_FILE, "r", encoding="utf-8") as f:
        video_info = json.load(f)

    os.makedirs(EP_DIR, exist_ok=True)
    log(f"期号: {slug}")
    log(f"视频: {video_info.get('title')}（{video_info.get('duration')} 秒）")
    log(f"翻译: {base_url}  模型 {CLAUDE_MODEL}   key 来源: {key_src}")

    # 1. 取英文分句。优先 CBS 官方字幕轨；它取不到（或显式 --whisper）才动语音识别。
    transcript = None
    if args.whisper:
        log("按 --whisper 走本地语音识别")
    elif not video_info.get("hls_subs"):
        log("video_info.json 里没有 hls_subs（重跑 fetch_cbs_video.py 可以补上），"
            "退回本地语音识别")
    else:
        try:
            transcript = get_captions(video_info, force=args.force)
        except (OSError, ValueError, http.client.HTTPException) as e:
            # 只兜网络/解析类错误。要是代码本身有 bug（AttributeError 之类）就让它
            # 直接炸出来 —— 否则会静默退到语音识别，白跑十几分钟 CPU 还多花一次
            # Claude 的钱，而且英文来源悄悄换了。
            log(f"警告: 源字幕取用失败（{type(e).__name__}: {e}），退回本地语音识别")
            log("      注意：换来源会改变句子切分，Claude 那半边要重新跑一次（花钱）")

    if transcript is None:
        if (args.force or not os.path.exists(CACHE_WHISPER)) and not os.path.exists(AUDIO_FILE):
            die(f"找不到 {AUDIO_FILE}，先跑 fetch_cbs_video.py 抓一期")
        log(f"识别: 本地 {args.whisper_model}（6 核 CPU，约十几分钟）")
        transcript = get_transcript(AUDIO_FILE, args.whisper_model, force=args.force)

    segments = transcript["segments"]
    if not segments:
        die("没拿到任何有效句子")

    # 2. Claude 分块翻译
    chunks = [segments[i:i + CHUNK_SIZE] for i in range(0, len(segments), CHUNK_SIZE)]
    if args.limit:
        chunks = chunks[:args.limit]

    # DeepSeek 返回的 thinking 块也算在 max_tokens 里，所以超时要给足
    client = anthropic.Anthropic(
        base_url=anthropic_base(base_url),
        api_key=api_key,
        max_retries=3,                        # 429/5xx 自动退避重试
        timeout=anthropic.Timeout(600.0, connect=30.0),
    )

    zh_by_index, vocabulary = analyze(client, chunks, len(segments),
                                      force=args.force_claude, limited=bool(args.limit))
    if not zh_by_index:
        die("一句译文都没拿到，检查端点是否正常返回 JSON")

    # 3. 导出
    result = merge(slug, segments, zh_by_index, vocabulary, video_info, transcript)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    zh_vtt = write_zh_vtt(os.path.join(EP_DIR, "subs_zh.vtt"), result["subtitles"])

    # 4. 入库。JSON 才是原始产物，DB 只是可重建的查询索引 —— 所以入库失败只警告，
    #    不能让一场已经付过钱的跑白费；事后 python db.py --resync 就能补上。
    try:
        import db
        counts = db.ingest_file(OUT_FILE)
        log(f"  已入库 {db.DB_PATH}（字幕 {counts['subtitles']} 句、词汇 {counts['vocabulary']} 个）")
    except Exception as e:
        log(f"警告: 入库失败（{type(e).__name__}: {e}）；"
            f"JSON 已写好，事后跑 python db.py --resync 可以补")

    size = os.path.getsize(OUT_FILE) / 1024
    log("完成")
    print(f"  英文来源: {result['meta']['transcript_source']}")
    print(f"  字幕    : {len(result['subtitles'])} 句（中英对照）")
    print(f"  词汇    : {len(vocabulary)} 个")
    print(f"  中文字幕轨: {zh_vtt}")
    print(f"  输出    : {OUT_FILE}  ({size:.0f} KB)")
    if args.limit:
        print(f"  注意    : 这次带了 --limit {args.limit}，只处理了前 {len(chunks)} 块")


if __name__ == "__main__":
    try:
        main()
    except anthropic.AuthenticationError:
        die("端点返回 401 —— key 不对或已失效")
    except anthropic.NotFoundError as e:
        die(f"端点返回 404 —— base_url 写错，或该端点不认模型 "
            f"'{CLAUDE_MODEL}': {e.message}")
    except anthropic.RateLimitError:
        die("端点返回 429 —— 限流了，等一会儿再跑（转写有缓存，不用重跑）")
    except anthropic.APIConnectionError as e:
        die(f"连不上 {DEFAULT_API_BASE_URL}：{e}")
    except anthropic.APIStatusError as e:
        die(f"端点返回 {e.status_code}: {e.message}")
    except KeyboardInterrupt:
        log("手动中断")
        sys.exit(130)
