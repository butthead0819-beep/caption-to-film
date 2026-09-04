# FCPXML ↔ DaVinci Resolve 匯入：結構與地雷

Exporter：[backend/exporters/fcpxml_exporter.py](../../../backend/exporters/fcpxml_exporter.py)（FCPXML 1.9）
下游主要是 **DaVinci Resolve**（使用者在 Resolve 調色，`scripts/resolve_auto_grade.py`）。

## 時基

- FPS 30，frame unit `100/3000s`（= 1/30s）。所有時間點對齊影格（`_frames` / `_dur_str`）。
- 序列 format `r0`：`1920x1080` (或 9:16 `1080x1920` 等)、`frameDuration="100/3000s"`、`colorSpace="1-1-1 (Rec. 709)"`。

## 每個素材

- 影片 → `<asset-clip>`；靜態圖片 → `<video>`。（FCP 規範：圖片必須用 `<video>`；Resolve 兩者都吃。）
- 每個 asset 一個 `<format>`，寬高用 `pymediainfo` / Pillow 真實探測（`backend/util/media_probe.py`）。
- `<media-rep kind="original-media" src="file://...">`，只連結**真的存在**的檔（`resolve_existing_path`）；找不到就保留原路徑 + 在 clip 加 marker「素材需重新連結」，**絕不竄改副檔名**。

## 地雷 0：Resolve 把「奇數尺寸」圖片標 Media Offline ← 下載照片的真凶

**寬或高不是 2 的倍數的圖片，Resolve 匯入直接 Media Offline。** 截圖 / 全景裁切 / 網路下載
的 JPG 常是奇數尺寸（IMG_2445 = 1477×1108、IMG_2608 = 2599×629…）。iPhone 原生照片一律偶數。
修法：`backend/util/media_prep.py` `prepare_still()` 把奇數邊裁 1px 變偶數 + 轉 sRGB baseline JPEG
存到 `<素材夾>/_prepared/`，rebuild 自動連過去（會印「奇數尺寸修正 N」）。

## 地雷 0.5：MOV 與 JPG 同名 → Resolve 去重 → 定格那半 offline

Live Photo 拆成 MOV(微動態) + JPG(定格) 兩鏡，若兩個 `<asset>` 都 `name="IMG_2440"`，
Resolve 按 name 去重、把 JPG 當成 MOV 同一媒體 → JPG clip offline。
→ exporter asset `name` 帶副檔名（`path.name`）。另外靜態圖 asset `duration` 用 `"0s"`（timeless），
給有限大值 Resolve 會當超長影片找不到影格。

## 地雷 1：Resolve 把靜態圖片全標 Media Offline

**原因**：Resolve 對「`<format>` 缺 `frameDuration`」的素材一律標 Media Offline（即使檔案存在、路徑正確）。
影片 format 有 `frameDuration` 所以正常；舊版圖片 format 沒有 → 每張照片鏡頭 offline。
Live Photo 鏡頭因為配對的 MOV A 段還在播，比較不易察覺；純照片鏡頭整個空 → 會被誤判成「下載的照片壞了」。

**修法**（已套用，圖片分支）：
```python
<format ... frameDuration="100/3000s" width=.. height=.. colorSpace="1-13-1 (sRGB IEC61966-2.1)" />
<asset ... uid="<md5(resolved_path)>" start="0s" duration="10800000/3000s"
       format=.. hasVideo="1" videoSources="1" hasAudio="0" audioSources="0">
```
`uid` 讓 Resolve 能穩定 key media；`hasAudio="0"` 避免它找不到音軌。

驗證：`for f in *.fcpxml; do echo "$f $(grep -c '<format' $f) $(grep -c frameDuration $f)"; done` — 兩個數字要相等。

## 地雷 2：Resolve 忽略 `<title>` 的 Position → 字幕置中壓畫面

中文字幕改走 SRT 字幕軌。見 `subtitles-zh.md`。

## 地雷 2.5：Ken Burns 在 Resolve 出不來 / 把素材推出畫面

兩層：
- Resolve 只吃靜態 `<adjust-transform scale=".." position="..">`，**不讀 `<param><keyframe>`**。
- **`<adjust-transform position>` 在 Resolve 被當「幾個畫面寬」不是像素** → `position="-30 -75"` = 往左推 30 個畫面 → 素材飛出 canvas。

修法（`fcpxml_exporter`）：`KEYFRAMES=False`（預設）→ 運鏡**只寫 `scale`（置中推近）、完全不寫 `position`**，
scale 夾 1.0–2.2x。精準對焦平移 FCPXML 給不了 Resolve，要就用 API 版或檢視器手拉 Dynamic Zoom。
`--fcp-keyframes` 才寫關鍵影格 + position（給 Final Cut）。

## 地雷 2.6：晃動沒剪掉 ≠ bug

`--stabilize` **不是預設**。要 `rebuild_all_projects.py --stabilize`（跑前備份 JSON，會大幅裁短）。
FCPXML 有 source_in/out 也只對影片 `<asset-clip>` 有效，`<video>` 靜態不吃。

