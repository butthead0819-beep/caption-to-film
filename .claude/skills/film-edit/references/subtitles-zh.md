# 中文字幕規格與工作流

## 同步：SRT 必須用「成片時間軸」而非原始 duration_seconds

`backend/exporters/timeline_layout.py` 算每顆鏡頭在成片上的精確起訖（扣掉晃動剪除、
影片夾到素材真實長度、影格對齊、跳過 skip）——**FCPXML 與 SRT 共用這份**，否則字幕越飄越遠。
`normalize_srt_cues` 重疊時**縮短前一則結尾、絕不推後一則起點**（推起點會雪崩）。
每顆鏡頭字幕撐超過「鏡頭長 + 1.2s 尾巴」→ 等比壓縮塞進去。
曾因 SRTExporter 用原始 `duration_seconds` 累加 → 方案1 字幕比時間軸長 184s。

**2026-09-02：字幕仍會超出片尾 10–20s（qa_timeline.py 抓到四個專案全中）**。根因：`generate_timed_subtitles`
每顆鏡頭把整段旁白排成「每則 ≥2s + 0.1s 間隔」，但鏡頭平均只有 2.5–4s → 每顆都溢一點 →
`normalize_srt_cues` 的不重疊 floor 一路累積把尾巴推到片長外，舊 `MAX_DRIFT=8s` 又把積壓的整批丟掉
（方案1 chrono 曾丟 ~100 則、末字幕 477s vs 時間軸 457s）。
已修 `normalize_srt_cues(cues, timeline_end=…)`：
  - 有 `timeline_end` → 硬夾在 `timeline_end + OVERFLOW_TAIL_SEC(1.5s)` 內；
  - 為「後面每一則」保留 `MIN_CUE_SEC + gap`，剩下的才給這則 → 接近片尾自動**壓縮**（下限 1.2s）而非整段消失；
  - 連 1.2s 都塞不下才丟這一則（不再一次 break 掉整條尾巴）；
  - 有 `timeline_end` 時不再用 `MAX_DRIFT` 丟（ceiling+保留已是更準的防線）。
  - 丟/壓的則數記在 `normalize_srt_cues.last_dropped` / `.last_compressed`，`SRTExporter` 會印警告叫人跑 `--regen-vo`。
`SRTExporter.export` 現在從 `timeline_layout` 算 `timeline_end` 傳進去。
實測（旁白未重寫）：方案1 704s→末694s、方案2 281s→末279s、父子 152s→末154s、全部 0 重疊，8 test 仍 OK。
**但方案1 仍丟 ~37 則、父子壓 24 則 → 旁白真的太密（父子審閱包標 263%），該跑 `--regen-vo` 或人工精簡。**

**2026-09-02：`--regen-vo` 改成「章節（scene）級預算」**（`script_engine.regenerate_voiceover`）：
- 依 `scene_title` 的【章節】把 layout 切成連續 scene；每個 scene 算 `總秒數 × 6字/秒 × coverage(預設0.6)` = 字數上限。
- Gemini prompt 逐 scene 給上限、強調「畫面是主角、旁白填空隙、純風景/蒙太奇 scene 就留空」，每句掛在 scene 內某顆鏡頭 i。
- 回來後**確定性裁切**（不硬信 LLM）：逐鏡頭砍到 `len×6×1.2` 念得完、逐 scene 砍到不超總預算，`_trim_to_chars` 只砍整句。
- 印 `[regen-vo] n/N 顆有旁白｜M 章｜共 X 字（上限 Y）`。
實測父子：7 章、336 字（上限 546）、18/37 顆有旁白（一半以上留白）、SRT 18 則末 151.2s / 片長 152.6s
→ **qa_timeline 0 錯 0 警告、SRTExporter 完全不需要壓縮或丟**。這才是根治；上面的 normalize ceiling 是沒重寫旁白時的兜底。

**2026-09-02 再修「字幕濃縮太短、看不懂 / 短鏡頭的長句互相插隊」**（使用者回饋）：
- coverage 0.6 → **0.85**（太低只剩串場）。旁白要用作者口氣把那個場景的故事講清楚，不是寫成俳句。
- 每章的旁白改成「一條連續敘述」：把該章所有句子照鏡頭順序**接起來** → 裁到 `章長 × 6 × coverage` →
  用 `motion_stability._distribute_text` **依每顆鏡頭長度比例分回各鏡頭**（整句、依序）。
  → 不會再有「1.4s 鏡頭掛 40 字、溢出去跟隔壁句子插隊」，也不跨章。
- `_trim_to_chars` 補「單一超長句在標點處硬切」（以前一句沒句號就整句塞不下）。
- `apply_voiceover.py` 和 `regenerate_voiceover` 都走這套。實測方案1：46 顆有旁白、1995 字、83 則、末 701s、0 錯。
- **仍卡的：結尾章（Day8 父親節答案）只有 6.3s 螢幕時間 → 收尾旁白（那段結語）
  塞不下，被裁掉。要完整結語得在剪輯把最後那顆定格鏡頭（i160）拉長到 ~8s。**

