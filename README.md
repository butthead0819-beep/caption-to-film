# caption-to-film

**把 iOS 照片「說明欄」裡的隻字片語，剪成一支有旁白、有字幕、有運鏡的紀錄片。**

> Turn the captions you wrote on your iPhone photos — plus Live Photos, GPS, and
> timestamps — into an AI-assisted documentary edit: storyboard, voiceover,
> reading-speed subtitles, Ken Burns moves, shake trimming, and an FCPXML /
> DaVinci Resolve project (or a rendered MP4 for iMovie).

這是一個給 iPhone / Mac 使用者的**個人紀錄片剪輯 pipeline**。它讀你相簿裡每張照片的
「說明欄（Caption / 想法 / 記憶）」、拍攝時間、GPS、相機、Live Photo 動態短片，經過
多模態 AI 與構圖分析，編出分鏡腳本與旁白，最後輸出可匯入 Final Cut / DaVinci Resolve
的 FCPXML，或直接算一支給 iMovie 收尾的 MP4。

---

## ⚠️ 先讀這段（scope 與限制）

- **macOS only。** 核心輸入靠 `osxphotos` 直讀 Apple Photos 本機相簿，以及
  `ffmpeg-full`（vidstab / libass）。Linux / Windows 跑不起來。
- **這是從一支真實的片子長出來的。** 原始用例是一趟多日家庭單車旅行。`backend/engines/`
  和 `scripts/` 裡的 prompt、節奏參數、地名正規化表，都是往「第一人稱、留白多、不煽情」
  那種個人紀錄片調的。換一種片型要自己改 prompt。參考片的實際編劇腳本在
  [`examples/reference-film/`](examples/reference-film/)，當 worked example 讀。
- **需要 Google Gemini API key**（編劇、旁白、場景標籤）。有離線備援（SmartCrop 構圖、
  OpenCLIP 場景標籤、啟發式旁白），但完整效果要 key。地圖片段另需 Google Maps key。
- **不是一鍵成片。** 流程裡有兩個「人要看」的關卡：寫手寫備註、過稿。中間很多步驟
  預期由一個 coding agent（例如 Claude Code）照著 [`.claude/skills/film-edit/`](.claude/skills/film-edit/)
  的工作流驅動。

---

## 安裝

```bash
python3 -m venv .venv               # 專案用 Python 3.14
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-tools.txt   # 選用：分鏡切點 / ML 標記 / 音訊分析

# 畫面穩定 / 字幕燒錄需要帶 vidstab + libass 的 ffmpeg
brew install ffmpeg
# 若主 ffmpeg 沒有 vidstab：brew install ffmpeg-full（keg-only）

cp .env.example .env                # 填 GEMINI_API_KEY（GOOGLE_MAPS_API_KEY 選用）
cp edit_project.example.json edit_project.json   # 填 media_dir 與 prefix
```

`edit_project.json` 是「目前在剪哪一支片」：

```json
{ "media_dir": "~/Pictures/my_trip", "prefix": "my_trip" }
```

所有 `scripts/` 都從這裡讀素材夾與輸出檔名前綴。

---

## 用法

### CLI（單次生成）

```bash
.venv/bin/python cli.py --list-albums

.venv/bin/python cli.py \
  --album "2024京都賞楓" \
  --prompt "京都賞楓旅行，口白真誠溫馨，重點在喝抹茶與漫步清水寺" \
  --style "自然感人旅行Vlog" --ratio 16:9 \
  --output my_script.fcpxml          # 副檔名支援 .md / .json / .fcpxml
```

### Web UI

```bash
.venv/bin/uvicorn backend.app:app --reload --port 8000
# 開 http://localhost:8000
```

### Pipeline（可重複執行）