## 徹底解法：`scripts/resolve_build_timeline.py`（繞過 FCPXML）

用 Resolve 腳本 API 直接建時間軸（同 `resolve_auto_grade.py` 的連線方式）：
- Resolve 自己的匯入器拉素材 → 哪個檔真的壞會列出來（不再猜 Media Offline）
- 逐 clip append，含 `source_in/source_out` 裁切 → 晃動剪除生效
- 每 clip 套 smart-crop 焦點的 `Pan/Tilt/ZoomX/ZoomY` 靜態推鏡框 + 開 Dynamic Zoom
- 限制：靜態框（動態 Ken Burns 起訖要在檢視器 Dynamic Zoom 手調）；字幕仍走 File▸Import▸Subtitle
**跑法 A（免費版也行，推薦）**：已裝好選單腳本
`~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/環島_建時間軸.py`
→ Resolve 內 **Workspace ▸ Scripts ▸ 環島_建時間軸**。改專案改該檔的 `PREFIX`。
選單腳本跑在 Resolve 自己的 Python，**不需要 External scripting 開關**。

**跑法 B（Studio 版、外部終端）**：Preferences ▸ **System**(上方分頁，不是 User) ▸ General ▸
最底 External scripting using ▸ Local ▸ 重開 Resolve，然後
```
export PYTHONPATH="/Library/.../DaVinci Resolve/Developer/Scripting/Modules:$PYTHONPATH"
python3 scripts/resolve_build_timeline.py <prefix>
```
`get_resolve()` 兩種都支援（先找注入的 `resolve`/`bmd`，再找外部模組）。

## 地雷 3：路徑百分比編碼

`src` 是 `path.as_uri()`，CJK 目錄會變 `%E5%96%AE...`。Resolve 17+ 能解，若遇到 offline 又確認檔在，
先試把素材夾改成純 ASCII 路徑再重匯。

## 地雷 4：重複 stabilize 把鏡頭切碎

`motion_stability.apply_to_storyboard` 對 `trim` 鏡頭產生多個 `source_in/source_out` 子鏡頭。
已含 `source_in` 的鏡頭會跳過（`lru_cache` + 檢查），`normalize_storyboard` 會還原基準。
**跑 `--stabilize` 前先備份該專案 JSON**。父子專案曾 22 → 225 鏡。

## Ken Burns / Transform

`effects_engine` 算好 `shot["ken_burns"] = {type, start/end:{scale,x,y}}`；x/y 是畫面比例。
exporter `_write_transform` 乘序列寬高成像素、寫 `<adjust-transform>` 關鍵影格（`time="0s"` → `time=<clip 長>`）。
靜態則寫單一 `scale` + `position`。

## 調色

FCPXML 的 `<adjust-color>` **Resolve 匯入會忽略** → 走 `scripts/resolve_auto_grade.py`（在 Resolve 內跑，
ffmpeg 抽幀分析 → ASC-CDL 灰世界白平衡 + 黑白點正規化 + 微飽和 → `TimelineItem.SetCDL()` 套第 1 節點）。

## 影片 clip 長度

影片 clip 一律夾到素材真實長度（`min(clip_sec, duration_s - src_in)`），避免時間軸超出素材產生定格 / 黑畫面。

## 既有工具（可 subprocess 調用）

見 `pipeline-and-tools.md` 階段 7 / 8。**主力仍是自寫 `fcpxml_exporter`**（對 Resolve 的地雷已全部繞過，換掉風險高）：

| 工具 | 安裝 | 用途 | 要不要用 |
|---|---|---|---|
| **OpenTimelineIO** + `otio-fcpx-xml-lite-adapter` | `pip install opentimelineio otio-fcpx-xml-lite-adapter` | 需要同時輸出 Premiere XML / OTIO / EDL 時，把時間軸建成 OTIO 當中介再轉出 | 加 `--format otio` 選項可以，但 lite adapter 對 transform/keyframe 支援弱 → 主線不走 |
| **DaVinci Resolve scripting API** | 系統 Resolve | 直接在 Resolve 內建時間軸（`scripts/resolve_build_timeline.py` + 選單腳本已備） | 徹底解法，等實測 |
| **`scripts/qa_timeline.py`**（✅ 已寫） | 已在 .venv | 匯出後靜態檢查：`<format>` frameDuration、`<media-rep src>` 檔存在、靜態圖 colorSpace/uid/偶數尺寸、asset 同 name、非預期 `<title>`、SRT 超出時間軸/重疊/過短。exit 1=硬錯 | `.venv/bin/python scripts/qa_timeline.py [prefix]` |
| **ffsubsync / pyloudnorm** | `pip install ffsubsync pyloudnorm` | 字幕對齊校正 / 成片響度正規化 -14 LUFS | 階段 8 |

## 原則

- 改 exporter 後一定跑 `rebuild_all_projects.py` 重生三個專案 + `xml.dom.minidom.parse` 驗證格式。
- 任何「Resolve 匯入後怪怪的」先問：format 完整嗎？路徑存在嗎？title 還是 SRT？
- 新地雷寫回這裡 + `SKILL.md` 的「已知地雷」。
