# 剪輯 pipeline 分階段 + 可調用的既有工具

> 使用者原本切 5 階段。這裡補成 9 階段（加了 ingest / 精華排序 / 配音配樂 / 調色修音 / 匯出校驗 / 回饋），
> 每階段列出「網路上既有、agent skill 可直接 subprocess 調用」的開源工具，避免自己重造。
> 原則：**LLM 決策留給 Gemini/Claude，機械性重活外包給這些工具。**

---

## 階段總覽

| # | 階段 | 產出 | 人類介入 |
|---|---|---|---|
| 0 | 素材匯入與整理 (ingest) | 乾淨素材清單 + proxy + 分鏡切點 | 否 |
| 1 | 集成人類備註：編劇與篇章設計 | 章節結構 + 分鏡 storyboard + 口白草稿 | **輸入**（備註/心得長文） |
| 2 | 處理素材：ML 標記、晃動偵測、裁切 | 每素材 labels/faces/晃動比例/裁切框 | 否 |
| 3 | 精華篩選與排序 (select + sequence) | 進片清單 + 鏡頭順序 + 每鏡時長 | 否（可覆核） |
| 4 | 集成素材 metadata、產生額外素材 | 地圖片段/海拔剖面/字卡/總覽圖 | 否 |
| 5 | 給人類過一版：腳本 + 關鍵影像 | 審閱包（PDF/網頁：分鏡表 + 縮圖 + 口白） | **審核閘門** |
| 6 | 配音、配樂、音效 (audio bed) | 旁白音軌 + 音樂 bed + ducking 標記 | 選配（挑曲風） |
| 7 | 產生 timeline + 字幕 | FCPXML + SRT | 否 |
| 8 | 調色、修音、匯出、校驗 (finish + QA) | 成片 + Media Offline/響度報告 | 覆核成片 |
| → | 回饋迴圈 | 人類批註回寫階段 1/3 | **輸入** |

現況 `rebuild_all_projects.py` 已涵蓋 1、2、3、4、7 的骨架；缺 0、6、8 與階段 5 的審閱包產生器。

---

## 階段 0 — 素材匯入與整理

| 工具 | 用途 | 調用方式 |
|---|---|---|
| **PySceneDetect** (`scenedetect`, v0.7.1) | 長影片切分鏡頭/偵測 cut、抽 key frame；`detect-content` 找快切、`detect-adaptive` 抗晃動誤切 | `pip install scenedetect[opencv]`；CLI `scenedetect -i in.mp4 detect-adaptive list-scenes save-images` 或 Python `scenedetect.detect()` |
| **ffmpeg / ffprobe** | proxy 轉檔、抽幀、探測真實尺寸時長（已用於 `media_probe.py`） | subprocess |
| **pymediainfo / exiftool** | 影片拍攝時間、機型、GPS（已用於 `folder_metadata.py`） | 已接 |
| **ImageHash / `imagededup`** | 近似重複照片去重（環島 318 檔常有連拍） | `pip install imagededup`；`PHash().find_duplicates()` |

新增：`scripts/detect_shots.py` 包 PySceneDetect，對每個 >8s 的影片切成候選鏡頭，寫進 storyboard 前置。

---

## 階段 1 — 集成人類備註：編劇與篇章設計

- **LLM 編劇**：現況 `script_engine._generate_with_gemini`。保留。可加 Claude 作 fallback（`claude-api` skill 查 model id）。
- **結構化人類備註**：把 素材夾內的心得 `.md` / 照片說明欄當輸入。建議用 front-matter 或固定標題讓 LLM 穩定解析（章節名、情緒、必留鏡頭、禁用鏡頭）。
- **參考片型**：無需工具，prompt 內給「方案1/2/父子」三種定位表。

---

## 階段 2 — 處理素材：ML 標記、晃動、裁切

