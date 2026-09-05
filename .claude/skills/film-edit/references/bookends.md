# 片頭 / 片尾：獨立段落，另外設計、另外挑素材（2026-09-04）

## 為什麼要獨立

以前 `highlight_engine` 只 `force(0, "開場")` / `force(n-1, "結尾")` 硬留頭尾兩顆鏡頭，
其餘 VO 預算、章節接縫、精華篩選都把它們當普通 body 鏡頭。結果片頭片尾沒有「設計」，
就是正片的第一顆和最後一顆。

**改法**：給鏡頭加 `segment` 欄位，片頭片尾變成**獨立段落**，由 `scripts/build_bookends.py`
從**獨立素材池**組出來，不走 highlight_engine 的常規流程，VO 也另一套規則。

## `segment` 欄位

| 值 | 意義 | VO | 進 highlight select |
|---|---|---|---|
| `None` / `body` | 正片（預設） | 章節預算 | 照常 |
| `cold_open` | 片頭風景蒙太奇（正片前） | 無（或 1 句 hook） | 強制留、不參與去重 |
| `title` | 片名卡 | 無 | 強制留 |
| `recap` | 片尾回顧蒙太奇 | 無 | 強制留、不參與去重 |
| `outro` | 定格情緒句點（回顧後那顆） | 保留昇華句，獨立預算 8s×6×0.85 | 強制留 |
| `return` | 返家鏡頭 | 1 句或留白 | 強制留 |
| `endcard` | 片尾卡（路線圖 + 里程 + 題獻） | 無 | 強制留 |
| `bloopers` | 花絮（疊 credits） | 無 | 強制留、不參與去重 |

`segment` 不在 `rebuild_all_projects._DERIVED_KEYS` → `normalize_storyboard` 會保留它。
`build_bookends.py` 產生的**新鏡頭**（卡片、蒙太奇短版拷貝）另打 `_bookend_generated: true`，
重跑時先全部刪掉再重建 → 冪等。

## 成片結構

```
[cold_open ×N]      風景蒙太奇短版拷貝，~11–15s，純環境音+音樂，長→短
[title]             片名卡：全台灣路線圖 + 紅線畫到一半 + 片名，~4s
── 正片（body，不動）──
[recap ×M]          每天代表鏡頭短版拷貝，~20s，無旁白，長→短→更短→停頓
[outro]             正片最後一顆（父親節答案）移到這、拉長到 8s、保留昇華旁白
[return]            返家鏡頭，~6s
[endcard]           片尾卡：完整整圈紅線 + 「環島 N 公里 · 8 天 · 2026 夏」+ 題獻
[bloopers ×3]       花絮短片段，疊工作人員字卡
```

## 素材怎麼挑（**專案無關的準則** — 這才是重點，換專案要能自動找到對應素材）

全部訊號來自 `photos_meta.json`（`labels` / `mood` / `has_people` / `favorite` / `gps` /
`place.name` / `taken`）+ storyboard 的 `highlight_score` / `shot_type` / `_hl`。**不寫死檔名。**

### cold_open — 片頭風景蒙太奇
候選池：全 storyboard，排除 `_hl.exclude`、排除晃動大（`_hl.shake ≤ -1`）、排除 Live 微動態(A)、
排除已被其他 segment 用掉的。
評分（每顆）：
- `+highlight_score`
- `+0.8` 若 `shot_type` 含「全景 / 遠景 / Wide」（establishing 感）
- `+0.6` 若 `has_people == False`（純風景開場）
- `+0.5` 若 `favorite`
- `+0.3` 若 `taken` 落在整趟前 1/3（片頭 = 「要去哪」不是「發生了什麼」）

**多樣性貪婪挑選**（關鍵）：每挑一顆，對剩下的每顆做懲罰 —
- 同 `place.name`（或 GPS 20km 內）已選過 → `-1.2`
- `labels` 與已選集合的 Jaccard 重疊 > 0.3 → `-0.8`

→ 強制地理 + 場景類型分散。挑滿 `--open-count`（預設 4），照 `taken` 排序（旅程方向感）。
時長曲線：3.5 / 3.0 / 2.5 / 2.5s。

### recap — 片尾回顧蒙太奇
依 `taken` 曆日分組（`taken.date()`）→ 每天取 `highlight_score` 最高、且未被別的 segment 用掉的一顆。
沒 GPS/日期的專案 → 退回用 `scene_id` 分組。上限 `--recap-max`（預設 8）。
時長曲線：前段每顆 2.2s，最後三顆 1.4 / 1.0 / 0.6s（停頓），接著就是 outro 定格。

### outro — 定格情緒句點
= 正片 body 的最後一顆（`normalize` 後 Live Photo 已併成一顆）。從 body 尾端 pop 出來 →
`segment=outro`、`duration_seconds` 拉到 `--outro-sec`（8）、保留它原本的 `voiceover`（昇華句）。
放在 recap 之後。

### return — 返家鏡頭
1. 手動優先：`photos_meta` 的 `keywords` 含 `返家` / `home` 的鏡頭。
2. 啟發式：最後一個曆日的最後一顆**影片**；或 `labels` 含 `house`/`home`/`night` 且 `mood` 沉靜。
3. 找不到就**跳過**（不是每個專案都有返家素材）。

