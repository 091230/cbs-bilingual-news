# CBS Evening News · 双语精听

开源英语新闻学习网站：抓取 **CBS Evening News** 每日晚间新闻，自动生成中英对照字幕与重点词汇，纯静态部署，**视频走 CBS 官方在线流，不占本地磁盘、不占云端存储**。

在线示例：部署后由 GitHub Pages 提供（见下方「部署」）。

---

## 功能

- **每日一期**：自动抓取 CBS 晚间新闻（约 20 分钟完整版），跳过已处理过的期次
- **中英对照字幕**：英文原稿优先取 CBS 官方字幕，中文由 Claude 翻译，逐句对齐、可点击跳转
- **重点词汇**：每期自动抽取约 20 个中高级词汇并标注 CEFR 等级
- **在线高清流播放**：视频直接播放 CBS CDN 的 HLS 流（自适应画质），**本机不存 440MB 视频文件**，网站本身只有几 MB
- **播放器**：双语字幕轨、画面内字幕颜色可调、倍速、全屏、进度条拖拽、键盘快捷键、字幕跟随高亮

## 技术栈

| 层 | 技术 |
|---|---|
| 抓取 | yt-dlp（Python 3.10） |
| 转写 | faster-whisper（无官方字幕时回退） |
| 翻译 | Claude API（Anthropic SDK） |
| 存储 | 本地 SQLite 索引 + JSON 原始产物 |
| 前端 | 原生 HTML/JS + Tailwind CSS（standalone 编译） |
| 部署 | 任意静态托管（GitHub Pages / R2 / OSS 均可） |

## 目录结构

```
├── pipeline.py            # 一键编排：抓取 → 字幕翻译 → 入库
├── fetch_cbs_video.py     # 抓单集：音频 + 源字幕 + HLS 地址（--no-video 跳过本地视频）
├── process_transcript.py  # 字幕翻译 + 词汇抽取 + 导出 latest_cbs.json
├── db.py                  # SQLite 索引（可 --resync 由 JSON 重建）
├── build_site.py          # 生成纯静态 dist/（与云端同构）
├── serve.py               # 本地预览（带 Range 支持与 /api）
├── sync_r2.py             # 可选：同步到 R2 / OSS / COS
├── index.html / player.html   # 站点页面（列表页 / 播放页）
└── data/episodes/<slug>/  # 每期：latest_cbs.json + 字幕 + 音频（视频可选）
```

## 快速开始

### 1. 本地预览

```bash
# 需 Python 3.10（装了 yt-dlp 的那个解释器）
python db.py --resync     # 由已有的 JSON 重建索引
python build_site.py      # 生成 dist/（含 Tailwind 编译）
python serve.py --site    # http://127.0.0.1:8000
```

### 2. 抓取新的一期

```bash
python pipeline.py --count 2        # 默认纯在线流：只下音频 + 字幕，不存视频
python pipeline.py --count 1 --with-video   # 显式要求本地 1080p 视频（约 440MB/期）
python pipeline.py --list           # 查看哪些期已有、哪些待抓
```

跑完 `python build_site.py` 即可在 dist/ 看到新一期。

### 3. 翻译配置

`process_transcript.py` 需要 Claude API key：

```bash
set RELAY_API_KEY=sk-xxx     # 或写入 ~/.claude/settings.json
```

没有 key 时仍可抓取与转写，只是不产出中文翻译。

## 部署（GitHub Pages）

仓库已按「main 存源码、gh-pages 存站点产物」组织：

```bash
# 1. 推 main 分支（源码 + 数据）
git push origin main

# 2. 生成站点产物并推 gh-pages 分支
python build_site.py
git checkout gh-pages && git rm -rf . && cp -r dist/* . && git add -A && git commit -m "deploy: <日期>" && git push origin gh-pages

# 3. 仓库 Settings → Pages → Source 选 gh-pages 分支，保存
```

之后每期更新：`python pipeline.py --count N` → `python build_site.py` → 重复第 2 步推 gh-pages。

## 部署（对象存储）

视频本身不落云端，dist/ 只有几百 KB，任何对象存储都能装：

```bash
set R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
set R2_ACCESS_KEY_ID=xxx
set R2_SECRET_ACCESS_KEY=xxx
set R2_BUCKET=xxx
python sync_r2.py --dry-run   # 先看清单
python sync_r2.py             # 上传
```

## 数据与版权

- 视频、官方字幕稿、标题与日期均来自 **CBS News** 公开页面，版权归 CBS 所有，本站仅供英语学习交流。
- 中文翻译由 Claude 生成，可能存在误差。
- 本项目为学习用途，请勿用于商业分发。

## License

MIT
