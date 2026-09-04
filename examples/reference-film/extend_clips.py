#!/usr/bin/env python3
"""把幾顆長影片素材放回接近原長 —— 當「感觸畫布」（在上面寫非場景內的人生體悟）
或讓活動片段呼吸。其餘鏡頭不動。

用法：.venv/bin/python examples/reference-film/extend_clips.py [--write]
之後：gen_vo_gemini.py --write → patch_vo.py --write → rebuild（不要 --regen-vo）→ render

CANVAS / BREATHE 的 key 是素材檔名（去副檔名），這裡填的是參考片的清單 —— 換成你自己的。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts._config import MEDIA_DIR, PREFIX, SEARCH_DIRS  # noqa: E402
from backend.util.media_probe import resolve_existing_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# stem -> (目標秒數, 前面留白秒數)
# 感觸畫布（在放長的鏡頭上鋪心得體悟）—— 有故事可講就給足時間，不會無聊
CANVAS: dict[str, tuple[float, float]] = {
    # "IMG_1234": (28.0, 2.0),   # 開場
    # "IMG_5678": (93.0, 3.5),   # 情緒最高點
}
# 活動片段：有動作 / 有梗的放多一點
BREATHE: dict[str, tuple[float, float]] = {
    # "IMG_2222": (14.0, 0.5),
}
PLAN = {**CANVAS, **BREATHE}
CANVAS_STEMS = set(CANVAS)


def _dur(p: str) -> float:
    try:
        return float(subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip())
    except Exception:
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    d = json.loads((ROOT / f"{PREFIX}.json").read_text("utf-8"))
    hits = 0
    for s in d["storyboard"]:
        stem = Path(str(s.get("media_file") or s.get("file_path") or "")).stem
        if stem not in PLAN:
            continue
        src = resolve_existing_path(str(s.get("file_path") or s.get("media_file") or ""), SEARCH_DIRS)
        real = _dur(str(src)) if src else 0.0
        want, head = PLAN[stem]
        head = min(head, max(0.0, real - want - 0.3))
        keep = min(want, max(1.0, real - head - 0.3))
        s["source_in"] = round(head, 3)
        s["source_out"] = round(head + keep, 3)
        s["duration_seconds"] = round(keep, 2)
        s["is_canvas"] = stem in CANVAS_STEMS         # gen_vo_gemini 讀這個決定放不放「感觸」
        s.pop("timed_subtitles", None)
        tag = "感觸畫布" if stem in CANVAS_STEMS else "活動呼吸"
        print(f"  #{s.get('shot_index'):>3} {stem[:26]:<26} 原{real:5.1f}s → {keep:5.1f}s  [{tag}]")
        hits += 1

    total = sum(
        (s.get("source_out", 0) - s.get("source_in", 0)) if s.get("source_out")
        else s.get("duration_seconds", 4)
        for s in d["storyboard"] if not s.get("skip"))
    print(f"\n改了 {hits} 顆｜估計新片長 ~{total/60:.1f} 分")
    if not args.write:
        print("（預覽，加 --write）")
        return
    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    print("✅ 寫回 → gen_vo_gemini.py --write")


if __name__ == "__main__":
    main()
