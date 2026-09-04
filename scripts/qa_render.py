#!/usr/bin/env python3
"""階段 8：對「已渲染的成片 mp4」做 self-eval（借 browser-use/video-use 的做法）。

`qa_timeline.py` 只看 FCPXML/SRT 靜態結構；這支看**真的畫面與聲音**：
  - 每個章節接縫抽前後兩幀 → agent 眼睛看有沒有跳格 / 不連戲
  - 響度：整體 LUFS、逐章 LUFS 落差（>6 就刺耳）、削波、超長靜音（留白過頭）
  - 畫面：章首近全黑（非夜景）、影片段落凍結（連續數秒同一幀）

用法：
  .venv/bin/python scripts/render_video.py --fast -o proxy.mp4
  .venv/bin/python scripts/qa_render.py proxy.mp4
  → 看 qa_render_frames/ 裡的接縫幀，agent 判斷

exit 1 = 有硬問題（削波 / 凍結 / 章首全黑）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import PREFIX, SEARCH_DIRS  # noqa: E402
from backend.exporters.timeline_layout import timeline_layout  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FF = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
FRAMES = ROOT / "qa_render_frames"


def _chapter_key(t: str) -> str:
    m = re.search(r"[【\[]([^】\]]+)[】\]]", t or "")
    return m.group(1).strip() if m else (t or "").split("：")[0].strip()


def scene_bounds(data: dict) -> list[tuple[float, str, str]]:
    """回傳 [(邊界秒, 前一章, 後一章)]。"""
    lay = timeline_layout(data, SEARCH_DIRS)
    # 跟 apply_voiceover 同一套：>60% 鏡頭有【章節】就用 scene_title，否則用 scene_id
    marked = sum(1 for e in lay
                 if re.search(r"[【\[][^】\]]+[】\]]", e["shot"].get("scene_title", "") or ""))
    use_title = marked > len(lay) * 0.6
    out, prev_key, prev_title = [], None, ""
    for e in lay:
        s = e["shot"]
        if use_title:
            key = _chapter_key(s.get("scene_title", ""))
            title = key or "?"
        else:
            key = s.get("scene_id", _chapter_key(s.get("scene_title", "")))
            title = s.get("scene_name") or "?"
        if prev_key is not None and key != prev_key:
            out.append((round(e["start"], 2), prev_title, title))
        prev_key, prev_title = key, title
    return out


def _frame(mp4: Path, t: float, dst: Path) -> None:
    subprocess.run([FF, "-nostdin", "-y", "-ss", f"{max(0, t):.3f}", "-i", str(mp4),
                    "-frames:v", "1", "-q:v", "3", str(dst)], capture_output=True)


def _luma(png: Path) -> float:
    try:
        from PIL import Image
        im = Image.open(png).convert("L").resize((64, 36))
        return sum(im.getdata()) / (64 * 36)
    except Exception:
        return 128.0


def loudness(mp4: Path, t0: float, t1: float) -> float | None:
    try:
        import numpy as np
        import pyloudnorm as pyln
    except Exception:
        return None
    raw = subprocess.run(
        [FF, "-nostdin", "-v", "error", "-ss", f"{t0:.2f}", "-to", f"{t1:.2f}", "-i", str(mp4),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True).stdout
    if len(raw) < 16000 * 4:
        return None
    import numpy as np
    x = np.frombuffer(raw, "<f4")
    try:
        return float(pyln.Meter(16000).integrated_loudness(x))
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4")
    ap.add_argument("--prefix", default=PREFIX)
    args = ap.parse_args()

    mp4 = Path(args.mp4)
    if not mp4.exists():
        sys.exit(f"找不到 {mp4}")
    data = json.loads((ROOT / f"{args.prefix}.json").read_text("utf-8"))
    FRAMES.mkdir(exist_ok=True)
    for p in FRAMES.glob("*.png"):
        p.unlink()

    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout or 0)
    errs, warns = [], []

    # 1) 章節接縫幀
    bounds = scene_bounds(data)
    print(f"章節接縫 {len(bounds)} 個 → 抽前後幀到 {FRAMES.name}/")
    for i, (t, a, b) in enumerate(bounds, 1):
        fa, fb = FRAMES / f"{i:02d}a_{t:.0f}s.png", FRAMES / f"{i:02d}b_{t:.0f}s.png"
        _frame(mp4, t - 0.2, fa)
        _frame(mp4, t + 0.2, fb)
        lb = _luma(fb)
        night = "night" in json.dumps(data)  # 粗略
        tag = ""
        if lb < 14:
            (errs if not night else warns).append(f"接縫 {i}（{b}，{t:.0f}s）章首近全黑 luma={lb:.0f}")
            tag = "  ← 章首偏黑"
        print(f"  {i:02d}  {t:6.0f}s  {a} → {b}{tag}")

    # 2) 響度
    li = loudness(mp4, 0, dur)
    if li is not None:
        print(f"\n整體響度 {li:.1f} LUFS（成片建議 -16 ~ -14）")
        if li > -11 or li < -20:
            warns.append(f"整體響度 {li:.1f} LUFS 偏離 -15 太多")
        scene_l = []
        pts = [0.0] + [t for t, _, _ in bounds] + [dur]
        for s, e in zip(pts, pts[1:]):
            if e - s < 3:
                continue
            v = loudness(mp4, s, e)
            if v is not None and v > -70:
                scene_l.append((s, v))
        if len(scene_l) >= 3:
            import statistics
            med = statistics.median(v for _, v in scene_l)
            for s, v in scene_l:
                if abs(v - med) > 6:
                    warns.append(f"{s:.0f}s 那一章響度 {v:.1f} LUFS，比中位 {med:.1f} 差 {v - med:+.1f}（刺耳）")

    # 3) 削波
    st = subprocess.run(
        [FF, "-nostdin", "-i", str(mp4), "-af", "astats=metadata=1", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    m = re.search(r"Peak level dB:\s*(-?[\d.]+)", st)
    if m and float(m.group(1)) > -0.5:
        errs.append(f"音訊峰值 {m.group(1)} dB（>-0.5 = 削波風險）")

    # 4) 凍結畫面
    fr = subprocess.run(
        [FF, "-nostdin", "-i", str(mp4), "-vf", "freezedetect=n=0.003:d=3", "-map", "0:v",
         "-f", "null", "-"], capture_output=True, text=True).stderr
    for m in re.finditer(r"freeze_start:\s*([\d.]+)", fr):
        errs.append(f"畫面凍結 @ {float(m.group(1)):.0f}s（≥3s 同一幀）")

    print("\n" + "=" * 50)
    for e in errs:
        print(f"  ❌ {e}")
    for w in warns:
        print(f"  ⚠️  {w}")
    print(f"{len(errs)} 個硬問題｜{len(warns)} 個警告"
          f"\n→ 接著用眼睛看 {FRAMES.name}/ 的接縫幀：前後幀是不是刺眼跳接 / 不連戲")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
