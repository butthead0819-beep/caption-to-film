---
name: film-edit
description: 這個專案的剪輯知識庫與工作流 — 決定 highlight、蒙太奇、A/B-roll、中文字幕規格、FCPXML↔DaVinci Resolve 匯入的所有已知地雷與慣例。任何要動 storyboard / 分鏡 / 節奏 / 字幕 / FCPXML / 匯出的工作都先讀這個。
triggers:
  - 剪輯
  - 分鏡
  - storyboard
  - highlight
  - 精華片段
  - 蒙太奇
  - montage
  - a-roll
  - b-roll
  - a/b roll
  - 字幕
  - subtitle
  - fcpxml
  - davinci
  - resolve
  - media offline
  - 節奏
  - 轉場
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - AskUserQuestion
---

# film-edit — 剪輯工作流

這個 skill = **一支 agent 可以照著把「一資料夾影片 + 我的手寫備註」剪成成片的可重複流程**，
加上「我的風格」的所有參數與已知地雷。不是每次都要來回調 20 次——那些調校已經收斂進
`風格設定檔` 和 `backend/`；新影片照下面的流程跑，人只在階段 5（過稿）和階段 8（成片）看兩次。

> 沒有公開/官方的剪輯 skill。這是本專案自建的。

---

## 換一支新影片

所有腳本從 `scripts/_config.py` 讀「素材夾」與「專案 prefix」。新影片二選一：
- repo 根放 `edit_project.json`：`{ "media_dir": "/path/to/素材夾", "prefix": "2026_某旅行" }`
- 或設環境變數 `EDIT_MEDIA_DIR` / `EDIT_PREFIX`

沒設就是預設的環島專案。`prefix` 就是所有輸出檔（`.fcpxml`/`_字幕.srt`/`.json`/`_分鏡腳本.md`）的檔名前綴。

---

## 新影片：完整流程

`.venv/bin/python` 前綴省略。⬛ = 需要人；其餘 agent 自動。