```bash
.venv/bin/python scripts/generate_project.py --reflection "<素材夾>/reflection.md"   # Gemini 編劇 → <prefix>.json
.venv/bin/python scripts/probe_folder_metadata.py                                   # GPS / 拍攝時間 / 人臉框
.venv/bin/python scripts/rebuild_all_projects.py [--stabilize]                      # → FCPXML + SRT + Markdown
.venv/bin/python scripts/render_video.py --for-imovie                              # → MP4（字幕燒進去當口白稿）
```

主要 script：

| script | 階段 | 做什麼 |
|---|---|---|
| `detect_shots.py` | 0 | PySceneDetect 找長片內的切點，供精華篩選 / 晃動剪除當前置 |
| `generate_project.py` | 1 | Gemini 從素材 + 心得長文編 storyboard |
| `segment_scenes.py` | 1 | 依曆日 / GPS 大跳點 / 標籤切章節 |
| `probe_folder_metadata.py` / `dump_photos_metadata.py` | 2 | 抽 EXIF GPS / 時間 / 人臉框 / Apple ML 標籤 |
| `local_scene_labels.py` / `gemini_scene_labels.py` | 2 | 場景標籤（離線 OpenCLIP 或 Gemini）→ 調色 + LLM 主題 |
| `sort_shots.py` | 3 | 章節內依拍攝時間排序 |
| `stabilize_clips.py` | 3 | ffmpeg vid.stab 產畫面穩定副本（原檔不動） |
| `make_map_clips.py` | 4 | GPS → 衛星地圖 establishing 片段 + 海拔剖面 |
| `apply_voiceover.py` | 6 | 依章節預算重寫旁白（畫面是主角、旁白填空隙、章接縫留鉤子） |
| `build_review_packet.py` | 5 | 產 HTML 過稿包（縮圖、旁白預算檢查、分鏡表、批註格式） |
| `apply_notes.py` | 5→1 | 把過稿批註 parse 回 storyboard |
| `rebuild_all_projects.py` | 7 | relink 素材 + 重切字幕 + 匯出 FCPXML / SRT / MD |
| `render_video.py` | 8 | 逐鏡渲染 → concat → 章間溶接 → 燒字幕 → x264 |
| `qa_timeline.py` / `qa_render.py` | 8 | 靜態校驗時間軸 / 成片響度與接縫自檢 |
| `resolve_auto_grade.py` / `resolve_build_timeline.py` | 8 | 在 DaVinci Resolve 內建時間軸 / 自動一級調色 |

---

## 剪輯知識庫

「怎麼剪」收在 [`.claude/skills/film-edit/`](.claude/skills/film-edit/) —— 不寫死在腳本裡：

- `SKILL.md` — pipeline 地圖、核心剪輯原則、風格設定檔、已知地雷清單
- `references/highlight-scoring.md` — 怎麼決定素材是精華
- `references/montage-and-abroll.md` — 蒙太奇手法、A/B-roll 疊軌
- `references/subtitles-zh.md` — 中文字幕規格、主述口氣、DaVinci Resolve SRT 流程
- `references/fcpxml-resolve.md` — FCPXML 結構與 Resolve 匯入地雷（Media Offline 等）
- `references/photo-metadata.md` / `map-clips.md` / `pipeline-and-tools.md` / `bookends.md`

它是為 Claude Code 的 skill 機制寫的，但當純文件讀也完全可以。

---

## 為什麼字幕 / 旁白這麼講究「留白」

核心原則：**畫面是主角，旁白只是填進畫面之間的空隙。** 純風景 / 蒙太奇 / 情緒定格
可以整段不放字。第一人稱旁白裡「我 / 我們」能省就省。地名日期交給畫面字卡，不用旁白念。
細節見 `references/subtitles-zh.md`。

---

## 測試

```bash
.venv/bin/python -m pytest tests/ -q
```

## 貢獻

歡迎 issue / PR。要動 storyboard / 節奏 / 字幕 / FCPXML 前，先讀對應的
`references/*.md`；踩到新的匯入地雷，把它寫回去。

## 授權

[Apache License 2.0](LICENSE)。
