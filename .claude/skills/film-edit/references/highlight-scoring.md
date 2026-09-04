# Highlight 篩選 — 怎麼決定素材是不是精華

## 已實作（2026-09-01）

`backend/engines/highlight_engine.py` + `scripts/dump_photos_metadata.py`。

```bash
# 1. (一次) 匯出相簿 favorite / keywords / Apple 影像分數 → scripts/photos_meta.json
.venv/bin/python scripts/dump_photos_metadata.py

# 2. rebuild 時加 --select（輸出到帶後綴的檔名，不覆寫正本 .json）
.venv/bin/python scripts/rebuild_all_projects.py --only <prefix> --select highlight --select-count 45
.venv/bin/python scripts/rebuild_all_projects.py --only <prefix> --select narrative   # 只去重/去爛
.venv/bin/python scripts/rebuild_all_projects.py --only <prefix> --select montage --select-shake
```

- `narrative`：旁白鏡頭全留，只剔除畫面重複 (aHash) / 明顯超晃。
- `highlight`：砍到 `--select-count`（預設 ~總數/3）；每個 Day/篇章保底最高分 2 個，其餘按全域分數補。
- `montage`：關晃動扣分，去重後靠廣度取鏡（影片目前不做去重，見下）。
- `--select-shake`：多跑 ffmpeg 分析晃動當扣分，較慢。
- 輸出：`<prefix>_<mode>.fcpxml` / `_字幕.srt` / `_分鏡腳本.md`。**正本 `.json` 不動**（被砍的鏡頭無法還原）。
- 每個 shot 會寫 `highlight_score` + `keep` + `keep_reason`（僅在記憶體/衍生輸出，normalize 會清掉）。

**已知限制**：這個資料集幾乎每個鏡頭都有 AI 旁白，所以「有無旁白」不是稀有訊號；
montage 的去重只作用在照片（影片沒做 frame hash）。要更準就接 photos_meta 的 favorite/score。

## 現況（設計背景，2026-09）

本專案**目前沒有自動 highlight 評分**。精華是靠三個來源隱性決定的：

1. **你在 iOS 說明欄寫的 caption** — `script_engine._generate_with_gemini` 把「有 caption 的素材」優先全數納入（[script_engine.py](../../../backend/engines/script_engine.py) 約 L71）。等於用打字投票。
2. **是不是 Live Photo / 影片** — 有動態的優先補進來湊數。
3. **Gemini 判斷的起承轉合** — AI 讀 caption + 全片心得長文，自己決定哪裡是開場/轉折/昇華。

`build_option1_full_narrative.py` 的章節（「全片精華：大坡池」）是**手寫**的，非演算。
`generate_options_1_and_2.py` 更是**每組素材都給一鏡**，只把 22 句口白平均灑上去，完全沒篩選。

## 已經抓到但沒拿來評分的訊號

| 訊號 | 來源 | 意義 |
|---|---|---|
| `favorite` | `apple_photos_extractor.py` L203 | 相簿愛心 = 你當下覺得重要 |
| `keywords` | `apple_photos_extractor.py` L202 | Apple 視覺標籤 / 你加的關鍵字 |
| `caption` 長度/內容 | metadata | 寫越多字通常越在意 |
| 晃動比例 | `analyzers/motion_stability.py` | 高 = 難用；低 = 可當主鏡 |
| 顯著性 / 主體清晰度 | `analyzers/smart_crop.py` `_compute_saliency_map` | 有明確主體 vs 一片糊 |
| 是否 Live Photo / 影片時長 | extractors | 有動態、夠長 = 素材價值高 |
| 拍攝時間 / GPS | metadata | 新地點第一張 = 該進片（establishing shot） |

## 建議的評分公式（要實作時用）

```
score(shot) =
    2.0 * has_caption
  + 1.5 * is_favorite
  + 1.0 * (is_live_photo or is_video)
  + 1.0 * saliency_has_clear_subject
  + 0.8 * is_first_at_new_location      # 每個地點/日至少留 1–2 個
  + 0.5 * min(video_duration / 3.0, 1)  # 影片夠長加分，封頂
  - 1.5 * shake_ratio                   # motion_stability 給的晃動比例
  - 1.0 * similarity_to_prev_kept       # 連續太像的去重（phash / 構圖）
```

選法：
- **敘事版（方案1 類）**：門檻式 — score ≥ T 全收，再確保每個「日/地點」至少 2 鏡、每 20–30s 有一個有 caption 的錨點。
- **精華版（3–5 分鐘）**：取 top-N，但強制覆蓋每一天 + 開場 + 結尾 + 每個手寫「精華」章節。
- **蒙太奇版（方案2 類）**：放寬去重、放寬晃動門檻（快剪看不出來），節拍優先於單鏡品質。

## 實作位置

新增 `backend/engines/highlight_engine.py`，在 `rebuild_all_projects.py` 的 pipeline 裡插在
`normalize_storyboard` 之後、`expand_live_photos` 之前：
`normalize → highlight_select(mode=...) → relink → expand_live_photos → stabilize → effects → subtitles → export`

輸出：給每個 shot 寫 `highlight_score` + `keep`(bool) + `keep_reason`；exporter/rebuild 過濾 `keep=False`
（跟現有 `skip` 一樣的機制）。保留分數在 JSON 裡方便之後手動微調。

## 既有工具（可 subprocess 調用，不用自己造）

見 `pipeline-and-tools.md` 階段 0 / 3。與 highlight 直接相關：

| 工具 | 安裝 | 用途 | 接在哪 |
|---|---|---|---|
| **PySceneDetect** `scenedetect[opencv]` v0.7.1 | 已在 .venv | 長影片（>8s）先切成候選鏡頭再評分，別把整段當一鏡 | ✅ `scripts/detect_shots.py` → `shot_candidates.json`；highlight/分鏡可讀來挑片內最好的段 |
| **imagededup** | `pip install imagededup` | 連拍 / 近似重複照片去重，比現在的 aHash 準（PHash + CNN 兩種） | `highlight_engine` 的 `similarity_to_prev_kept` |
| **open_clip_torch** (OpenCLIP) | `pip install open_clip_torch` | 語意去重（"這 3 張都是同一個海灣"）+ 覆蓋度檢查（每個主題至少留 1 鏡）；離線 | `highlight_engine` 去重 / coverage |
| **librosa** | `pip install librosa` | 有音訊的影片抽 onset / RMS 能量峰值 → 當「精彩片段」訊號（歡呼、下坡加速） | `highlight_score` 的影片加分項 |

`--select-shake` 的 ffmpeg 晃動分析建議改吃 `vidstabdetect` 的 `.trf`（見 `pipeline-and-tools.md` 階段 2）。

## 原則

- **favorite 和 caption 是最強訊號** — 那是你當下的判斷，比任何演算法準。
- 演算法的工作是「補齊你沒標的」和「去重 + 剔除爛素材」，不是取代你的選擇。
- 每個地點/每天都要有代表鏡頭，不然觀眾會覺得跳。
- 去重要看構圖相似度，不是只看時間接近（連拍 5 張選 1 張最穩的）。
