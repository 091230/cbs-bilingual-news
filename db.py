# -*- coding: utf-8 -*-
"""SQLite 索引：把每期跑出来的 latest_cbs.json 收进 data/cbs.db，给网站和量产做底。

定位很重要：**data/episodes/<slug>/latest_cbs.json 才是原始产物，这个库只是
可重建的查询索引。** 跑完一期就 ingest 一次（process_transcript.py 结尾会自己调）；
库坏了、换机器、以后加字段，`--resync` 从所有 JSON 全量重建就行，不会丢任何
已经付过钱的东西。所以入库失败只是警告，不该让一场跑白费。

schema 按 Postgres 兼容写——纯 TEXT/INTEGER/REAL，不用 SQLite 专有类型。以后要
搬到云服务器上，要动的地方就三处：`DB_PATH` 换成连接串、`_PH` 换成 "%s"、
`runs.id` 的 AUTOINCREMENT 换成 SERIAL，外加 `INSERT OR REPLACE` 改成
`ON CONFLICT (slug) DO UPDATE`。

用法:
  python db.py --init      # 建库建表
  python db.py --resync    # 以 JSON 为准全量重建（先清后灌）
  python db.py --list      # 列出库里有哪些期
  python db.py             # 建表 + 把每期的 JSON 都 upsert 一遍
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
EPISODES_DIR = os.path.join(DATA_DIR, "episodes")
DB_PATH = os.path.join(DATA_DIR, "cbs.db")

# 参数占位符。SQLite 是 "?"，Postgres 是 "%s" —— 换库时只改这一处。
_PH = "?"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS episodes (
        slug              TEXT PRIMARY KEY,
        video_id          TEXT,
        title             TEXT,
        channel           TEXT,
        duration          INTEGER,
        upload_date       TEXT,
        webpage_url       TEXT,
        hls_master        TEXT,
        hls_subs          TEXT,
        video_file        TEXT,
        subs_en_file      TEXT,
        subs_zh_file      TEXT,
        audio_file        TEXT,
        video_size_mb     REAL,
        transcript_source TEXT,
        transcript_detail TEXT,
        claude_model      TEXT,
        segment_count     INTEGER,
        vocab_count       INTEGER,
        generated_at      TEXT,
        ingested_at       TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS subtitles (
        slug  TEXT    NOT NULL,
        idx   INTEGER NOT NULL,
        start REAL,
        end   REAL,
        en    TEXT,
        zh    TEXT,
        PRIMARY KEY (slug, idx),
        FOREIGN KEY (slug) REFERENCES episodes(slug) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS vocabulary (
        slug     TEXT    NOT NULL,
        position INTEGER NOT NULL,
        word     TEXT,
        level    TEXT,
        meaning  TEXT,
        PRIMARY KEY (slug, position),
        FOREIGN KEY (slug) REFERENCES episodes(slug) ON DELETE CASCADE
    )""",
    # 量产用的运行流水：每跑一期记一行，以后想知道"哪几期翻过了""花了多少""哪期失败过"
    # 直接查这张表，不用去翻终端日志。
    """CREATE TABLE IF NOT EXISTS runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        slug              TEXT,
        stage             TEXT,
        ok                INTEGER,
        transcript_source TEXT,
        claude_model      TEXT,
        segments          INTEGER,
        vocab_count       INTEGER,
        note              TEXT,
        ran_at            TEXT
    )""",
]


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    """开一个新连接。请求/任务各开各的：sqlite3 的连接不能跨线程用，
    而上的又是 ThreadingHTTPServer —— 共用一个连接迟早撞上 "SQLite objects
    created in a thread can only be used in that same thread"。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # 写入时读不受阻
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def upsert_episode(conn, payload):
    """把一份 latest_cbs.json 写进库。整期一个事务：先删子表再插，重复跑结果一样。"""
    meta = payload.get("meta") or {}
    video = payload.get("video") or {}
    subs = payload.get("subtitles") or []
    vocab = payload.get("vocabulary") or []
    slug = payload.get("slug") or meta.get("slug")
    if not slug:
        raise ValueError("这份 JSON 里没有 slug（顶层或 meta 里都找不到）")

    row = {
        "slug": slug,
        "video_id": video.get("video_id"),
        "title": video.get("title"),
        "channel": video.get("channel"),
        "duration": video.get("duration"),
        "upload_date": video.get("upload_date"),
        "webpage_url": video.get("webpage_url"),
        "hls_master": video.get("hls_master"),
        "hls_subs": video.get("hls_subs"),
        "video_file": video.get("local_video"),
        "subs_en_file": video.get("local_subs"),
        "subs_zh_file": "subs_zh.vtt",
        "audio_file": video.get("audio_file"),
        "video_size_mb": video.get("video_size_mb"),
        "transcript_source": meta.get("transcript_source"),
        "transcript_detail": meta.get("transcript_detail"),
        "claude_model": meta.get("claude_model"),
        "segment_count": meta.get("segment_count") or len(subs),
        "vocab_count": len(vocab),
        "generated_at": meta.get("generated_at"),
        "ingested_at": now(),
    }
    cols = list(row)
    with conn:
        conn.execute(f"DELETE FROM subtitles WHERE slug = {_PH}", (slug,))
        conn.execute(f"DELETE FROM vocabulary WHERE slug = {_PH}", (slug,))
        conn.execute(
            f"INSERT OR REPLACE INTO episodes ({', '.join(cols)}) "
            f"VALUES ({', '.join([_PH] * len(cols))})",
            tuple(row[c] for c in cols))
        conn.executemany(
            f"INSERT INTO subtitles (slug, idx, start, end, en, zh) "
            f"VALUES ({', '.join([_PH] * 6)})",
            [(slug, s.get("index"), s.get("start"), s.get("end"),
              s.get("en"), s.get("zh")) for s in subs])
        conn.executemany(
            f"INSERT INTO vocabulary (slug, position, word, level, meaning) "
            f"VALUES ({', '.join([_PH] * 5)})",
            [(slug, i, v.get("word"), v.get("level"), v.get("meaning"))
             for i, v in enumerate(vocab)])
    return {"subtitles": len(subs), "vocabulary": len(vocab)}


def record_run(conn, slug, stage, ok=True, note=None, meta=None, counts=None):
    meta = meta or {}
    counts = counts or {}
    with conn:
        conn.execute(
            f"INSERT INTO runs (slug, stage, ok, transcript_source, claude_model, "
            f"segments, vocab_count, note, ran_at) "
            f"VALUES ({', '.join([_PH] * 9)})",
            (slug, stage, 1 if ok else 0, meta.get("transcript_source"),
             meta.get("claude_model"), counts.get("subtitles"),
             counts.get("vocabulary"), note, now()))


def ingest_file(path, conn=None, log_run=True):
    """读一份 latest_cbs.json 入库。

    log_run=False 用于 resync —— 那只是拿文件重建索引，不是"又跑了一遍翻译"，
    真记进 runs 的话流水就不可信了（看着像重复花了好几次钱）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        init_schema(conn)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if not payload.get("slug"):
            # 早期导出的 JSON 顶层没写 slug，目录名就是期号
            payload["slug"] = os.path.basename(os.path.dirname(os.path.abspath(path)))
        counts = upsert_episode(conn, payload)
        if log_run:
            record_run(conn, payload["slug"], "transcript", ok=True,
                       meta=payload.get("meta"), counts=counts)
        return counts
    finally:
        if own:
            conn.close()