| # | 階段 | 指令 | 產出 |
|---|---|---|---|
| ⬛ | **寫備註** | 在 iOS 照片說明欄寫 caption + 一篇「心得長文」（為什麼出發、心境、哪些一定要留） | 素材夾裡的文字 |
| 0 | 素材 metadata | `scripts/probe_folder_metadata.py --geocode` | `scripts/photos_meta.json`（GPS/時間/人臉/地名） |
| 0 | 場景標籤 | `scripts/gemini_scene_labels.py`（額度用完 → `scripts/local_scene_labels.py`） | photos_meta 補 `labels`/`mood`/`has_people` |
| 0 | 長片切候選鏡頭 | `scripts/detect_shots.py --thumbs` | `scripts/shot_candidates.json` |
| 1 | 編劇（分鏡+初版口白） | `python cli.py generate -i <素材夾> --prompt "…"`（Gemini） | `<prefix>.json` storyboard |
| 1 | 切小章節 | `scripts/segment_scenes.py <prefix> --write --in-place` | 每鏡 `scene_id`/`scene_name`（曆日>GPS>label） |
| 1 | 章節內按時間排（修局部亂序）| `scripts/sort_shots.py --write` | 保留章節順序，章內鏡頭照 `taken` stable-sort，沒時間的跟前一顆 |
| 2 | 畫面穩定（晃到不行的） | `scripts/stabilize_clips.py --auto --do`（門檻 shaky≥0.30） | `<素材夾>/_stabilized/` |
| 2–7 | **主重建**（relink→穩定→Live Photo 拆→晃動剪→運鏡→字幕→匯出） | `scripts/rebuild_all_projects.py --stabilize --stabilize-clips` | `<prefix>.fcpxml` / `_字幕.srt` / `_分鏡腳本.md` |
| 4 | 地圖過場片段 | `scripts/make_map_clips.py --storyboard <prefix>.json --per-day` | `map_clips/` + manifest（每章開頭插一張、`route_overview.jpg`） |
| 4.5 | **片頭 / 片尾**（獨立段落、另外挑素材、生片名卡/片尾卡） | `scripts/build_bookends.py <prefix>`（dry-run）→ `--write` | 正本 `.json` 加 `segment` tag + `bookends/*.jpg` + `bookend_config` |
| 5 | **過稿包** | `scripts/build_review_packet.py <prefix>` | `<prefix>_審閱包.html` |
| ⬛ | **看過稿包**、批註 | 寫一個純文字檔 `notes.txt`，一行一條：`<鏡頭號> <動作>`。動作：`刪` / `換序→N` / `改口白：…` / `留白` / `換素材：檔名` / `加長到 4s` / `縮到 2s` | `notes.txt` |
| 5→1 | 回饋 patch | `scripts/apply_notes.py notes.txt --write`（先備份 `.json`） | 更新的 `<prefix>.json` |
| 6 | 重寫口白（配合剪好的時間軸）| `scripts/rebuild_all_projects.py --regen-vo`（Gemini 額度用完 → `apply_voiceover.py --dump` 讓 agent 直接寫 vo.json 再 `--vo`） | 章節級旁白 + 章接縫 hook |
| 8 | 匯出校驗 | `scripts/qa_timeline.py`（FCPXML/SRT 靜態）+ `scripts/qa_render.py <mp4>`（成片 self-eval：接縫幀 / 響度 / 削波 / 凍結） | 報告，exit 必須 0 |
| 8 | **出畫面鎖定版** | `scripts/render_video.py --for-imovie --grade` | `<prefix>.mp4`：切點 + Ken Burns + CDL 調色 + **燒中文字幕（當口白稿）** + 現場音壓到 -10dB 當環境底。H.264 high + faststart，iMovie 直接吃 |
| ⬛ | **iMovie 收尾**（使用者選這條，不用 Resolve）| 把 mp4 拖進 iMovie → 用「旁白」工具照著畫面上的字幕唸 → 加音樂（iMovie 音樂庫或檔案，自動 ducking）→ 匯出 | 成片 |
| ⬛ | 或走 Resolve（要精細調色 / SRT 字幕軌）| File▸Import▸Subtitle 選 `_字幕.srt`；`scripts/resolve_auto_grade.py` | 成片 |

`normalize_storyboard` 讓每次 rebuild 冪等（先把上次加工的衍生欄位還原成乾淨基準）。
`--select`/`--abroll`/`--chrono` 是衍生剪法 → 輸出到 `<prefix>_<variant>.fcpxml`、**不碰正本 `.json`**。
`--regen-vo` 單獨用會寫回正本（只改旁白不動鏡頭）。

---

## 風格設定檔（我的風格 = 這些數值 + 原則）

| 面向 | 設定 | 在哪 |
|---|---|---|
| **節奏** | 全景≥3s、中景 2–3s、特寫 1.5–2s、蒙太奇節拍 0.8–1.5s | 原則 1 |
| **旁白密度** | 章長 × 6 字/秒 × **0.85**（coverage）；留白過半的鏡頭沒有旁白 | `apply_voiceover.COVERAGE` / `regenerate_voiceover(coverage=)` |
| **旁白寫法** | 用「我的口氣」把**每個章節的故事講清楚**（不是串場、不是俳句）。一章的句子接成一條連續敘述 → 裁到章長 → 依鏡頭長度比例分回各鏡頭 | `apply_voiceover` / `script_engine._trim_to_chars` + `_distribute_text` |
| **章與章接縫** | 章尾留鉤子（提問/預告/留白），章首接住或翻轉；一放一收。視覺橋 = 地圖過場片段 | 原則 2b |
| **章節切法** | 敘事片用 Gemini `scene_title【…】`；否則 `segment_scenes` 的曆日>GPS>label，收斂到 12–60s/章 | 原則 1b |
| **字幕排版** | 白字+黑描邊(3–4)+柔陰影；每行 ≤16 全形字、每則 ≤2 行、每則 ≥2s 絕不壓、下三分之一 | `subtitle_engine` 常數 / `references/subtitles-zh.md` |
| **晃動** | 大幅晃動段 → **剪掉**（`--stabilize`，shake_threshold 3.2）；整支晃到不行 → **穩定畫面**（`--stabilize-clips`，shaky_fraction ≥0.30，vid.stab） | `motion_stability` / `stabilize_clips` |
| **Live Photo** | 每個拆 A(微動態去頭尾~1.4s 無旁白) + B(配對靜態定格，帶字幕) | `livephoto_engine` |
| **運鏡** | 照片用 SmartCrop 焦點推近、全景左右平移、比例落差大用 fit 不硬填；有人臉框就對焦人臉 | `effects_engine` |
| **調色** | 灰世界白平衡 + 黑白點正規化 + 微飽和，疊 Apple 場景標籤 preset（sunset 暖、ocean 冷…）；在 Resolve 內跑 | `resolve_auto_grade.py --hints` |
| **匯出目標** | **主線 = iMovie**：`render_video.py --for-imovie --grade` 出畫面鎖定版（切點/Ken Burns/調色/燒字幕當口白稿/現場音-10dB）→ 拖進 iMovie 錄旁白 + 加音樂。備線 = FCPXML → DaVinci Resolve（要精細調色時；字幕走 SRT 軌不寫 `<title>`、每個 `<format>` 要 `frameDuration`） | `render_video.py --for-imovie` / `fcpxml_exporter` |