| 工具 | 用途 | 調用方式 |
|---|---|---|
| **ffmpeg `vidstabdetect`** (vid.stab, georgmartius) | 兩段式晃動偵測：第一段產 `.trf`（逐幀位移/旋轉量）→ 直接拿來算晃動比例、挑穩定區間，比現在的相位相關更準且免自己寫 | `ffmpeg -i clip.mp4 -vf vidstabdetect=shakiness=10:accuracy=15:result=t.trf -f null -`，parse `.trf` |
| **ffmpeg `vidstabtransform` / `deshake`** | 真的要穩定畫面（非只剪掉）時第二段套用 | `-vf vidstabtransform=smoothing=30` |
| **CLIP / OpenCLIP** (open_clip_torch) | 本地零樣本場景標籤、文字搜圖（"找所有海邊鏡頭"）；取代 Gemini 場景標籤的離線方案 | `pip install open_clip_torch`；算 image embedding vs prompt list |
| **Florence-2 / moondream2 / Ollama llava** | 本地影像 caption / 物件框，離線補 `labels`+`has_people` | Ollama `ollama run moondream` 或 transformers |
| **YOLOv8** (ultralytics) | 人/物件偵測框 → 給 smart crop 與 A/B-roll 分類更硬的依據 | `pip install ultralytics`；`YOLO('yolov8n.pt')(img)` |
| **MediaPipe / Google AutoFlip** | 主體感知的自動 reframe（16:9→9:16），避免裁掉重點；比現在的 saliency-only smart_crop 強 | AutoFlip 需 build MediaPipe graph；輕量替代 **Autocrop-vertical**（YOLOv8+ffmpeg CLI） |
| **RETURN 現有** `smart_crop.py` / `vision_analyzer.py` | 保留作為 fallback | — |

建議：晃動偵測換成 `vidstabdetect` 的 `.trf` 解析（`motion_stability.py` v2）；場景標籤保留 Gemini，加 `--offline-labels` 走 OpenCLIP/moondream 當額度耗盡時的備援。

---

## 階段 3 — 精華篩選與排序

- 現況：`highlight_engine.py`（caption + favorite + Gemini score）+ `--select {narrative|highlight|montage}` + `--chrono`。
- 補：**排序後的口白重寫**（已知缺口）→ `script_engine.regenerate_voiceover()`（`--regen-vo`，等 Gemini 額度）。
- 工具面沒有更好的現成品；這是本專案 domain 邏輯。可用 **librosa** 對有音訊的影片抽 onset/能量當「精彩片段」訊號。

---

## 階段 4 — 集成 metadata、產生額外素材

| 工具 | 用途 |
|---|---|
| **現有** `make_map_clips.py`（Esri World Imagery + PIL 路線/海拔） | GPS → 地圖 establishing 片段。**建議用法 `--per-day`** |
| **staticmap / prettymapp / MapLibre 無頭** | 更好看的地圖底圖（見 `map-clips.md`） |
| **Google Places (New) / Aerial View** | 地名 caption / 3D 環繞（Aerial View 不涵蓋台灣，已知） |
| **matplotlib / PIL** | 海拔剖面、Day 分隔字卡、片頭片尾字卡 |
| **gpxpy** | 若有 GPX 軌跡檔 → 更精準路線動畫 |

---

## 階段 4.5 — 片頭 / 片尾（**新，`scripts/build_bookends.py`**）

片頭片尾變成**獨立段落**（`segment` 欄位），不走 highlight_engine 常規流程。
自動挑素材用**專案無關準則**（地理/場景分散、每日 top、favorite、`keywords` tag `#片頭`/`#返家`/`#花絮`），
所以換專案也能自動找到對應素材。片名卡/片尾卡用 PIL 疊在 `map_clips/route_overview.jpg` 上。
`--write` 寫回正本 `.json`（動鏡頭 → 算正式重建，先備份）。細節與 VO 規則見 `references/bookends.md`。

