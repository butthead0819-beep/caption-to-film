#!/usr/bin/env python3
"""用 Gemini 幫素材資料夾產生「場景標籤」→ 併進 scripts/photos_meta.json。

取代 Apple ML 場景標籤（不用開 Photos App / 不佔空間）。標籤詞彙對齊
backend/engines/grading_engine.py 的調色 preset，讓調色與 LLM 主題直接吃得到。

- 只處理還沒有 labels 的素材（可 --force 重跑）
- 影片抽中間一幀分析
- 隨時可中斷：每張都寫回檔案；遇到 429 額度用完會存檔後優雅結束
- 免費層約 20 次/分鐘 + 每日上限，用 --sleep / --limit 控制

用法:
  .venv/bin/python scripts/gemini_scene_labels.py
  .venv/bin/python scripts/gemini_scene_labels.py -i /path/folder --limit 40 --sleep 4
"""

import argparse
import io
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import config
from backend.analyzers.video_sampler import extract_sampled_frames

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "scripts" / "photos_meta.json"
from scripts._config import MEDIA_DIR  # noqa: E402
DEFAULT_FOLDER = str(MEDIA_DIR)
IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VID = {".mov", ".mp4", ".m4v"}

# 對齊 grading_engine._PRESETS + abroll/highlight 用得到的
VOCAB = ("sunrise sunset dusk dawn golden-hour daylight night "
         "beach ocean sea coast wave lake river reflection waterfall harbor "
         "forest tree grass field rice mountain hill valley cliff "
         "road highway bridge tunnel "
         "urban street building architecture sign temple shrine market "
         "food meal drink dessert "
         "snow fog mist cloud rain "
         "bicycle vehicle train "
         "portrait selfie group-photo people crowd animal dog "
         "indoor vehicle-interior sky panorama")

PROMPT = f"""你是紀錄片剪輯師。看這張影格，回傳 JSON（只回 JSON）：
{{
  "scene_labels": ["最多 5 個，優先從這個詞表挑，可加 1~2 個表內沒有的英文小寫詞: {VOCAB}"],
  "mood": "一個中文詞（如 溫馨/壯闊/寧靜/歡樂/疲憊/緊張）",
  "has_people": true/false,
  "time_of_day": "dawn|morning|noon|afternoon|golden-hour|dusk|night|unknown"
}}"""


def frame_bytes(path: Path) -> bytes | None:
    from PIL import Image

    if path.suffix.lower() in IMG:
        try:
            im = Image.open(path)
            im.thumbnail((1024, 1024))
            b = io.BytesIO()
            im.convert("RGB").save(b, "JPEG", quality=85)
            return b.getvalue()
        except Exception:
            return None
    # 影片：抽中間幀
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip() or 2.0)
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(max(0.1, dur / 2)),
             "-i", str(path), "-frames:v", "1", "-vf", "scale=1024:-2",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=40)
        return out.stdout or None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", default=DEFAULT_FOLDER)
    ap.add_argument("--meta", default=str(META))
    ap.add_argument("--limit", type=int, default=1000, help="這次最多分析幾張")
    ap.add_argument("--sleep", type=float, default=3.0, help="每次呼叫間隔秒")
    ap.add_argument("--force", action="store_true", help="連已有 labels 的也重跑")
    ap.add_argument("--video-interval", type=float, default=None,
                    help="若處理長影片，指定抽樣間隔秒數（如 10.0 表示 10 秒 1 幀）；省略則只抽中點單幀")
    args = ap.parse_args()

    if not config.gemini_api_key:
        sys.exit("沒有 GEMINI_API_KEY / GOOGLE_API_KEY")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.gemini_api_key)
    meta_path = Path(args.meta)
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}

    files = sorted(f for f in Path(args.input).iterdir()
                   if f.suffix.lower() in (IMG | VID) and not f.name.startswith("."))
    seen_stems = set()
    todo = []
    for f in files:
        st = f.stem.lower()
        if st in seen_stems:
            continue
        seen_stems.add(st)
        rec = meta.get(st, {})
        if rec.get("labels") and not args.force:
            continue
        todo.append(f)

    print(f"待分析 {len(todo)} 張（上限 {args.limit}）")
    done = 0
    for f in todo[:args.limit]:
        st = f.stem.lower()
        
        contents = []
        if f.suffix.lower() in VID and args.video_interval:
            sampled = extract_sampled_frames(f, interval_s=args.video_interval, max_dimension=1024)
            if not sampled:
                print(f"  · 跳過 {f.name}（抽幀失敗）")
                continue
            for ts, b in sampled:
                contents.append(f"[{ts:.1f}s]")
                contents.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))
        else:
            b = frame_bytes(f)
            if not b:
                print(f"  · 跳過 {f.name}（抽幀失敗）")
                continue
            contents.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))

        contents.append(PROMPT)
        try:
            resp = client.models.generate_content(
                model=config.vision_model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            r = json.loads(resp.text)
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                print(f"\n⚠️ Gemini 額度用完，已分析 {done} 張、存檔後停止。稍後再跑一次會接續。")
                break
            print(f"  · {f.name} 失敗：{msg[:80]}")
            continue

        rec = meta.setdefault(st, {})
        labels = [str(x).lower().strip() for x in (r.get("scene_labels") or []) if x]
        if r.get("time_of_day") in ("golden-hour", "dusk", "dawn"):
            labels.append("golden hour")
        if r.get("time_of_day") == "night":
            labels.append("night")
        rec["labels"] = sorted(set(labels))
        rec.setdefault("activities", [])
        if r.get("mood"):
            rec["mood"] = r["mood"]
        rec["has_people"] = bool(r.get("has_people"))
        rec.setdefault("gps", rec.get("gps"))
        done += 1
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        print(f"  ✓ {f.name}: {rec['labels']} | {rec.get('mood','')} | 人={rec['has_people']}")
        time.sleep(args.sleep)

    n_lab = sum(1 for v in meta.values() if v.get("labels"))
    print(f"\n完成本輪 {done} 張。photos_meta.json 目前 {n_lab} 張有場景標籤。")


if __name__ == "__main__":
    main()