---

## 要讀哪個 reference

- 精華篩選 / 進片判斷 → `references/highlight-scoring.md`
- 鏡頭長度、轉場、蒙太奇、A/B-roll 疊軌 → `references/montage-and-abroll.md`
- 中文字幕切分/樣式/Resolve SRT 流程 / 旁白密度與接縫 / 旁白主述去重複 → `references/subtitles-zh.md`
- 片頭 / 片尾獨立段落 / `segment` 欄位 / 自動挑素材準則 / 片名卡片尾卡 → `references/bookends.md`
- FCPXML 結構 / Media Offline / Resolve 匯入行為 → `references/fcpxml-resolve.md`
- 照片可抽欄位 + 應用（faces→對焦、labels→調色/主題、GPS→地圖）→ `references/photo-metadata.md`
- GPS → 地圖過場片段 → `references/map-clips.md`
- 9 階段完整分工 + 每階段的既有開源工具 + 已寫好的接合腳本清單 → `references/pipeline-and-tools.md`
- 別人開源的剪輯 skill / pipeline 掃過一輪 + 該借什麼（Murch 六法則、成片 self-eval…）→ `references/prior-art.md`

引擎速查：`highlight_engine`（精華評分）、`abroll_engine`（A/B 分類）、`grading_engine`（調色 sidecar）、
`util/photos_meta.py`（載入器 + `face_center()`）、`util/folder_metadata.py`（不開 Photos DB 讀內嵌 metadata）。

---

## 核心剪輯原則（速查）

1. **節奏 = 資訊密度**。觀眾看懂一個鏡頭要時間：全景/空景 ≥3s，中景 2–3s，特寫 1.5–2s，蒙太奇節拍 0.8–1.5s。旁白鏡頭至少要念得完那句話。
1b. **畫面是主角，旁白填空隙**。旁白依「章節（scene）」的**總時間**編寫，不是每顆鏡頭各寫一句再塞進時間軸——那會超時。一個 scene 有 N 顆鏡頭共 T 秒 → 旁白總字數 ≤ `T × 6字/秒 × ~0.6`（其餘留白給環境音）；純風景/蒙太奇 scene 可完全不放旁白。實作：`script_engine.regenerate_voiceover`（`rebuild --regen-vo`）逐 scene 給 Gemini 字數上限 + 回來後確定性裁切。章節來源：敘事片用 Gemini 的 `scene_title【…】`，其他用 `segment_scenes.py` 的 `scene_id`（曆日 > GPS > label）。
2. **並排產生意義**（蒙太奇）。單一鏡頭不重要，是前後兩鏡放一起的感覺。
2c. **Murch 剪接六法則**（借 smixs/visual-skills）。決定一個剪接點好不好，優先序：
   情緒 51% ＞ 故事 23% ＞ 節奏 10% ＞ 視線 7% ＞ 畫面平面 5% ＞ 3D 空間 4%。
   為了畫面平面 / 空間連戲而犧牲情緒，是本末倒置。
