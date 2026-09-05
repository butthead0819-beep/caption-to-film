#!/usr/bin/env python3
"""把片頭的「路線動畫」地圖影片插到 storyboard 最前面。

素材 = map_clips/route_burn.mp4（make_map_clips 之外另做的環島路線逐段描繪動畫，11s / 1920x1080）。
給它自己的 scene_id（比現有最小的再小 1）→ 片頭獨立一章，會跟第一顆正片鏡頭之間做章間交叉溶接。
標 intro_card=True → gen_vo_gemini 不在上面寫旁白 / 註解；chyron 因為沒有 GPS metadata 本來就不顯示。

冪等：storyboard 第一顆已經是 route_burn 就只更新時長、不重複插。

用法：.venv/bin/python scripts/add_intro_map.py --write [--seconds N]
之後：gen_vo_gemini.py --write → patch_vo.py --write → rebuild → render_video.py
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAP_CLIP = ROOT / "map_clips" / "route_burn.mp4"
MAP_STEM = "route_burn"


def _probe_dur(p: Path) -> float:
    import subprocess
    try:
        out = subprocess.run(
            ["/opt/homebrew/opt/ffmpeg-full/bin/ffprobe", "-v", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        return 11.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = 用整支長度")
    args = ap.parse_args()

    if not MAP_CLIP.exists():
        sys.exit(f"找不到 {MAP_CLIP}（先跑 make_map_clips.py 產路線動畫）")

    full = _probe_dur(MAP_CLIP)
    keep = round(full if args.seconds <= 0 else min(args.seconds, full), 2)

    src = ROOT / f"{PREFIX}.json"
    data = json.loads(src.read_text("utf-8"))
    sb = data["storyboard"]

    sids = [int(s["scene_id"]) for s in sb if isinstance(s.get("scene_id"), (int, float))]
    intro_sid = (min(sids) - 1) if sids else 0

    first_stem = Path(str(sb[0].get("media_file", ""))).stem if sb else ""
    if first_stem == MAP_STEM:
        sb[0]["duration_seconds"] = keep
        sb[0]["source_out"] = keep
        action = "更新片頭地圖時長"
    else:
        anchor = copy.deepcopy(sb[0]) if sb else {}
        anchor.clear()
        anchor.update({
            "shot_index": 0,
            "scene_id": intro_sid,
            "scene_title": "序：環島路線",
            "scene_name": "路線動畫",
            "media_file": MAP_CLIP.name,
            "file_path": str(MAP_CLIP),
            "media_type": "video",
            "is_live_photo": False,
            "duration_seconds": keep,
            "source_in": 0.0,
            "source_out": keep,
            "shot_type": "地圖動畫",
            "visual_description": "從台中出發，逆時針描繪這趟八天環島的完整路線。",
            "visual_action": "路線動畫",
            "camera_motion": "Static",
            "transition": "直切 (Cut)",
            "voiceover": "",
            "intro_card": True,
        })
        sb.insert(0, anchor)
        action = "插入片頭地圖"

    data["storyboard"] = sb
    print(f"{action}：{MAP_CLIP.name}  {keep:.1f}s  scene_id={intro_sid}")
    print(f"storyboard：{len(sb)} 顆")
    if not args.write:
        print("（預覽，加 --write）")
        return
    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print("✅ 寫回 → gen_vo_gemini.py --write → patch_vo.py --write → rebuild → render")


if __name__ == "__main__":
    main()
