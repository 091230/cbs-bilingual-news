# -*- coding: utf-8 -*-
"""从 CBS 的 HLS 字幕播放列表取官方 WebVTT 听打稿，并清洗成句子级。

两个脚本都要这份数据，只是用法不同：
  - fetch_cbs_video.py  落成 subs_en.vtt（前端英文 CC 轨 + 嵌进 mp4 给外部播放器）
  - process_transcript.py  送去 Claude 翻译

所以放在这里一份。以前 fetch 那边走的是 `yt-dlp --write-subs`，而 yt-dlp 的
CBS 提取器**有时列不出字幕轨**（09-24 那期就报了 "There are no subtitles for
the requested languages"），于是那一期静默地没有英文字幕。而 master.m3u8 里那条
`#EXT-X-MEDIA:TYPE=SUBTITLES` 是稳定存在的——直接顺着它拉分片就对了，
200 个分片 6 路并发约 10 秒。

管线是 parse_vtt -> clean_cue -> merge_cues，三级都在清洗，因为 CBS 每期给的稿子
差别很大（09-23 是干净的听打稿，09-24 是广播字幕原稿）：
  1. parse_vtt    拆出每条 cue，剥标签/解实体/去音乐符和 `>>` 说话人标记
  2. _overlap_glue 接缝对齐：滚动字幕会把上一行重新显示一遍，重合的部分只留一份
  3. _dedup_rows  行间对齐：跨行的滚动重复，整行重说的直接并掉
改这三处的任意一处都要把 CACHE_VERSION 加一，否则 process_transcript 会继续用
旧规则算出来的字幕缓存（句数可能一模一样，内容却已经不同了）。
"""
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")

# 一次 6 秒左右的显示片段一个分片，整期约 200 个。并发拉，别一个个来。
CAPTION_WORKERS = 6
# 两条 cue 之间的空档超过这个秒数就当换了话题，在这里断句（否则会把两段话黏成一行）
CAPTION_GAP_SPLIT = 0.8
# 相邻文本接缝处，尾部和新头重合这么多词以上就认定是滚动字幕的重复显示，去掉一份。
# 2 而不是 3：滚动字幕里 "ALSO BREAKING" + "ALSO BREAKING TONIGHT" 这种两词重叠很常见。
MIN_OVERLAP = 2

# 清洗/合并规则的版本号。process_transcript 把它一起存进字幕缓存，对不上就重取 ——
# 缓存原来只认 subs_url，改规则后旧缓存会被静默复用，等于白改。
# v4: 句末缩写判定（ABBREV_END）+ 一条 cue 切出多行时剩余部分从切口接着算时间。
# v5: 全大写稿还原成正常大小写（sentence_case）。
CACHE_VERSION = 5

# 画面内字幕的默认位置（cue settings，只能写进 VTT 文件里，::cue 管不到位置）。
# 英文轨贴顶、中文轨不写（浏览器默认贴底）—— 英文在上、中文在下，两条同开不打架。
#
# 只能用行号，**不能用百分比**（`line:6%` 这种）：Chromium 对百分比行不换行，整句
# 摊成一行、右边超出画面被切掉（实测只剩一行且右边缘有亮像素）；加 `size` 也救不
# 回来（从"超出画面"变成"框内截断"）。行号形式换行正常。行号按行高算，全屏字号
# 变大时贴顶的距离也跟着走。
CUE_TOP = "line:1"
CUE_BOTTOM = ""

CUE_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})\.(\d{3})")
# 句末标点（容忍后面跟引号/括号）。用来把跨 cue 的长句合并成一行。
SENT_END = re.compile(r'[.!?]["\')\]]*(?=\s|$)')
# 点号后面跟空格**未必**是句末：缩写词后面就是这样。CBS 稿里遍地是
# "FLYOVERS OF BOTH U.S. FIGHTER JETS" / "his new wife, Dr. Rachel Dinis."，
# 认成句末就会把一句话切成两个半句（中文也跟着变成半句）。
# 只认"字母点字母点"（U.S. / P.I.T.）和常见缩写词，`plan A.` 这种单个字母不算。
ABBREV_END = re.compile(
    r"(?:^|[\s(])(?:[A-Za-z]\.){2,}$"
    r"|(?:^|[\s(])(?:Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Gen|Col|Sgt|Lt|Capt|Adm|St|Jr|Sr"
    r"|Inc|Corp|Co|Ltd|vs|etc|No|Ave|Blvd|Dept|Univ|Pres)\.$")
