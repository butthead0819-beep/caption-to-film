# GPS → 地圖 / aerial zoom-in 片段

環島片最有識別度的畫面。GPS 在 `photos_meta.json` 的 `gps: {lat, lon, alt}`。

## 能不能做出 Google Earth 那種 aerial zoom-in？

能，分三個等級：

### 等級 1：衛星圖 + Ken Burns（**已實作**：`scripts/make_map_clips.py`）
- Esri World Imagery 靜圖 export（**免 API key**），PIL 畫上紅色路線 + 白圈 pin
- 依 GPS 距離 / 地名 / 時間間隔分「段(leg)」，每段一張；另出全路線總覽 + 海拔剖面透明疊圖
- 每張附 `ken_burns` zoom-in（scale 1.0 → 2.6，位移到 pin），`map_clips/manifest.json` 標「插在哪個 scene_title 前」
- 用法：`.venv/bin/python scripts/make_map_clips.py --storyboard <prefix>.json --per-day`
  （`--per-day` 一天一張約 8~9 張，caption = 章節【】名如「直面壽卡」；不加則依距離分，會很多張）
- 效果：從高空拉近到地點。**沒有 3D 傾斜**。目前拖進 Resolve 手動用，或之後接 rebuild `--with-maps`

### 燃燒火線片頭（**已實作**：`scripts/route_burn.py` → `map_clips/route_burn.mp4`）

片名卡的動畫版：整條環島 GPS 軌跡逐幀畫成一條燒過去的導火線。
- **火頭**：白核→黃→橙疊圓 + 高斯發光層 + 每幀重隨機的閃爍火花
- **尾巴**：燃完的餘燼（暖灰暖橙、離火頭越遠越暗、會淡，但 alpha floor 100 → 整條路仍看得見）
- **點燃**：火頭經過每個章節起點 → 擴散光環 + 常亮 pin + 「Day N · 地名」標籤淡入保留
  （Day = 實際曆日序，用 `taken` 日期算；地名優先【】章節名，否則 `_clean_place(scene_name)`）
- **鏡頭**：先跟拍火頭（zoom 1.9），最後 ~1.8s ease-out 拉遠到全島 → 整圈亮起定住 → `--title` 片名淡入
- 衛星底圖抓 2x（3840×2160）給跟拍 zoom 用；PIL 逐幀 + ffmpeg libx264。10s/30fps ≈ 70s 算圖。
- 用法：`.venv/bin/python scripts/route_burn.py --storyboard <prefix>.json --title "片名" --seconds 10`
- **`build_bookends.py` 會自動偵測**：`map_clips/route_burn.mp4` 存在 → `title` 段用它（video），
  否則退回 PIL 靜態片名卡 `bookends/title_card.jpg`。
- 章節結構差的專案（沒 scene_id / 沒【】）標籤會退成行政區名，可能較雜 → 先跑 `segment_scenes.py`。

### 地名 caption（`backend/util/poi.py`）

**map clip 的 caption 用分鏡章節名最準**（人寫的）：`make_map_clips._scene_caption()`
把「Day 4【縱谷與稻田】：大坡池」→「大坡池」。這是預設。

退階：
1. Apple `place.name`（`photos_meta` 有的話，跑過 `dump_photos_metadata.py`）
2. **`area_name(lat, lon)`** — 行政區級（"屏東縣枋山鄉"、"花蓮縣秀林鄉"）。Nominatim，免 key，穩定。
   `probe_folder_metadata.py --geocode` 用這個填 `photos_meta` 的 `place`。

另有 **`place_name(lat, lon)`**（Google Places (New) → Overpass → Nominatim）= 最近的「地標」，
**但馬路邊會回最近的店家**（"133自助洗衣"…），不適合當 map caption；留給「已知這張在某地標」的情況。

Google 設定（2026-09-01 完成）：`.env` 另開了 `GOOGLE_MAPS_API_KEY`（Maps 專用，非 Gemini 那把），
Places API (New) 已啟用+放行、可用；Aerial View API 台灣不支援。快取 `scripts/poi_cache.json`。

### 等級 2a：Google Aerial View API（已接 `--aerial`，但**台灣不支援**）
- `backend/util/aerial.py` + `make_map_clips.py --aerial [--aerial-wait 180]`
- 實測（2026-09-01，key 已放行、API 已啟用）：`renderVideo` 對「Taipei 101, Taiwan」回
  **`400 Address not supported`** → Aerial View 目前**完全不涵蓋台灣**（美國 + 少數國家）。
- 程式碼保留：Google 擴張到台灣就能用，拿不到一律優雅退回等級 1 衛星靜圖。

### 等級 2b：MapLibre / Mapbox GL 無頭渲染（真 3D，自訂相機）
- `setTerrain()` + `flyTo({pitch:70})`，Playwright 逐幀截 canvas → ffmpeg
- 專案沒 node；等 Aerial View 不夠用再考慮

### 等級 3：Google Earth Studio（畫質最好，半手動）
- 免費網頁工具，真 Google Earth 3D 影像，關鍵影格相機動畫，可匯入 KML 軌跡
- 但**沒有 API**，每個地點要手動建專案、算圖、下載
- 適合：開場一個 hero 俯衝鏡頭，手工做一次

## 建議路線

1. 先做**等級 1**：`scripts/make_map_clips.py`
   - 讀 `photos_meta.json` → GPS 分群（每個 `place.name` 或每 ~2km 一群）
   - 每群抓衛星圖，選一個當 establishing shot 插在該段開頭
   - storyboard 新增 `synthetic: "map"` 鏡頭，exporter 照常吃（給它 zoom-in ken_burns）
   - 海拔：把整趟 `alt` 畫成一條折線圖疊在角落（PIL），壽卡段自動標高點
2. route 線總覽：一張全台灣衛星圖 + 紅線軌跡，開場/結尾各放一次
3. 之後有需求再上**等級 2** 做飛行鏡頭

## 既有工具（可 subprocess 調用）

見 `pipeline-and-tools.md` 階段 4。現況 `make_map_clips.py` 自己用 Esri 靜圖 + PIL 畫線，夠用；要升級時：

| 工具 | 安裝 | 用途 |
|---|---|---|
| **gpxpy** | `pip install gpxpy` | 若有 GPS 記錄 App 匯出的 `.gpx` 軌跡 → 比照片點更密的路線動畫 |
| **staticmap** | `pip install staticmap` | 純 Python 靜態地圖 + polyline，換底圖方便（OSM / 自訂 tile） |
| **prettymapp** / **plotly** | `pip install prettymapp` | 風格化路線圖（開場總覽用） |
| **MapLibre GL + Playwright** | node + `playwright` | 等級 2b 真 3D 飛行鏡頭：`flyTo({pitch:70})` 逐幀截圖 → ffmpeg（專案沒 node，需求出現再上） |

## 其他 GPS 應用（不畫地圖也能用）

- 相鄰鏡頭 haversine 距離 → 「Day3 騎了 68km」下標；`alt` 差 → 「爬升 1,100m」
- GPS 分群 = 比 scene_title 更準的「新地點」判斷 → establishing shot 挑選（接回 highlight）
- 時間戳亂掉時用 GPS 軌跡順序排鏡頭
