# 參考片：三軌字幕的實作範例

`caption-to-film` 是從一支真實的個人紀錄片長出來的 —— 一趟多日的家庭單車旅行，
101 個素材（HEIC 照片 + Live Photo + 幾支長影片），素材本身帶著作者在 iOS 照片
「說明欄」裡寫的當下心情，另外還有一篇旅程結束後寫的心得長文。

這個資料夾裡的三支腳本是**那支片子實際用的編劇腳本**，不是通用工具。放在這裡是要當
「三軌字幕怎麼做」的 worked example 讀 —— 裡面的 shot index、素材檔名、場景清單都是
那支片子的，換成你自己的片子要自己填。

| 腳本 | 做什麼 |
|---|---|
| `extend_clips.py` | 把幾顆長鏡頭放回接近原長，標成「畫布」（`is_canvas`），讓感觸字幕有地方鋪 |
| `gen_vo_gemini.py` | 三段 Gemini：① 全片弧線 + 每場 mood + 畫布暗線 ② 逐場「先講故事再分句」的註解 + 串接 ③ 逐畫布把指定心得段落順成通順的感觸句 |
| `patch_vo.py` | 產完之後人工覆寫幾句（每條標理由） |

## 三種字幕軌

- **annotation**（白、下方、密）：第一人稱，像故事書 —— 這場發生什麼、心情、接下來。取材自 caption。
- **bridge**（白、每場開頭一句）：第三人稱，交代時空、接住上一場。
- **reflection**（琥珀、置中偏下、放大）：第一人稱，只鋪在「畫布」鏡頭上，逐字／接近逐字取自心得長文，慢慢浮現。

## 輸入

- `<素材夾>/reflection.md` —— 心得長文
- `scratch/extracted_captions.json` —— `[{"stem": "IMG_1234", "caption": "……"}]`，每個素材的 iOS 說明欄
- `photos_meta.json` —— 由 `scripts/probe_folder_metadata.py` 產（GPS / 日期 / 海拔 / place）

## 跑法

```bash
.venv/bin/python examples/reference-film/extend_clips.py --write
.venv/bin/python examples/reference-film/gen_vo_gemini.py --write
.venv/bin/python examples/reference-film/patch_vo.py --write        # 可選
.venv/bin/python scripts/rebuild_all_projects.py                    # 不要 --regen-vo
.venv/bin/python scripts/render_video.py --for-imovie
```

字幕規格、留白節奏、主述口氣的完整原則見 `.claude/skills/film-edit/references/subtitles-zh.md`。