# 纯音效标注，如 [INSPIRATIONAL MUSIC]，不翻译、不显示
SOUND_ONLY = re.compile(r"^\[[^\]]*\]$")

# ---------------------------------------------------------------------------
# 全大写稿的还原（CBS 有一半的期次给的是广播字幕原稿，全大写）
#
# 09-24 那期整条轨都是大写（"PRESIDENT TRUMP WELCOMES CHINA'S XI JINPING"），
# 精听时读着费劲，译文的右栏也一样。09-18 / 09-23 给的是正常听打稿，所以只对
# "整条轨都是大写"的期次做还原，另外那两期一字不动（指纹不变 = 译文缓存还命中）。
#
# 还原＝全小写之后，把句首、I、和**这份表里的词**重新大写。表是从 09-18 / 09-23
# 两期里挖出来的（同一周的新闻、同一批人名地名），加上 09-24 独有的几个实体。
#
# ⚠️ 这份表是**冻结**的，不能改成"每次从别的期里现挖"：那样清洗结果会随目录里
# 有哪些期而变，source_hash 跟着变，已付过钱的译文就白存了。同理，以后往表里加词
# 也会让所有全大写期次的指纹变（要重新花钱翻译），所以要加就趁早一次加够。
CASE_KEEP = {}
for _w in [
        # 台标、机构、缩写
        "AI", "CBS", "CNN", "CEO", "FAA", "GFS", "LLC", "MTV", "NVIDIA", "TMZ", "TV", "UN",
        "USS", "WHCA", "NATO", "FBI", "CIA", "ICE", "GOP", "DOJ", "CDC", "FDA", "IRS",
        "U.S.", "U.K.", "D.C.", "N.Y.", "N.J.", "U.N.", "EU", "AP", "WTF", "OD", "U.S.-China",
        # 人
        "Abraham", "Acosta", "Andrea", "Andrews", "Ashley", "Botner", "Brennan", "Bruno",
        "Bryan", "Caleb", "Calia", "Callan", "Carter", "Charles", "Charlie", "Cherish",
        "Christopher", "Claire", "Clancy", "Coates", "Collins", "Cora", "D'Agata",
        "Dallas", "Danny", "Dawson", "Desronvil", "Devlin", "Dinis", "Dokoupil", "Dolly",
        "Dordick", "Douthat", "Eugenio", "Flynn", "George", "Greer-Wilkinson", "Hanson",
        "Harry", "Hawley", "Huang", "Ian", "Jensen", "Jessica", "Jim", "Jinping", "Jo",
        "Kaitlan", "Kelly", "Kent", "Kiniry", "Lee", "Levinson", "Lincoln", "Lindsay",
        "Ling", "Margaret", "Marciano", "Michael", "Nancy", "Naomi", "Nozell", "Parton",
        "Patrick", "Paula", "Rachel", "Ross", "Seaver", "Shanelle", "Steve", "Timothy",
        "Tom", "Tony", "Trey", "Trump", "Weijia", "Wilk", "Xi", "Zelenskyy", "Netanyahu",
        "Taylor", "Swift", "Obama", "Benjamin", "Tillis", "Thom", "Mamdani", "Susan",
        "Sarandon", "Meg", "Oliver", "Lala", "Nolo", "Polo", "Elijah", "Hemingway",
        "Idris", "Evans", "Jonathan", "Vigliotti", "Billy", "Ramirez", "Lilia",
        "Luciano", "Aidan", "Hamilton", "Robert", "Strang", "Cristian", "Benavides",
        "Eugene", "Dakota", "Colin", "Farrell", "Macchio", "Richard", "Nixon", "Pat",
        "Rob", "Brown", "Johnson", "Marie",
        # 地名。Beach / Coast / Island / Middle / Shore / North / Southwest 故意不进表：
        # 广播稿里 "BEACH EROSION" "THE MIDDLE OF THE NIGHT" "THE COAST OF THE CAROLINAS"
        # 都是普通名词，进了表就会写成 "Beach erosion"。真正是专名的那几个走 CASE_PHRASES。
        "Alberta", "America", "American", "Americans", "Amsterdam", "Angeles", "Atlanta",
        "Atlantic", "Baja", "Beijing", "Boston", "British", "California", "Canada",
        "Carolina", "Carolinas", "China", "Chinese", "City", "Colorado",
        "Connecticut", "Curacao", "Delmarva", "Eastern", "England", "Englewood",
        "Florida", "Francisco", "Gaza", "Greenland", "Gulf", "Hawaii", "Hialeah", "Iran",
        "Iranian", "Israel", "Israeli", "Jersey", "Latino", "London", "Los",
        "Massachusetts", "Mexico", "Miami", "Miami-Dade", "Mid-Atlantic",
        "Mississippi", "Nashville", "Norfolk", "Northeast", "Northern", "Ohio",
        "Pacific", "Pittsburgh", "Providence", "San", "Stanford",
        "Taiwan", "Tehran", "Ukraine", "United", "Utah", "Virginia", "Wales",
        "Washington", "Wisconsin", "York", "Nor'easter", "B-2", "B-52", "P.I.T.",
        # 机构/专有名词
        "Amendment", "Associated", "Bureau", "Category", "Congress", "Constitution",
        "DEI", "Democrat", "Democrats", "Democratic", "Force", "House", "Hurricane",
        "Joint", "Mayor", "Nation", "Naval", "Navy", "Office", "Oval", "Politico",
        "President", "Republican", "Republicans", "Royal", "Senator", "White",
        "University", "Dr.", "Mr.", "Mrs.", "Ms.", "Sen.", "Rep.", "Gov.", "Jiang",
        # 品牌/公司
        "Amazon", "Nissan", "Substack",
        # 连字符复合词：WORD 把 "PRO-TRUMP" 当一个词，整词查表，所以按整词登记
        "pro-Trump",
        # 星期/月份。May 故意不在表里：09-24 那期 6 处 MAY 全是情态动词
        # （"IT MAY NOT BE FAR AWAY"），留着会把 "may have been" 写成 "May have been"。
        # 代价是将来真有日期的 "MAY 5" 会变 "may 5"，比每句都错强。
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
        "January", "February", "March", "April", "June", "July", "August",
        "September", "October", "November", "December",
]:
    CASE_KEEP[_w.lower()] = _w