| 工具 | 用途 |
|---|---|
| **現有** `make_map_clips.py` | 先產 `route_overview.jpg` 當卡片底 |
| **`route_burn.py`**（新） | 燃燒火線片頭動畫 → `route_burn.mp4`（PIL 逐幀 + ffmpeg，跟拍火頭→拉遠→片名） |
| **PIL** | 片尾卡 / 靜態片名卡 fallback（自動縮字級、CJK 字型 fallback、里程 haversine 自動算） |
| `highlight_engine.score_storyboard` | 借用來給候選鏡頭算分 |

---

## 階段 5 — 給人類過一版：腳本 + 關鍵影像（**新，需自建產生器**）

- 產出一份審閱包，讓人類在剪 timeline 前就能改：
  - 分鏡表（章節 / 鏡頭號 / 縮圖 / 景別 / 秒數 / 口白 / 素材檔名）
  - 每章「關鍵影像」大圖（highlight_score 最高的 3–5 張）
  - 口白全文（可估總字數 vs 片長）
- 工具：
  - **`make-pdf` skill**（本機已裝）— markdown → 出版級 PDF。
  - 或 **Artifact / `design` skill** 出可點的網頁分鏡板。
  - 縮圖：ffmpeg 抽每鏡代表幀。
- 人類批註格式固定（鏡頭號 + 動作：刪/換序/改口白/換素材）→ 回饋迴圈直接 parse。
- 新增：`scripts/build_review_packet.py`。

---

## 階段 6 — 配音、配樂、音效（**新**）

| 工具 | 用途 | 備註 |
|---|---|---|
| **Piper TTS** / **Coqui XTTS-v2** / **F5-TTS** | 本地中文旁白配音（男聲/自己的聲音克隆） | Piper 最輕、XTTS 音質好可克隆 |
| ElevenLabs / Gemini TTS API | 雲端高品質中文旁白 | 需 key |
| **WhisperX**（faster-whisper + wav2vec2 對齊） | 把配好的旁白音檔**逐字對齊** → 產精準字幕時間碼，取代現在「用秒數累加估字幕」 | `pip install whisperx`；輸出 srt/json 帶 word timestamps（±50ms） |
| **ffmpeg `sidechaincompress`** | 旁白一出現自動壓低音樂（ducking） | 濾鏡鏈 |
| **pyloudnorm** | 音樂 bed / 成片響度正規化到 -14 LUFS（YouTube） | `pip install pyloudnorm` |
| 免費音樂庫 | YouTube Audio Library / Pixabay / Uppbeat（需人挑曲風、注意授權） | 階段 5 讓人類選 |

流程：階段 3 定稿口白 → TTS 生音檔 → WhisperX 對齊得真時間碼 → 回寫 storyboard 的每鏡秒數（讓畫面配合旁白，而非反過來）→ 才進階段 7。

---

## 階段 7 — 產生 timeline + 字幕

| 工具 | 用途 | 決策 |
|---|---|---|
| **現有** `fcpxml_exporter.py` + `timeline_layout.py` | FCPXML 1.9（已修 Media Offline / title / 關鍵影格三大地雷） | **保留為主力**（對 Resolve 的地雷已全部繞過，換掉風險高） |
| **OpenTimelineIO** + `otio-fcpx-xml-lite-adapter` | 若要同時輸出 Premiere XML / OTIO / EDL；OTIO 當中介格式 | 可加 `--format otio`，但 lite adapter 對 transform/keyframe 支援弱，主線仍走自寫 exporter |
| **DaVinci Resolve scripting API** | 直接在 Resolve 內建時間軸（`scripts/resolve_build_timeline.py` + 選單腳本已備，免費版可用） | 徹底解法，等使用者實測 |
| **字幕** `subtitle_engine.py` + `normalize_srt_cues` | 依閱讀速度切、每則 ≥2s、走 SRT 軌 | 保留；有真旁白音檔時改吃 WhisperX word timestamps |
| **ffsubsync** | 字幕與音軌自動對齊校正（保險） | `pip install ffsubsync` |

