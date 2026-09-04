# 蒙太奇 與 A/B-roll

## 蒙太奇 (Montage)

**定義**：用一連串很短的鏡頭，把一段時間 / 空間 / 情緒壓縮成一個「印象」，而不是完整演一場戲。
重點是鏡頭**並排在一起**產生的意義（Kuleshov 效應 / Eisenstein 蒙太奇理論）— 單一鏡頭本身不重要。

**旅行片實務參數**：
- 每片段 **0.8–1.5s**（快的段落可到 0.5s，但別整段都極快，觀眾會累）
- **卡在音樂節拍上**（每 1 拍或每 2 拍換一鏡）— 這是蒙太奇好看的關鍵
- 通常**單一主題**：整段都「爬坡」、整段都「美食」、整段都「夕陽」，不要混
- **幾乎不放旁白**，只留環境音 + 音樂；必要時每段開頭/結尾放一句錨點口白
- 常用在：開場 hook、Day 之間的過場、體能/時間流逝的壓縮、結尾回顧
- 節奏設計：慢起 → 漸快 → 一個重拍停頓 → 收。不要從頭到尾同一個速度。

**本專案**：`方案2_蒙太奇快剪版` = 每組素材 1.2s、純直切、只留環境音 + 22 句錨點口白。
要做更講究的蒙太奇：按主題分組排序（而非時間），依 BGM BPM 算每鏡 frame 數，
在 `generate` 階段就把 `duration_seconds` 設成節拍的整數倍。

---

## A-roll / B-roll

| | A-roll | B-roll |
|---|---|---|
| 是什麼 | 主軸畫面：人物出鏡、同步收音、旁白來源、故事骨幹 | 補充 / cutaway：風景、手部特寫、空景、細節、establishing |
| 聲音 | 保留（旁白 / 同步聲） | 通常壓掉或靜音，底下鋪環境音 + 音樂 |
| 作用 | 講故事 | 說明、藏剪接點、提升質感、給眼睛變化 |

**A/B-roll 剪法** = A-roll 的**聲音在底軌連續不斷**，上面一軌不停切 B-roll 換畫面。
觀眾聽到旁白沒斷，但畫面一直在變 → 專業感的來源。

### 在 DaVinci Resolve 的軌道配置

| 軌道 | 內容 |
|---|---|
| V2（上） | **B-roll**：風景 Live Photo、大景、細節鏡頭；剪短去蓋在旁白換氣 / 停頓 / 剪接點上 |
| V1（下） | **A-roll**：父子出鏡、同步聲重要時刻 |
| A1 | 連續的**旁白**（或 SRT 對應配音） |
| A2 | 環境音底噪 |
| A3 | 音樂 bed（B-roll 段落可調大聲一點，旁白段 duck 下來） |

### 已實作（2026-09-01）

`backend/engines/abroll_engine.py` (`classify_roles`) + `fcpxml_exporter.export(..., abroll=True)`。

```bash
.venv/bin/python scripts/rebuild_all_projects.py --only <prefix> --abroll
# 可疊 --select：--select highlight --abroll → <prefix>_highlight_abroll.fcpxml
```

**分類** `classify_roles`：靠 scene_title/visual 的關鍵字打分（人物/同步聲/動作詞 → A-roll；
風景/空景/全景/壯闊/倒影/公路/朝霞… → B-roll）；平手時動態素材當 A-roll、純靜態當 B-roll。
**旁白有無不影響分類**（旁白會蓋在 B-roll 上，這才是 A/B-roll 的重點）。
每個 shot 寫 `role`（normalize 會清掉，每次重算）。

**exporter abroll 模式**（`_build_spine_abroll`）：
- A-roll 排 spine；緊接其後的連續 B-roll（上限 8 個）被「吸收」成該 A-roll 的
  `lane="1"` 靜音 connected clip，平鋪在**延長的尾段**上。
- host A-roll 的 spine 長度 = 自身可見長度 + Σ(B-roll 長度)；前段看 A-roll，後段被 B-roll 蓋掉。
- **所有字幕（含 B-roll 原本的）搬到 host 上、依時間位移** → 畫面時間與字幕一則都不掉。
- 影片 B-roll 加 `<adjust-volume amount="-96dB">` 靜音。
- 落單 / 開頭的 B-roll 照常放 spine。

**限制**：沒有獨立旁白音檔，所以 A-roll 底下沒有「連續人聲」的骨幹，字幕仍逐 clip 掛。
真正的 A/B-roll 威力要等錄了配音後把 VO 拉成一條 A1 音軌。目前這個模式給的是
「Resolve 打開就有疊軌結構」的起點；純風景片（方案2）abroll 意義不大。

## 既有工具（可 subprocess 調用）

見 `pipeline-and-tools.md` 階段 2 / 6。

| 工具 | 安裝 | 用途 |
|---|---|---|
| **librosa** / **aubio** | `pip install librosa` | 抓 BGM 的 BPM + beat 時間點 → 蒙太奇每鏡 `duration_seconds` 設成節拍整數倍（現在是死的 1.2s） |
| **PySceneDetect** | `pip install scenedetect[opencv]` | 影片素材先找既有 cut 點，蒙太奇挑「一個動作的高潮那 1s」而非隨便截頭 |
| **ultralytics** (YOLOv8) | `pip install ultralytics` | 人 / 單車 / 物件偵測框 → `abroll_engine.classify_roles` 多一個硬訊號（有清楚人物 = A-roll），不只靠關鍵字 |
| **ffmpeg `sidechaincompress`** | 系統 ffmpeg | A/B-roll 的音樂 ducking：旁白一出現自動壓低 A3 音樂軌 |

`classify_roles` 目前純關鍵字打分；接 YOLOv8 person 偵測後可把「畫面有大的人臉/人形」直接判 A-roll。

## 剪接理論（借 smixs/visual-skills，見 `prior-art.md`）

- **Murch 剪接六法則**（一個剪接點好不好，優先序）：情緒 51% ＞ 故事 23% ＞ 節奏 10% ＞
  視線 7% ＞ 畫面平面 5% ＞ 3D 空間 4%。**寧可跳一下也要對情緒**，別為了連戲犧牲感覺。
- **每顆鏡頭三選一**：改變情緒 / 推進動作 / 增加壓力 —— 三個都不做就刪（接進 `highlight_engine` 的排序：
  有 caption / favorite / 決定性瞬間 = 通常三者之一，加權）。
- **Details Law**：一顆鏡頭要有三個物理事實 —— 環境壓力（天氣/坡度/人潮）、微動作（擦汗/換檔/回頭）、
  聲音錨點（鏈條/浪/風）。旁白別重複畫面已經給的，補畫面沒說的。
- **蒙太奇節奏曲線**：長 → 短 → 更短 → **停頓** → 撞擊。15/30/60/90 秒各一套；
  不要從頭到尾同一個速度。本專案蒙太奇每鏡 0.8–1.5s，那個「停頓」放一顆 2–3s 的喘息。

## 原則

- B-roll 蓋的位置要對齊**旁白的意義**（講到「海」就切海），不是隨便蓋。
- B-roll 切點放在旁白的**停頓 / 換氣 / 句號**，不要切在字中間。
- 一段旁白配 2–4 個 B-roll 就夠，不要每 0.5s 換一個（那是蒙太奇不是 A/B-roll）。
- A-roll 一定要留幾個「看得到人臉、聽得到現場聲」的鏡頭，不然全是空景會很冷。