def episode_json_path(slug):
    return os.path.join(EPISODES_DIR, slug, "latest_cbs.json")


def resync():
    """以 JSON 为准全量重建。先清空期次/字幕/词汇（runs 流水保留）。"""
    conn = connect()
    try:
        init_schema(conn)
        with conn:
            conn.execute("DELETE FROM subtitles")
            conn.execute("DELETE FROM vocabulary")
            conn.execute("DELETE FROM episodes")
        done, skipped = [], []
        for slug in sorted(os.listdir(EPISODES_DIR)) if os.path.isdir(EPISODES_DIR) else []:
            path = episode_json_path(slug)
            if os.path.exists(path):
                counts = ingest_file(path, conn, log_run=False)
                done.append((slug, counts["subtitles"], counts["vocabulary"]))
            else:
                skipped.append(slug)
        return done, skipped
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 读取（serve.py 的 /api 用）
# ---------------------------------------------------------------------------
def list_episodes():
    conn = connect()
    try:
        init_schema(conn)
        rows = conn.execute(
            "SELECT slug, title, duration, upload_date, video_size_mb, "
            "transcript_source, claude_model, segment_count, vocab_count, "
            "generated_at, webpage_url FROM episodes "
            "ORDER BY upload_date DESC, slug DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_episode(slug):
    """还原成前端要的形状：{slug, video, subtitles, vocabulary, meta}。"""
    conn = connect()
    try:
        init_schema(conn)
        ep = conn.execute(f"SELECT * FROM episodes WHERE slug = {_PH}", (slug,)).fetchone()
        if not ep:
            return None
        subs = conn.execute(
            f"SELECT idx, start, end, en, zh FROM subtitles WHERE slug = {_PH} "
            f"ORDER BY idx", (slug,)).fetchall()
        vocab = conn.execute(
            f"SELECT word, level, meaning FROM vocabulary WHERE slug = {_PH} "
            f"ORDER BY position", (slug,)).fetchall()
        return {
            "slug": slug,
            "video": {
                "video_id": ep["video_id"], "title": ep["title"],
                "channel": ep["channel"], "duration": ep["duration"],
                "upload_date": ep["upload_date"], "webpage_url": ep["webpage_url"],
                "hls_master": ep["hls_master"], "hls_subs": ep["hls_subs"],
                "local_video": ep["video_file"], "local_subs": ep["subs_en_file"],
                "local_subs_zh": ep["subs_zh_file"], "audio_file": ep["audio_file"],
                "video_size_mb": ep["video_size_mb"],
            },
            "subtitles": [{"index": s["idx"], "start": s["start"], "end": s["end"],
                           "en": s["en"], "zh": s["zh"]} for s in subs],
            "vocabulary": [dict(v) for v in vocab],
            "meta": {
                "slug": slug, "generated_at": ep["generated_at"],
                "claude_model": ep["claude_model"],
                "transcript_source": ep["transcript_source"],
                "transcript_detail": ep["transcript_detail"],
                "segment_count": ep["segment_count"],
            },
        }
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="把各期的 latest_cbs.json 收进 SQLite")
    ap.add_argument("--init", action="store_true", help="只建库建表")
    ap.add_argument("--resync", action="store_true", help="以 JSON 为准全量重建")
    ap.add_argument("--list", action="store_true", help="列出库里的期次")
    args = ap.parse_args()

    if args.resync:
        done, skipped = resync()
        print(f"重建 {DB_PATH}")
        for slug, n, v in done:
            print(f"  {slug}  字幕 {n} 句  词汇 {v} 个")
        for slug in skipped:
            print(f"  {slug}  跳过（还没有 latest_cbs.json）")
        print(f"共 {len(done)} 期")
        return

    if args.list:
        rows = list_episodes()
        for r in rows:
            print(f"  {r['slug']}  {r['segment_count']} 句  {r['vocab_count']} 词  "
                  f"{r['transcript_source']}  {r['generated_at']}")
        print(f"共 {len(rows)} 期")
        return

    conn = connect()
    try:
        init_schema(conn)
    finally:
        conn.close()
    print(f"建表完成: {DB_PATH}")
    if args.init:
        return

    done = 0
    for slug in sorted(os.listdir(EPISODES_DIR)) if os.path.isdir(EPISODES_DIR) else []:
        path = episode_json_path(slug)
        if os.path.exists(path):
            counts = ingest_file(path)
            print(f"  {slug}  字幕 {counts['subtitles']} 句  词汇 {counts['vocabulary']} 个")
            done += 1
    print(f"入库 {done} 期")


if __name__ == "__main__":
    main()