---

## 階段 8 — 調色、修音、匯出、校驗（**部分新**）

| 工具 | 用途 |
|---|---|
| **現有** `resolve_auto_grade.py`（ffmpeg 抽幀 → ASC-CDL 灰世界 + `--hints`） | 自動一級調色，Resolve 內跑 |
| **ffmpeg `loudnorm` / pyloudnorm** | EBU R128 響度正規化 |
| **成片校驗**：`media_probe` + 開啟 FCPXML 檢查所有 `<format>` 有 `frameDuration`、所有連結檔存在 | 產「Media Offline 風險報告」，匯給人類前先自檢（已知最大地雷） |
| **ffmpeg** 或 Resolve deliver | 最終 render（H.264/H.265, YouTube 建議 params） |

新增：`scripts/qa_timeline.py`（靜態檢查 FCPXML + 素材連結 + 響度）。

---

## 回饋迴圈

人類在階段 5（腳本審閱）和階段 8（成片）的批註 → 固定格式 → parse 成 storyboard patch（刪鏡/改序/改口白/換素材/改時長）→ 回到階段 3 重跑，正本 `.json` 只在這裡被有意識地改寫（其他衍生剪法一律 `<prefix>_<variant>.fcpxml`）。

---

## 安裝狀態（2026-09-02，已裝完）

專案 venv 是 **Python 3.14**。清單見 `requirements-tools.txt`。

**已裝進 .venv（`.venv/bin/pip`）**：`scenedetect[opencv]` 0.7.1、`opencv-python` 5.0、`librosa` 1.0、
`numba` 0.67、`scikit-learn` 1.9、`scipy` 1.18、`matplotlib` 3.11、`pyloudnorm` 0.2、`ffsubsync` 0.5.1、
`opentimelineio` 0.18.1、`gpxpy` 1.6、`staticmap` 0.5.7、`torch` 2.13、`torchvision` 0.28、
`open_clip_torch` 3.3、`timm` 1.0、`ultralytics` 8.4、`imagededup` 0.3.3。全部 import 通過、8 個既有 test 仍 OK。

**vid.stab ffmpeg**：主 `ffmpeg`（`/opt/homebrew/bin/ffmpeg` 9.0.1）**沒有** vidstab。
裝了 keg-only 的 **`ffmpeg-full`** → vidstab 版在
**`/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`**（腳本要用晃動偵測就指這支；其餘照舊用主 ffmpeg）。
實測 `vidstabdetect=result=x.trf` 產出正常。
⚠️ 裝 `ffmpeg-full` 會升級 `x265` 依賴、打斷舊主 ffmpeg → 已 `brew reinstall ffmpeg` 修好（升到 9.0.1）。

**WhisperX 裝不了**：pin `ctranslate2==4.4.0` 無 cp314 wheel，且 whisperx 本身 `Requires-Python <3.14`。
替代方案（階段 6 才需要，目前無旁白音檔）：
- **whisper.cpp** 已隨 `ffmpeg-full` 裝好 → `/opt/homebrew/bin/whisper-cli`（native，無 Python 版本問題）；
- 或另開 `.venv-stt`（Python 3.12）只給 whisperx 做逐字對齊。
先記著，等真的錄了配音再處理。

**還沒裝 / 需額外 build**：Google AutoFlip（MediaPipe，需 bazel build）— 先用 `ultralytics` YOLOv8 + PIL 自己算 reframe；
moondream/Florence-2 走 Ollama 或 transformers（torch 已在，要用再 `pip install transformers`）。

---

## 已寫好的接合腳本（2026-09-02）