del _w
# 音乐符号和说话人标记。CBS 有的期次给的是干净的听打稿（09-23），有的给的是广播
# 字幕原稿（09-24）：全大写、`>> Tony:` 标说话人、`♪` 标音乐。这些标记留着的话
# 会原样出现在中文译文里（">> 托尼：…"、"♪ ♪ ♪ ♪" 独占一行），必须清掉。
MUSIC = re.compile(r"[♩-♯]+")
# 先连着说话人名字一起清（`>> Tony:`），再清剩下没跟名字的箭头
SPEAKER_LABEL = re.compile(r">>+\s*(?:[A-Z][A-Za-z.'\-]*\s*){1,3}:")
SPEAKER_MARK = re.compile(r">>+")

# CBS 用内联标记标剧名（`<i>American</i> <i>Idol</i>`），还会用实体转义。
# 不剥掉的话这些尖括号会原样出现在双语字幕栏里。
CUE_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
CUE_ENT = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
           ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "))

# 刻意绕开系统代理：Windows 上 urllib 会去读注册表里的 IE 代理设置，本机 Clash
# 设了系统代理（127.0.0.1:7897），于是每个几百字节的分片都要多花约 9.7 秒——200 片
# 要跑 9 分钟，实测直连只要 20 秒。CBS 的 CDN 直连可达，走代理纯属自找麻烦。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _to_secs(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


# 词（含撇号所有格、缩写里的点和连字符、型号里的数字）：CHINA'S / U.S. / MIAMI-DADE / B-2
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.'’\-]*")