### bloopers — 花絮
1. 手動優先：`keywords` 含 `花絮` / `NG` / `blooper`。
2. 啟發式：`highlight_score` 中低（-0.5 ~ 0.5）、`shot_type` 特寫/中景、`has_people`、
   `mood` 含 搞笑/尷尬/驚訝/無奈，且**沒被 body select 選中**（`keep == False`）。取 3 顆。
3. 找不到就跳過。

### title — 片名段
**優先用燃燒火線動畫**：`scripts/route_burn.py` 產的 `map_clips/route_burn.mp4`（見 `map-clips.md`
「燃燒火線片頭」）。`build_bookends.py` 偵測到就用它當 `title` 段（video、`--title` 已把片名燒在拉遠後的尾段）。
沒有 mp4 → 退回 PIL 靜態片名卡 `bookends/title_card.jpg`（背景 = `route_overview.jpg`，片名 + 副標，自動縮字級）。

### endcard — 片尾卡（PIL 生成）
背景 = `map_clips/route_overview.jpg`。完整整圈紅線 + 「環島 約 {km} 公里 · {日期範圍}」+ sign_off + 題獻。
- `km` 自動算：storyboard 有 GPS 的鏡頭照 `taken` 排序，相鄰 haversine 加總。
- 文字來自 storyboard 新欄位 `bookend_config`（見下）。
- 沒有 `route_overview.jpg` → 用純黑底 + 白字，仍可跑（印警告叫人先跑 `make_map_clips`）。
- 存到 `bookends/title_card.jpg` / `end_card.jpg`，當 photo 鏡頭插進 storyboard。

## storyboard 新欄位 `bookend_config`（放 script 頂層，跟 `soundtrack_design` 同層）

```json
"bookend_config": {
  "film_title": "逐光而行",
  "film_subtitle": "副標",
  "dedication": "獻詞",
  "sign_off": "攝於 2026 夏",
  "open_count": 4,
  "recap_max": 8,
  "outro_sec": 8.0,
  "bloopers": true
}
```

Gemini 編劇（`script_engine`）產 storyboard 時就可以填 `film_title` / `dedication`；
其餘用預設。缺整個 `bookend_config` → 全用預設、`film_title` 退回 `project_title`。

## VO 規則（接 `subtitles-zh.md` 的主述校訂）

`regenerate_voiceover` 分 scene 算預算時：
- `segment in (cold_open, title, recap, endcard, bloopers, return)` 的鏡頭**排除在章節預算外**，
  且 voiceover 一律清空（畫面 / 音樂 / 字卡自己講）。例外：`cold_open` 若 storyboard 既有 hook 句就留。
- `segment == outro`：給它**獨立預算** `outro_sec × 6 × 0.85`，只放那句昇華，走主述校訂。
- body 的章節預算計算要跳過所有非 body 鏡頭（否則片頭片尾的秒數會被算進某章，預算爆）。

## 用法

```
# 排序 / 精華篩選之後、地圖過場之後、--regen-vo 之前
.venv/bin/python scripts/make_map_clips.py --storyboard <prefix>.json --per-day   # 先要有 route_overview
.venv/bin/python scripts/route_burn.py --storyboard <prefix>.json --title "片名"   # 燃燒火線片頭（選配、~70s）
.venv/bin/python scripts/build_bookends.py <prefix>            # dry-run：印出自動挑的片頭片尾鏡頭
.venv/bin/python scripts/build_bookends.py <prefix> --write    # 寫回正本 .json（冪等，先備份）
.venv/bin/python scripts/build_review_packet.py <prefix>       # 審閱包會多「片頭 / 片尾」區塊
# notes.txt 覆核：  片頭+：IMG_1234   片頭-：IMG_5678   返家：IMG_7247   花絮+：IMG_9999
.venv/bin/python scripts/apply_notes.py notes.txt --write
.venv/bin/python scripts/rebuild_all_projects.py --regen-vo
```

`build_bookends.py` 寫回正本 `.json`（跟 `--segment` / 單獨 `--regen-vo` 一樣是「正式重建」），
因為它會動鏡頭（加卡片、加蒙太奇拷貝、pop 出 outro）。跑前自動備份 `.json.bak-<時間>`。

## 待接（TODO，尚未實作）

- `build_review_packet.py`：片頭 / 片尾區塊 + 縮圖 + 「這幾顆是自動挑的，notes.txt 可改」提示。
- `apply_notes.py`：`片頭+/-`、`片尾+/-`、`返家：`、`花絮+/-` 動作 → patch `segment` 欄位。
- ~~`title` 紅線動畫~~ → 已做（`route_burn.py`）。剩 `endcard` 可考慮也做成「整圈快速燒起來」的動畫。
- `render_video.py` / `fcpxml_exporter.py`：`bloopers` 疊工作人員字卡軌。
- 音樂：片頭蒙太奇、recap 需要獨立音樂段（階段 6），目前只留環境音。