| 腳本 | 階段 | 做什麼 | 用法 |
|---|---|---|---|
| `scripts/detect_shots.py` | 0 | PySceneDetect 把 >8s 影片切成候選鏡頭 → `scripts/shot_candidates.json`（key=檔名stem）。adaptive 給 ≤1 段的長片退回 content。`--thumbs` 每段抽中點縮圖。實跑：115 片中 16 片 >8s、9 片切多段、46 候選鏡頭、~8 分鐘。 | `.venv/bin/python scripts/detect_shots.py [--thumbs] [--merge] [-i a.mov …]` |
| `scripts/local_scene_labels.py` | 2 | `gemini_scene_labels.py` 的離線替代：OpenCLIP 零樣本場景標籤 + mood + time_of_day + has_people；`--backend yolo` 用 YOLOv8 物件框（has_people 較準）；`both` 取聯集。寫同一份 `photos_meta.json`。 | `.venv/bin/python scripts/local_scene_labels.py [--backend openclip\|yolo\|both] [--force] [--limit N]` |
| `scripts/qa_timeline.py` | 8 | 匯出後靜態校驗：`<format>` frameDuration、`<media-rep>` 檔存在、靜態圖 colorSpace/uid/偶數尺寸、asset 同 name、非預期 `<title>`、SRT 超出時間軸/重疊/過短。exit 1 = 有硬錯。（實跑抓到「SRT 全部超出時間軸 10–20s」→ 已修，見 `subtitles-zh.md`） | `.venv/bin/python scripts/qa_timeline.py [prefix] [--allow-titles] [--audio 成片.wav]` |
| `scripts/build_review_packet.py` | 5 | storyboard JSON → `<prefix>_審閱包.html`（自帶 base64 縮圖）：企劃摘要 + 口白預算檢查 + 依章節分組 + 每章 highlight_score 前 N 張關鍵影像 + 全片分鏡表 + 批註格式提示。瀏覽器列印成 PDF 給人看。 | `.venv/bin/python scripts/build_review_packet.py <prefix> [--key-images N]` |
| `scripts/apply_notes.py` | 5→1 | 把人看完審閱包寫的批註 `notes.txt` patch 回 `<prefix>.json`。一行一條「`<鏡頭號> <動作>`」：`刪`/`換序→N`/`改口白：…`/`留白`/`換素材：檔名`/`加長到 4s`/`縮到 2s`（英文別名 del/move→N/vo:/silent/swap:/len 也吃）。`--write` 先備份。鏡頭號 = 審閱包 `#N` = storyboard `shot_index`（Live Photo A/B 共用一號 → 刪/時長套兩筆、其餘套 B）。 | `.venv/bin/python scripts/apply_notes.py notes.txt [--write]` |
| `scripts/apply_voiceover.py` | 6 | `regenerate_voiceover` 的「不含 LLM 呼叫」版。`--dump` 印分章 payload → **Gemini 額度用完時由 Claude Code 這個 session 直接寫旁白** vo.json（`{"vo":[{"i","text"}]}`）→ `--vo` 套用 + 重匯出 fcpxml/srt/md/json（正本）。實測方案1：46 顆有旁白、1995 字、83 則字幕、末 701s、qa 0 錯。 | `.venv/bin/python scripts/apply_voiceover.py <prefix> --dump` / `--vo vo.json` |
| `scripts/stabilize_clips.py` | 2 | 對「晃到不行」的影片做**畫面穩定**（不是裁剪掉晃動段）。ffmpeg vid.stab 兩段式 → 副本到 `<素材夾>/_stabilized/`，原檔不動，rebuild 自動 relink。`--auto` 掃全部影片列晃動比例排名、`--auto --do` 直接穩定 >門檻的、或直接給檔名。**規則版**：`rebuild --stabilize-clips [--shaky-clip 0.30]`（冪等，已有穩定版就跳過）。需 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`。 | `.venv/bin/python scripts/stabilize_clips.py --auto` / `X.MP4 Y.MOV` |
| `scripts/build_bookends.py` | 4.5 | 片頭片尾獨立段落：自動挑 cold_open 蒙太奇（地理/場景分散貪婪）+ recap（每日 top）+ return（tag/啟發式）+ bloopers（tag/mood）+ 生片名卡/片尾卡（PIL 疊 route_overview、里程自動算）。打 `segment` tag + `_bookend_generated`（冪等）。`--write` 寫正本 .json。dry-run 印所有自動挑的鏡頭給人覆核。實跑父子：4 cold_open + 8 recap + 返家 IMG_7247 + outro 拉 8s、783km。 | `.venv/bin/python scripts/build_bookends.py <prefix>` / `--write` |
| `scripts/segment_scenes.py` | 1 | 把 storyboard 切「小章節」寫 `scene_id`/`scene_name`。**預設一個曆日一章**（依 `taken` 日期排名，不受亂序影響）；`--by-gps`/`--by-label` 再細切；**收斂到長度區間**：`--min-scene-sec`(12) 反覆把最短的碎章併進較短的鄰居（可跨日），`--max-scene-sec`(60) 對「有換日/換地點內部訊號」的長章對切；蒙太奇自動整支一章。命名 `area_name`(行政區,穩) → place.name → label。非時序會警告。`--write [--in-place] [--rewrite-title]`。 | `.venv/bin/python scripts/segment_scenes.py <prefix> [--write --in-place]` |

**章節怎麼切**（`regenerate_voiceover` / 審閱包分組的依據）：
- **Gemini 的 `scene_title【…】`**：敘事片（父子、方案1）Gemini 分得好 → 直接用。`regenerate_voiceover` 偵測 >60% 鏡頭有【】就走這條。
- **`segment_scenes.py` 的 `scene_id`**：chrono / 蒙太奇 / 生素材，Gemini 沒好好分章時的機械骨架。訊號優先序：**曆日 > GPS 大跳點 > ML label 轉折 > 人的 caption**。`rebuild --segment`（`--regen-vo` 自動帶）會寫進去。
- 純機械切法會**過細** → merge/split 收斂到 ~12–60s/章（父子 37 鏡 → 6 章 ~25s、方案1 非時序會警告→改用 scene_title）。

**章與章之間的 hook / 串接**：
- **視覺橋**：章首放 establishing 空景 / 地圖片段。`make_map_clips.py --per-day` 已自動每天一張衛星 zoom-in + 章節名 caption，插在該章開頭。
- **聲音橋**：`regenerate_voiceover` 的 prompt 把每個 scene 的**首/尾鏡頭標 seam**（「章尾·留鉤子」「章首·接住上一章」）→ 章尾最後一句留懸念或留白，章首第一句接住/翻轉，兩句像一放一收的對話。實測父子：「望向龜山島，旅程將告一段落？」→「不，旅程繼續。」
- **呼吸**：章的接縫容許 1–2s 純環境音留白（章尾鏡頭可以完全不寫旁白，讓畫面收尾）。

## 來源

- PySceneDetect — https://www.scenedetect.com/ , https://github.com/Breakthrough/PySceneDetect
- vid.stab / ffmpeg vidstabdetect — https://github.com/georgmartius/vid.stab , https://www.paulirish.com/2021/video-stabilization-with-ffmpeg-and-vidstab/
- Google AutoFlip — https://research.google/blog/autoflip-an-open-source-framework-for-intelligent-video-reframing/
- Autocrop-vertical — https://github.com/kamilstanuch/Autocrop-vertical
- WhisperX — https://github.com/m-bain/whisperX
- OpenTimelineIO FCPX lite adapter — https://pypi.org/project/otio-fcpx-xml-lite-adapter/ , https://github.com/OpenTimelineIO/otio-fcpx-xml-adapter
- OpenCLIP — https://github.com/mlfoundations/open_clip
- 本地影像 caption：VideoHighlighter (Ollama) — https://github.com/Aseiel/VideoHighlighter