## 旁白主述：不要讓「我／我們／父子」變吵（2026-09-04 使用者回饋）

畫面開場就看得出只有一對父子 → 旁白再一直講「我」「我們」「父子倆」就是在做畫面已經
做完的事（指認人物、報地名、報第幾天），聽起來像流水帳。

**定調**：全片旁白 = 父親第一人稱。「我」是預設發話者，能省就省；兒子是「他／名字／你」；
螢幕上那個雙人鏡頭就是「我們」，不用講出來。

規則（`regenerate_voiceover` 的兩個 prompt、`apply_voiceover.py --dump` 的 rules 都已寫入）：

1. 「我們／父子／父子倆」當旁白主語 → **全片上限 1 次**，留給真的要強調「兩個人一起」那一刻。
2. 「我」開頭的句子 → **全片 ≤5 句**，只放情緒轉折處（父親節的答案、「有人說我讓孩子太辛苦」）。
3. 交代型旁白（誰／哪裡／第幾天）**預設砍主語**；砍完仍累贅就整句刪，地名日期丟畫面字卡。
   例：「單車環島第二天，父子倆從屏東萬巒鄉出發，目標車城福安宮」→ 字卡「Day 2 萬巒→車城」+ 旁白留白。
4. 把主語換成景物 / 動作 / 物件，鏡頭視角本身就是「我們」。
   例：「傍晚騎行在車城海岸，夕陽與海浪令人心靈沉澱」→「傍晚的車城海岸。夕陽、浪聲，把一天的雜念磨平。」
5. 稱謂輪替：用名字、側寫「前面那個背影」、抽離的第三人稱「一個六十歲的人」。
6. 心境型可改**第二人稱**直接對兒子說：「你在後座睡著的時候，我在想…」——親密，且不累積「我我我」。
7. 情緒段落用**無主語名詞短句**：「濕的柏油。逆風。兩個鐘頭沒講話。」
8. 連續兩句不要同一種開頭（時間／地點／景物／副詞／反問輪流）。

**流程**：`regenerate_voiceover` 現在是**兩段式 LLM**——
- 第一段照 scene 預算 + seam 寫初稿（system_instruction 已含上面規則）；
- **第二段校訂**（`_polish_voiceover_voice`）：把全片初稿依鏡頭順序整份丟回去，只修「主述重複／
  句式單調／我·我們·父子超標」，**只准同長或更短、不加新資訊**，廢話句（「父子倆繼續前進」）直接清空；
- 回來後才做原本的確定性裁切 + 依鏡頭長度分配。校訂失敗（額度/格式）→ 靜默沿用初稿。
- 只接受 `_char_count(校訂) <= _char_count(初稿)` 的那幾則，其餘保留初稿（防止校訂反而變長爆預算）。

`apply_voiceover.py`（無 LLM、Claude 手寫旁白那條路）：`--dump` 的 rules 已含這 8 條，手寫時自己遵守。

## 切分規格（`backend/engines/subtitle_engine.py`）

| 參數 | 值 | 常數 |
|---|---|---|
| 閱讀速度 | 6 字/秒（舒適），上限 ~9 | `READING_CPS = 6.0` |
| 每則最短 / 最長 | 1.2s / 6.0s | `MIN_CUE_SEC` / `MAX_CUE_SEC` |
| 兩則間隔 | 0.10s（避免黏連閃爍） | `CUE_GAP_SEC` |
| 每行字數 | **16 全形字**（16:9）／ 12（9:16 直式） | `MAX_CHARS_PER_LINE` |
| 每則行數 | 2 | `MAX_LINES` |
| 每則硬上限 | 32 字 | `MAX_CHARS_PER_CUE` |

切分邏輯：句（。！？…）→ 過長切子句（，、；：）→ 仍過長硬切 → 貪婪合併相鄰短單元到接近上限。
不再限制段數（20 秒旁白該切 8–10 則就切）。`normalize_srt_cues` 做最終去重疊 / 修負長度。

**注意**：16 全形字/行是 Netflix 繁中規範的上限，不是目標。實際念起來 12–14 更舒服。
如果覺得字幕擠，先把 `MAX_CHARS_PER_LINE` 降到 14 再重跑。

## 樣式：白字 + 黑描邊 + 柔陰影

背景是大量亮天空 / 海面 / 柏油路 → 白字 + 黑描邊 + 柔陰影就夠（描邊管邊緣，陰影跟亮背景拉開層次）。
只有全白過曝畫面才需要再加半透明黑底色塊。

**中文特別注意**：描邊 `strokeWidth` 不要超過 4 — 中文筆劃密，6 會把筆劃糊成一團。