2d. **每顆鏡頭三選一**：改變情緒、推進動作、或增加壓力 —— 三個都沒有就刪。
   蒙太奇節奏：長 → 短 → 更短 → 停頓 → 撞擊（見 `montage-and-abroll.md`）。
2b. **章與章的接縫**。章尾留鉤子（提問 / 預告 / 情緒未完 / 或直接留白讓畫面收），章首接住或翻轉（時間或地點的跳接、establishing 空景 / 地圖片段當過場）。相鄰兩章的鉤子+接句是一放一收的對話，不要兩句都在總結。視覺橋 = `make_map_clips --per-day` 的地圖片段；聲音橋 = `regenerate_voiceover` 的 seam 標記。接縫容許 1–2s 純環境音呼吸。
3. **A-roll 扛故事，B-roll 補畫面**。聲音（旁白/訪談）在底軌連續不斷，上面一軌切 B-roll 換畫面、藏剪接點。
4. **留白**。不是每個鏡頭都要說話；環境音 + 音樂 bed 的呼吸段落讓片子不喘。
4b. **片頭片尾是獨立段落，不是正片的頭尾兩顆**。`build_bookends.py` 從獨立素材池（用專案無關的準則：地理/場景類型分散、每日 top、favorite、tag）組出風景蒙太奇冷開場 + 片名卡 + 回顧蒙太奇 + 定格句點 + 返家 + 片尾卡 + 花絮，打 `segment` tag。這些鏡頭 highlight select 一律保留、regen-vo 一律靜默（outro 除外）。見 `references/bookends.md`。
5. **先選片再排節奏再套效果**。highlight 篩選 → 排序 → ken burns / 調色 / 字幕。
6. **匯出目標決定寫法**。這個團隊的下游是 DaVinci Resolve → 字幕走 SRT 軌不走 `<title>`，每個 `<format>` 都要 `frameDuration`。

---

## 已知地雷（血淚，勿再踩）

- **Resolve：`<format>` 缺 `frameDuration` → 用它的 clip 全部 Media Offline**（靜態圖片最容易中）。exporter 圖片分支已補 `frameDuration` + `colorSpace` + asset `uid`。細節見 `references/fcpxml-resolve.md`。
- **Resolve 匯入 FCPXML `<title>` 會忽略 Position → 字幕一律置中壓在畫面中間**。中文字幕改走 SRT 字幕軌。見 `references/subtitles-zh.md`。
- **中文描邊 strokeWidth 6 會把筆劃糊在一起**，取 3–4。
- 舊 exporter 曾用已淘汰的 `<asset src=>`、亂改副檔名、缺 format → Media Offline。已改 `<media-rep>` + 真實探測。
- 重複跑 `--stabilize` 會把一個鏡頭越切越碎（父子專案曾 22→225 鏡）；`normalize_storyboard` + `source_in` 檢查已擋，跑前仍先備份 JSON。
- 方案2 JSON 有 11 個 bogus 鏡頭（舊腳本把自己的 .srt/.md 當素材）；rebuild 用檔名關鍵字過濾。
- **正本 `.json` 是唯一真相來源，被 `--select` 砍掉的鏡頭無法還原**。所以衍生剪法一律輸出到
  帶後綴的檔名、不碰 `.json`。曾因 `--select` 直接覆寫 `.json` 把方案1 從 125 打成 52，
  靠 `build_option1_full_narrative.py`（自帶手寫分鏡）+ `generate_options_1_and_2.py` 才救回來。

---

## 維護這個 skill

做完一次剪輯工作後：
- 新的剪輯決策原則 → 加到對應 reference 的「原則」段
- 新踩到的地雷 → 加到上面「已知地雷」+ reference
- 通用知識寫 reference，專案專屬數值/檔名也寫清楚（未來的人要能照做）
- 對應的 code 改動仍寫在 `backend/` / `scripts/`，skill 只記「為什麼這樣改 + 怎麼用」