# 整条换掉的短语。栏目名/房间名里的 Evening、News、Room、Portico 都是普通词，塞进
# CASE_KEEP 会把 "breaking news" "this evening" 也一起大写。
CASE_PHRASES = [
    ("atlantic coast", "Atlantic Coast"),
    ("big island", "Big Island"),
    ("cbs evening news", "CBS Evening News"),
    ("cbs mornings", "CBS Mornings"),
    ("cbs news", "CBS News"),
    ("capitol hill", "Capitol Hill"),
    ("east coast", "East Coast"),
    ("east room", "East Room"),
    ("first lady", "First Lady"),
    ("florida highway patrol", "Florida Highway Patrol"),
    ("general assembly", "General Assembly"),
    ("gulf coast", "Gulf Coast"),
    ("ice cream", "ice cream"),
    ("jersey shore", "Jersey Shore"),
    ("jordan brown", "Jordan Brown"),
    ("marine one", "Marine One"),
    ("miami-dade county", "Miami-Dade County"),
    ("middle east", "Middle East"),
    ("ms now", "MS NOW"),
    ("mtv video music awards", "MTV Video Music Awards"),
    ("new england", "New England"),
    ("new jersey", "New Jersey"),
    ("new york", "New York"),
    ("north america", "North America"),
    ("north carolina", "North Carolina"),
    ("north portico", "North Portico"),
    ("outer banks", "Outer Banks"),
    ("patient zero", "Patient Zero"),
    ("prime minister", "Prime Minister"),
    ("sea bright", "Sea Bright"),
    ("south carolina", "South Carolina"),
    ("south florida", "South Florida"),
    ("stanford daily", "Stanford Daily"),
    ("supreme court", "Supreme Court"),
    ("tropical storm", "Tropical Storm"),
    ("united states", "United States"),
]


def is_all_caps(cues):
    """整条轨是不是全大写稿。

    按整期判而不是按单条 cue —— 短的 cue（"HELLO."）本来就是大写，逐条判会冤枉
    正常稿。整期里小写字母占比不到 2% 才算（09-24 实测 0%，09-18/09-23 是 93%）。
    """
    text = "".join(t for _, _, t in cues)
    up = sum(c.isupper() for c in text)
    low = sum(c.islower() for c in text)
    return up + low > 500 and low < (up + low) * 0.02


def _keep_form(word):
    """这个词该写成什么（专有名词/缩写）。不在表里返回 None。

    先按原样查、再掐掉句末的点查（"AMERICA." 要能命中 "america"）；还查不到就
    按所有格拆一次（"CHINA'S" -> "China's"）。
    """
    low = word.lower()
    if low in CASE_KEEP:
        return CASE_KEEP[low]
    bare = low.rstrip(".")                 # "AMERICA." / "D.C.…" 的句末点要原样留下
    if bare != low and bare in CASE_KEEP:
        return CASE_KEEP[bare] + low[len(bare):]
    m = re.match(r"^([a-z][a-z.]*)(['’][a-z]*)$", low)
    if m and m.group(1).rstrip(".") in CASE_KEEP:
        return CASE_KEEP[m.group(1).rstrip(".")] + m.group(2)
    return None


def sentence_case(text):
    """全大写的一句话 -> 正常大小写：句首、I、表里的专有名词大写，其余小写。"""
    low = text.lower()
    out, pos, cap = [], 0, True
    for m in WORD.finditer(low):
        w = m.group()
        keep = _keep_form(w)
        if keep:
            new = keep
        elif w == "i" or w[:2] in ("i'", "i’"):
            new = "I" + w[1:]                # 我 / I'm / I've / I'll / I'd
        elif cap:
            new = w[0].upper() + w[1:]
        else:
            new = w
        out.append(low[pos:m.start()])
        out.append(new)
        pos = m.end()
        # 句末就大写下一个词，但缩写后面的点不算（"Dr. Rachel" / "U.S. Navy"）
        cap = w[-1] in ".!?" and not (w[-1] == "." and ABBREV_END.search(low[:m.end()]))
    out.append(low[pos:])
    s = "".join(out)
    for a, b in CASE_PHRASES:
        s = re.sub(r"\b" + re.escape(a) + r"\b", b, s, flags=re.I)
    return s


def clean_cue(t):
    """先剥真标签（转义过的 &lt;i&gt; 是字面文本，不该剥），再解实体。

    顺手去掉音乐符号和 `>>` 说话人箭头——这两样是广播字幕的排版标记，不是内容。
    """
    t = CUE_TAG.sub("", t)
    for k, v in CUE_ENT:
        t = t.replace(k, v)
    t = SPEAKER_MARK.sub(" ", MUSIC.sub(" ", SPEAKER_LABEL.sub(" ", t)))
    return re.sub(r"\s+", " ", t).strip()