**燒進畫面的字幕（render_video.py）2026-09-03 修**：
- 不用 `subtitles` filter 的 `force_style`（含空白的字體名「PingFang TC」會靜默失效）→ 改先 SRT→ASS、
  把 `[Script Info]` 的 `PlayResX/Y` 換成實際影片解析度、`[V4+ Styles]` 的 Default 換成 preset 樣式，再 `ass=` 燒。
  沒設 PlayRes → libass 預設 288，FontSize 被放大 3–4 倍（使用者回饋「字太大」）。
- 樣式在 `backend/util/subtitle_preset.py` 的 `style` 區（`subtitle_preset.json` 覆寫）：
  `size_1080` 44（原 58 太大）、`outline_px_1080` 3、`margin_v_frac` 0.06（置中靠下、安全區內）、Alignment=2。

## 匯出目標 = DaVinci Resolve → 走 SRT 字幕軌，不要靠 FCPXML `<title>`

**Resolve 匯入 FCPXML `<title>` 會忽略 Position 參數 → 字幕全部跑到畫面正中央**，壓在人臉上，這就是「很難閱讀」的來源。字型、描邊也常常掉。

→ 已改：`FCPXMLExporter.EMIT_TITLES = False`（預設）→ **FCPXML 完全不寫 `<title>`**，字幕只走 SRT。
`rebuild_all_projects.py --fcp-titles` 才內嵌（給 Final Cut）。
症狀：使用者「還沒 import SRT 就有字幕、在正中央」= Resolve 讀到內嵌的 `<title>`。

### 正確流程（在 Resolve 裡）

1. `File ▸ Import ▸ Subtitle` → 選 `<prefix>_字幕.srt`
2. 拖到時間軸最上方字幕軌
3. 選字幕軌，Inspector 設定一次（全部套用）：

| 項目 | 值 |
|---|---|
| 字型 | PingFang TC 或 思源黑體 / Noto Sans CJK TC，**Bold** |
| Size | ~56–62（1080p） |
| 對齊 | 水平置中 |
| Position Y | 往下到畫面下緣約 9–12%（字幕安全區內） |
| Stroke / Outline | 黑，粗細 **3–4** |
| Drop Shadow | 開，不透明度 ~60%、Blur ~6、Offset 往正下方一點 |
| 背景色塊 | 關 |

## FCPXML `<title>` 現況（給 FCP 用 / 備援）

`fcpxml_exporter.py` 的 `_add_title` + class 常數：
- `SUBTITLE_SIZE = 72`、`SUBTITLE_Y = -430`（FCP 座標，畫面中心 0，下緣 -540）
- `strokeWidth="4"`、`shadowColor="0 0 0 0.6"` + `shadowBlurRadius="6"`、`lineSpacing="-8"`
- `<param name="Position">` 有寫，但只有 FCP 會讀。

## 既有工具（可 subprocess 調用）

見 `pipeline-and-tools.md` 階段 6 / 7。目前字幕時間碼是「用每鏡秒數累加估算」，**一旦錄了真旁白音檔就該改用強制對齊**：

| 工具 | 安裝 | 用途 |
|---|---|---|
| **whisper.cpp** `whisper-cli` | 已裝：`/opt/homebrew/bin/whisper-cli`（隨 `ffmpeg-full`） | 旁白音檔 → SRT 帶時間碼，native、無 Python 版本問題。`whisper-cli -m ggml-large-v3.bin -l zh -osrt audio.wav` |
| **WhisperX**（逐字 ±50ms 對齊，比 whisper.cpp 準） | ⚠️ **裝不了**：`ctranslate2==4.4.0` 無 Python 3.14 wheel。要用得另開 `.venv-stt`（Python 3.12） | 旁白音檔 → word-level timestamps，輸出 srt/json |
| **ffsubsync** | 已裝（`.venv`） | 現有 SRT 與音軌自動對齊校正（`ffsubsync ref.mp4 -i in.srt -o out.srt`），當保險 |
| **Piper TTS** / **Coqui XTTS-v2** | `pip install piper-tts` / `TTS`（未裝） | 本地中文 TTS 生旁白（XTTS 可克隆自己聲音）→ 餵給 whisper 拿時間碼 |

流程：口白定稿 → TTS 生音檔 → WhisperX 對齊 → 回寫 `timeline_layout` 每鏡秒數 → 產 SRT。
`subtitle_engine` 的切分規格（16 全形字 / ≥2s / 讀速 6cps）仍套用在 WhisperX 給的句子上。

## 原則

- 一行能講完就別折兩行；折行折在標點處，不要折在詞中間。
- 一句話拆多則時，每則語意要完整，不要「孩子卸下了 / 稚氣」這種尷尬斷點。
- 秒數寧可長一點（看得完）也不要卡太緊。
- 純環境音 / 留白鏡頭不要放字幕（`null`、`無`、`(純畫面與環境音)` 已被過濾）。
