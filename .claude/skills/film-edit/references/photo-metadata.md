# 照片可抽取的資料與剪輯應用

## 產生 `scripts/photos_meta.json`（key = 檔名 stem，小寫；共用載入器 `backend/util/photos_meta.py`）

| 腳本 | 速度 | 內容 | 何時用 |
|---|---|---|---|
| `scripts/probe_folder_metadata.py` | 快（秒） | 讀**單一資料夾檔案的內嵌 metadata**：EXIF/QuickTime GPS/拍攝時間/相機型號（影片用 pymediainfo 讀 `comapplequicktimecreationdate`/`model`）、XMP 人臉框、panorama。`--geocode` 加地名 | **預設**，你只處理一個資料夾 |
| `scripts/gemini_scene_labels.py` | 中（每張 ~3s，受 Gemini 免費層 20/分+每日上限） | 用 Gemini 產 **場景標籤 labels + mood + has_people**（取代 Apple ML，不用 Photos App / 不佔空間）。可中斷續跑 | **推薦**：要調色 preset + LLM 主題時 |
| `scripts/dump_photos_metadata.py` | 慢（開整個 Photos DB 數分鐘，佔 DB/縮圖空間） | Apple ML 場景標籤 + 美學分數 score_detail + favorite + persons 名字 | 磁碟夠、想要 Apple 美學分數時，`--merge` |

> `osxphotos` 讀的是 Mac Photos App 的本機 DB，**不下載原檔**（即使「最佳化 Mac 儲存空間」開著）；
> 但前提是照片要在這台 Mac 的 Photos App 裡，且 DB+縮圖本身仍佔幾 GB。空間緊 → 用 `gemini_scene_labels.py`。

消費者（有就讀、沒有就略過）：`highlight_engine`（favorite/persons/faces/score/screenshot/exclude/edited）、
`abroll_engine`（persons/faces/labels 分 A/B-roll）、`effects_engine`（faces → Ken Burns 對焦）、
`grading_engine`（labels/時段 → 調色 sidecar）、`script_engine`（labels/persons → LLM prompt）。

**已實作**：faces→smart crop 對焦、labels→grade hints、labels/persons→script_engine prompt、GPS→map clips。

## 每筆欄位

| 欄位 | 內容 | 已用在 | 還能做 |
|---|---|---|---|
| `favorite` | 相簿愛心 | highlight +1.5 | — |
| `keywords` | 使用者關鍵字 | highlight +0.8 | LLM 主題 |
| `title` / `descr` | Photos App 說明欄（比 EXIF 可靠） | — | 取代 / 補強 caption 餵 LLM |
| `persons` | 入鏡人物名字 | highlight +1.0、abroll→A-roll | 「父子同框」= 情感重點加分 |
| `faces` | 人臉框 `{name,x,y,w}` 正規化 | abroll→A-roll | **smart crop 焦點對準人臉**（比顯著性猜準） |
| `score` | Apple ML 綜合分 (overall, ~-1..1) | highlight fallback | — |
| `score_detail` | `curation` / `promotion` / `well_timed_shot` … 子分數 | highlight（優先 curation/promotion） | `well_timed_shot` 挑決定性瞬間 |
| `labels` | Apple ML 場景標籤 (bicycle / sunset / food / mountain) | abroll 角色分類 | **LLM 主題、濾鏡/調色 preset、smart crop 規則**（見下） |
| `activities` | Apple ML 活動 (Cycling / Hiking) | — | 章節命名、配樂情緒 |
| `place` | `{name, city, country}` 逆地理 | — | 章節分段、地名下標、地圖片段（見 `map-clips.md`） |
| `gps` | `{lat, lon, alt}` | — | 路線圖、距離/爬升下標、aerial zoom（見 `map-clips.md`） |
| `taken` | 拍攝時間 ISO | — | 時間間隔 >N 小時→切 Day；時段→黃金時刻加分 + 暖調 |
| `kind.burst` | 連拍組 | — | 只留一張（真去重） |
| `kind.panorama` | 全景 | — | 強制左右平移運鏡 |
| `kind.screenshot` | 截圖 | highlight -2.0 | 通常排除 |
| `kind.slow_mo` / `time_lapse` | 慢動作 / 縮時 | — | 特殊 retime / 當轉場 |
| `edited` | 使用者修過圖 | highlight +0.5 | — |
| `exclude` | hidden / 已刪除 | highlight 強制剔除 | — |

## Apple ML labels 的三個進階用途（已實作）

標籤來源二選一：`scripts/gemini_scene_labels.py`（Gemini，推薦）或 `dump_photos_metadata.py`（Apple）。
兩者都寫進 `photos_meta.json` 的 `labels` + `has_people` + `mood`。

### 1. 強化 LLM 主題 — `script_engine._generate_with_gemini`
- 每則 item 附 `apple_scene_labels` + `people_in_frame`
- prompt 加「全片場景主題比重」直方圖（`labels` Counter top-15）→ AI 抓章節骨架與各段情緒
- 只在新 generation（cli.py）生效；rebuild 不重跑 AI

### 2. 調色 preset — `grading_engine.py` → `<prefix>_grade_hints.json`
label / 時段 → `{slope_mul, offset_add(RGB), sat_mul, note}`，`resolve_auto_grade.py --hints <path>`
會把它**乘/加在灰世界白平衡結果之上**（不是取代）。preset 表在 `grading_engine._PRESETS`：
sunset→暖+抬黑位、ocean→微冷+提藍綠、forest/rice→綠意、night→冷陰影+壓亮、food→暖+高飽和。
沒有 ML labels 時，退而用 `taken` 時段推 golden hour / night。

### 3. Smart crop 對焦 — `effects_engine.apply_effects`
- `photos_meta` 有 `faces` → Ken Burns 焦點直接設在人臉框重心（`util/photos_meta.face_center`），
  跳過顯著性猜測（實測方案1 有 25 張改用人臉對焦）
- 待做：label 條件裁切（sunset 保地平線、bicycle 保單車、panorama 強制平移）

## 標籤來源第三選項：本地視覺模型（Gemini 額度耗盡 / 離線）

`gemini_scene_labels.py` 撞每日上限時的備援，都寫同一份 `photos_meta.json` 的 `labels`/`has_people`/`mood`：

| 工具 | 安裝 | 產出 | 備註 |
|---|---|---|---|
| **OpenCLIP** (`open_clip_torch`) | `pip install open_clip_torch` | 零樣本場景標籤（給一組候選詞算相似度）、文字搜圖 | 最輕，詞彙要自己對齊 `grading_engine._PRESETS` |
| **moondream2** / **llava** (Ollama) | `ollama pull moondream` | 影像 caption + 是否有人 | 免 GPU 也能跑，慢 |
| **Florence-2** (transformers) | `pip install transformers` | caption + 物件偵測框（`<OD>` task） | 框可直接餵 smart crop |
| **YOLOv8** (`ultralytics`) | `pip install ultralytics` | person / bicycle / dog… 偵測框 + count | `has_people` 與 abroll 分類最硬的訊號；框給 smart crop |

✅ 已寫 `scripts/local_scene_labels.py`：`--backend {openclip,yolo,both}`，介面同 `gemini_scene_labels.py`
（同一份 photos_meta.json、只補沒 labels 的、可 `--force`）。openclip=場景標籤+mood+time_of_day+has_people；
yolo=YOLOv8 物件框（has_people 較準）；both 取聯集。見 `pipeline-and-tools.md`「已寫好的接合腳本」。