def parse_vtt(text):
    """一段 WebVTT -> [(start, end, text)]，按出现顺序。"""
    lines = text.replace("﻿", "").splitlines()
    out, i = [], 0
    while i < len(lines):
        m = CUE_TIME.search(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _to_secs(*g[:4]), _to_secs(*g[4:])
        i += 1
        buf = []
        while i < len(lines) and lines[i].strip():
            buf.append(lines[i].strip())
            i += 1
        t = clean_cue(re.sub(r"\s+", " ", " ".join(buf)).strip())
        # 清完标记后一个词都没有的（"♪ ♪ ♪ ♪"、"--"）说明这条 cue 本来就只有音乐
        # 或停顿，留着会在中文字幕里变成一整行符号。\w 对中日韩字符同样成立。
        if t and not SOUND_ONLY.match(t) and re.search(r"\w", t):
            out.append((start, end, t))
    return out


def _overlap_len(buf, t, min_overlap=MIN_OVERLAP):
    """buf 的尾部和 t 的头部重合了几个词（不足 min_overlap 就算 0）。"""
    bw, tw = buf.split(), t.split()
    for k in range(min(len(bw), len(tw)), min_overlap - 1, -1):
        if bw[-k:] == tw[:k]:
            return k
    return 0


def _overlap_glue(buf, t, min_overlap=MIN_OVERLAP):
    """把 t 接到 buf 后面，去掉接缝处重复的那一段。

    广播字幕是滚动式的：同一句话会连着显示好几次，每次窗口里多露出几个词，于是
    稿子本身就带重复。直接接起来会得到 "PRESIDENT TRUMP WELCOMES CHINA'S
    PRESIDENT TRUMP WELCOMES CHINA'S XI JINPING…"，中文译文也跟着重一遍。按词
    对齐，尾部和新头重合 min_overlap 个词以上就只补不重合的那截。
    """
    if not buf:
        return t
    k = _overlap_len(buf, t, min_overlap)
    return f"{buf} {t}".strip() if not k else f"{buf} {' '.join(t.split()[k:])}".strip()


def _clamp_rows(rows):
    """把时间轴压成单调不重叠：下一行开始时，上一行就该结束。

    行的时间取的是"首 cue 的开始 ~ 末 cue 的结束"，而滚动字幕重说的时候，下一行的
    第一个 cue 往往在上一行最后一个 cue 结束之前就出现了（09-24 有 215/225 对相邻行
    这样）。不压的话那零点几秒里画面上会同时堆着两句中文，中英同开更是叠成一团。
    """
    out = []
    for s, e, t in rows:
        if out and out[-1][0] < s < out[-1][1]:
            out[-1] = (out[-1][0], s, out[-1][2])
        out.append((s, e, t))
    return out


def _dedup_rows(rows, min_overlap=MIN_OVERLAP):
    """行与行之间再对一次齐，把滚动字幕的跨行重复收掉。

    merge_cues 按句末标点收行，但滚动字幕的下一行常常把上一行原样重说一遍——
    实测 09-24 有 280/401 行这样。当前行开头若和上一行结尾重合就截掉重合的那截
    （那截上一行已经说过了）；整行都只是上一行结尾的重说（哪怕只有一个词，比如
    上一行以 "…A LAVISH STATE DINNER." 结尾、这行就是 "DINNER."）就整行丢掉，
    时间并给上一行，别让字幕空着。

    注意**不能**把截剩的部分并回上一行——那样上一行会越接越长，实测会滚出 175 个
    词、45 秒的长行，反而没法看。
    """
    out = []
    for s, e, t in rows:
        if out:
            pt = out[-1][2]
            tw, pw = t.split(), pt.split()
            if tw and len(tw) <= len(pw) and pw[-len(tw):] == tw:
                out[-1] = (out[-1][0], e, pt)
                continue
            k = _overlap_len(pt, t, min_overlap)
            if k:
                t = " ".join(tw[k:]).strip()
        out.append((s, e, t))
    return out


def merge_cues(cues):
    """把显示级的短 cue 合并成句子级的行 -> [(start, end, text)]。

    CBS 的 cue 是按字幕显示行切的（"Double trouble-- millions\\nbracing for ..."），
    一句话常横跨四五条。不合并的话，Claude 只能拿到半句话去翻译，中文会很破碎，
    而且字幕行数会多一倍。

    切行的位置取**缓冲区里每一个句末标点**，而不是"这条 cue 里有句号就把整行冲掉"：
    608 稿的一条 cue 常是 "DINNER. CHINA'S FIRST STATE VISIT IN" 这种"上句结尾 +
    下句开头"，按后者切会把下句的开头粘在上句尾巴上（09-24 就是这么碎的）。
    空档超过 CAPTION_GAP_SPLIT 也截一刀，免得两段隔着长时间停顿的话粘成一行。

    时间一律留两位小数再交出去：按句末标点切出来的行是 round 过的，直接从缓冲区冲
    出去的行（这句还没说完就到片尾/到了长空档）原来是原始 cue 时间。于是同一行在
    英文轨（直接由这里的 rows 生成）和中文轨（绕一趟 JSON，round 过）上会差个几毫秒
    （09-23 有 63/249 行差 0.005 秒）。视觉上看不出来，但"中英同轴"就不再严格成立。
    """
    rows, start, end, buf = [], None, None, ""
    for cs, ce, t in cues:
        if buf and cs - end > CAPTION_GAP_SPLIT:
            rows.append((round(start, 2), round(end, 2), buf))
            buf = ""
        if not buf:
            start = cs
        buf = _overlap_glue(buf, t)
        end = ce
        pos, span = 0, max(len(buf), 1)
        for m in SENT_END.finditer(buf):
            if buf[m.start()] == "." and ABBREV_END.search(buf[:m.end()]):
                continue                                   # 缩写里的点，不是句末
            piece = buf[pos:m.end()].strip()
            # 一条 cue 里切出好几行时，时间按字符占比摊到各行上。都给整条 cue 的
            # (cs, ce) 的话，这几行会在同一段时间里同时显示（画面上叠两三句中文）。
            cut = start + (end - start) * m.end() / span
            if piece:
                rows.append((round(start + (end - start) * pos / span, 2),
                             round(cut, 2), piece))
            pos, start = m.end(), cut          # 剩下的部分从切口处接着算时间
        if pos:
            buf = buf[pos:].strip()
    if buf:
        rows.append((round(start, 2), round(end, 2), buf))
    rows = _clamp_rows(_dedup_rows(rows))
    if is_all_caps(cues):
        # 全大写是**广播字幕原稿**的排版，不是内容。还原放在最后一步：前面几级清洗
        # 按词对齐重复时，两边都是同一套写法，跟大小写无关，先合后还原更省事。
        rows = [(s, e, sentence_case(t)) for s, e, t in rows]
    return rows


def fetch_cues(subs_url, workers=CAPTION_WORKERS):
    """顺着 subs_en.m3u8 把整条字幕轨拉下来 -> [(start, end, text)]，已按时间排序。

    分片里的时间戳是**全片绝对时间**（第 50 片就从 5:00 开始），不用自己算偏移。
    """
    playlist = http_text(subs_url)
    base = subs_url.rsplit("/", 1)[0]
    chunks = re.findall(r"^(?!#)(\S+\.vtt)\s*$", playlist, re.M)
    if not chunks:
        raise ValueError("字幕播放列表里没解析出分片")

    with ThreadPoolExecutor(workers) as ex:
        texts = list(ex.map(lambda c: http_text(f"{base}/{c}"), chunks))

    cues = []
    for t in texts:
        cues.extend(parse_vtt(t))
    if not cues:
        raise ValueError("字幕分片里没有解析出任何 cue")
    cues.sort(key=lambda c: c[0])
    return cues


# ---------------------------------------------------------------------------
# VTT 输出
# ---------------------------------------------------------------------------
def vtt_time(sec):
    ms = int(round(max(0.0, float(sec)) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def vtt_escape(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_vtt(cues, settings=""):
    """把 cue 列表重新排版成一份干净的 WebVTT。

    重新生成而不是把分片原文拼起来，是为了顺手避掉一个坑：ffmpeg 转出来的 VTT
    会在时间行下面多插一个空行，而按规范"时间行后第一个空行"就代表这条 cue 的
    文本结束——于是 cue 被判成空的，浏览器整条丢掉，字幕一条都显示不出来。
    这里每条 cue 都是自己拼的，不会出现那个空行。

    `settings` 是塞在每条时间行后面的 cue settings，用来定字幕在画面里的位置。
    不传就是浏览器默认的底部。
    """
    lines = ["WEBVTT", ""]
    for s, e, t in cues:
        lines.append(f"{vtt_time(s)} --> {vtt_time(e)}" + (f" {settings}" if settings else ""))
        lines.append(vtt_escape(t))
        lines.append("")
    return "\n".join(lines)
