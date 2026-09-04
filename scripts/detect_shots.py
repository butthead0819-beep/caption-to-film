#!/usr/bin/env python3
"""階段 0：把長影片切成「候選鏡頭」清單，供分鏡 / 精華篩選 / 晃動剪除當前置。

長影片（例如一鏡到底的騎行 / 走路片段）在 storyboard 裡只佔一顆鏡頭，
Gemini 也只給一句口白。這支用 PySceneDetect 找出片內的 cut / 內容轉折，
輸出每個影片的候選區段（start, end），讓後續可以「挑片內最好的那 1-2 秒」
而不是硬截頭尾。

作法：PySceneDetect AdaptiveDetector（對手震較不會誤切；skill 註記過），
內容變化太小的長片再退回 ContentDetector 補一刀。

輸出 scripts/shot_candidates.json：
  { "<檔名stem小寫>": {
      "file": "/abs/path/IMG_2591.MOV",
      "duration": 174.3,
      "detector": "adaptive",
      "scenes": [[0.0, 12.4], [12.4, 30.1], ...],   # 秒
      "n_scenes": 9 } }

用法：
  .venv/bin/python scripts/detect_shots.py                    # 掃預設素材夾
  .venv/bin/python scripts/detect_shots.py -i a.mov b.mp4     # 指定檔案
  .venv/bin/python scripts/detect_shots.py --min-seconds 6 --thumbs --merge
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.media_probe import VIDEO_EXTS, probe_video  # noqa: E402

from scripts._config import MEDIA_DIR  # noqa: E402
OUT = Path(__file__).resolve().parent / "shot_candidates.json"
THUMB_DIR = Path(__file__).resolve().parent / "shot_candidates_thumbs"


def _iter_videos(inputs: list[str]) -> list[Path]:
    if inputs:
        return [Path(p).expanduser().resolve() for p in inputs]
    return sorted(
        p for p in MEDIA_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


def detect_one(path: Path, min_scene_seconds: float) -> dict | None:
    """回傳單一影片的候選鏡頭；偵測失敗回 None。"""
    from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video

    try:
        video = open_video(str(path))
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  {path.name}: 無法開啟 ({e})")
        return None

    fps = video.frame_rate or 30.0
    min_len_frames = max(1, int(round(min_scene_seconds * fps)))

    sm = SceneManager()
    sm.add_detector(AdaptiveDetector(min_scene_len=min_len_frames))
    detector = "adaptive"
    try:
        sm.detect_scenes(video, show_progress=False)
        scenes = sm.get_scene_list()
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  {path.name}: adaptive 偵測失敗 ({e})，改用 content")
        scenes = []

    # 長片但幾乎沒切點 → 內容偵測補一次（騎行片畫面漸變，adaptive 常給 1 段）
    dur = probe_video(str(path)).get("duration_s", 0.0)
    if len(scenes) <= 1 and dur > 20:
        video.reset()
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=27.0, min_scene_len=min_len_frames))
        try:
            sm.detect_scenes(video, show_progress=False)
            scenes = sm.get_scene_list()
            detector = "content"
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️  {path.name}: content 偵測也失敗 ({e})")

    ranges: list[list[float]] = []
    if scenes:
        for start, end in scenes:
            ranges.append([round(start.seconds, 3), round(end.seconds, 3)])
    else:
        ranges = [[0.0, round(dur, 3)]]  # 整片當一段

    return {
        "file": str(path),
        "duration": round(dur, 3),
        "detector": detector,
        "scenes": ranges,
        "n_scenes": len(ranges),
    }


def save_thumbs(stem: str, info: dict) -> None:
    out_dir = THUMB_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (a, b) in enumerate(info["scenes"], start=1):
        mid = (a + b) / 2.0
        dst = out_dir / f"{i:02d}_{mid:07.2f}s.jpg"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{mid:.3f}",
             "-i", info["file"], "-frames:v", "1", "-vf", "scale=480:-2", str(dst)],
            check=False,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", nargs="*", default=[],
                    help="指定影片檔（省略則掃預設素材夾）")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--min-seconds", type=float, default=8.0,
                    help="影片短於此秒數就不切（預設 8）")
    ap.add_argument("--min-scene-seconds", type=float, default=1.2,
                    help="單一候選鏡頭最短秒數（預設 1.2）")
    ap.add_argument("--thumbs", action="store_true",
                    help="每個候選鏡頭抽一張中點縮圖到 shot_candidates_thumbs/")
    ap.add_argument("--merge", action="store_true",
                    help="保留現有 json 裡這次沒掃到的項目")
    args = ap.parse_args()

    videos = _iter_videos(args.input)
    print(f"掃描 {len(videos)} 個影片（min-seconds={args.min_seconds}）…")

    result: dict[str, dict] = {}
    skipped_short = 0
    for path in videos:
        if not path.exists():
            print(f"   ⚠️  找不到 {path}")
            continue
        dur = probe_video(str(path)).get("duration_s", 0.0)
        if dur < args.min_seconds:
            skipped_short += 1
            continue
        print(f"   • {path.name}  ({dur:.1f}s)")
        info = detect_one(path, args.min_scene_seconds)
        if info is None:
            continue
        result[path.stem.lower()] = info
        if args.thumbs:
            save_thumbs(path.stem.lower(), info)

    out_path = Path(args.out)
    if args.merge and out_path.exists():
        old = json.loads(out_path.read_text(encoding="utf-8"))
        old.update(result)
        result = old

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    multi = sum(1 for v in result.values() if v["n_scenes"] > 1)
    total_scenes = sum(v["n_scenes"] for v in result.values())
    print(
        f"\n✅ {out_path}\n"
        f"   {len(result)} 個影片｜{multi} 個切出多段｜共 {total_scenes} 個候選鏡頭"
        f"｜跳過 {skipped_short} 個短片"
        + ("\n   縮圖：" + str(THUMB_DIR) if args.thumbs else "")
    )


if __name__ == "__main__":
    main()
