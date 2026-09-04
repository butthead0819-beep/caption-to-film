#!/usr/bin/env python3
"""DaVinci Resolve 自動一級調色 (在 Resolve 執行)。

對「目前開啟的時間軸」上每個片段：
  1. 用 ffmpeg 取樣數張畫格，分析平均 RGB 與亮度分佈。
  2. 計算 ASC-CDL (Slope / Offset / Power / Saturation)：
       - 灰世界白平衡 (自動校色偏)
       - 黑白點正規化 (自動對比)
       - 輕微飽和度提升
  3. 透過 Resolve API `TimelineItem.SetCDL()` 套到片段第 1 個節點 (非破壞性，可在色彩頁再微調)。
  4. 可選：`--lut foo.cube` 對每個片段再套一顆 LUT。

執行方式 (Resolve 開著，終端機)：
  export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
  export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
  export PYTHONPATH="$RESOLVE_SCRIPT_API/Modules:$PYTHONPATH"
  python3 scripts/resolve_auto_grade.py --strength 0.8 [--lut "/path/to.cube"] [--reset]

也可從 Resolve 的 Workspace ▸ Console / Scripts 選單直接執行 (參數改用下方 CONFIG)。
"""

import argparse
import subprocess
import sys
from pathlib import Path

# 從 Resolve Scripts 選單執行 (無法傳參數) 時使用的預設值
CONFIG = {"strength": 0.8, "lut": "", "reset": False, "sample_seconds": 24, "sample_fps": 1}

GREY_TARGET = 0.5
SLOPE_CLAMP = (0.80, 1.90)
WB_CLAMP = (0.82, 1.22)
SAT_BOOST = 1.06


def get_resolve():
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except ImportError:
        for p in (
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
            str(Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"),
        ):
            if Path(p).is_dir():
                sys.path.append(p)
        try:
            import DaVinciResolveScript as dvr  # type: ignore
        except ImportError:
            sys.exit("找不到 DaVinciResolveScript 模組；請先設定 RESOLVE_SCRIPT_API / PYTHONPATH，並確認 Resolve 正在執行。")
    r = dvr.scriptapp("Resolve")
    if r is None:
        sys.exit("無法連上 Resolve，請確認應用程式已開啟。")
    return r


def sample_stats(path: str, seconds: int, fps: int):
    """用 ffmpeg 抽幀，回傳 (mean_rgb[3], black_point, white_point)，值域 0~1。"""
    import numpy as np

    W, H = 128, 72
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-t", str(seconds), "-i", path,
        "-vf", f"fps={fps},scale={W}:{H},format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    except Exception:
        return None
    if not out:
        # 靜態圖片：抓單張
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
             "-vf", f"scale={W}:{H},format=rgb24", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=60,
        ).stdout
        if not out:
            return None
    n = len(out) // (W * H * 3)
    if n == 0:
        return None
    px = np.frombuffer(out[: n * W * H * 3], dtype=np.uint8).astype(np.float32).reshape(-1, 3) / 255.0
    mean_rgb = px.mean(axis=0)
    luma = px @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    black = float(np.percentile(luma, 1.0))
    white = float(np.percentile(luma, 99.0))
    if white - black < 0.2:
        white = min(1.0, black + 0.2)
    return mean_rgb, black, white


def compute_cdl(stats, strength: float):
    import numpy as np

    mean_rgb, black, white = stats
    span = max(1e-3, white - black)
    global_slope = min(SLOPE_CLAMP[1], max(SLOPE_CLAMP[0], 1.0 / span))

    grey = float(np.mean(mean_rgb))
    grey = grey if grey > 1e-3 else GREY_TARGET
    wb = np.clip(grey / np.maximum(mean_rgb, 1e-3), *WB_CLAMP)

    slope = global_slope * wb
    offset = np.full(3, -black * global_slope, dtype=np.float32)
    power = np.ones(3, dtype=np.float32)
    sat = SAT_BOOST

    k = max(0.0, min(1.0, strength))
    slope = 1.0 + (slope - 1.0) * k
    offset = offset * k
    sat = 1.0 + (sat - 1.0) * k

    fmt = lambda a: " ".join(f"{v:.5f}" for v in a)
    return {
        "NodeIndex": "1",
        "Slope": fmt(slope),
        "Offset": fmt(offset),
        "Power": fmt(power),
        "Saturation": f"{sat:.4f}",
    }


IDENTITY_CDL = {
    "NodeIndex": "1", "Slope": "1 1 1", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": "1",
}


def iter_items(timeline):
    for t in range(1, timeline.GetTrackCount("video") + 1):
        for item in timeline.GetItemListInTrack("video", t) or []:
            yield item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strength", type=float, default=CONFIG["strength"])
    ap.add_argument("--lut", default=CONFIG["lut"])
    ap.add_argument("--reset", action="store_true", default=CONFIG["reset"])
    ap.add_argument("--hints", default=None,
                    help="grade_hints.json（依 Apple 場景標籤疊在灰世界之上）")
    args, _ = ap.parse_known_args()

    hints = {}
    if args.hints and Path(args.hints).exists():
        import json as _json
        hints = {k.lower(): v for k, v in _json.loads(Path(args.hints).read_text("utf-8")).items()}
        print(f"載入 {len(hints)} 筆場景調色建議：{args.hints}")

    def apply_hint(cdl, stem):
        h = hints.get(str(stem).lower())
        if not h:
            return cdl
        sl = [float(x) for x in cdl["Slope"].split()]
        of = [float(x) for x in cdl["Offset"].split()]
        sl = [a * b for a, b in zip(sl, h.get("slope_mul", [1, 1, 1]))]
        of = [a + b for a, b in zip(of, h.get("offset_add", [0, 0, 0]))]
        sat = float(cdl["Saturation"]) * h.get("sat_mul", 1.0)
        return {**cdl, "Slope": " ".join(f"{v:.5f}" for v in sl),
                "Offset": " ".join(f"{v:.5f}" for v in of), "Saturation": f"{sat:.4f}"}

    resolve = get_resolve()
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    if not timeline:
        sys.exit("目前沒有開啟的時間軸。")

    print(f"時間軸: {timeline.GetName()}  強度={args.strength}  LUT={args.lut or '(無)'}")
    done = skipped = 0
    seen_cache = {}

    for item in iter_items(timeline):
        name = item.GetName()
        if args.reset:
            item.SetCDL(dict(IDENTITY_CDL))
            done += 1
            continue

        mp = item.GetMediaPoolItem()
        path = mp.GetClipProperty("File Path") if mp else ""
        if not path or not Path(path).exists():
            print(f"  · 跳過 {name} (找不到來源檔)")
            skipped += 1
            continue

        if path not in seen_cache:
            stats = sample_stats(path, CONFIG["sample_seconds"], CONFIG["sample_fps"])
            seen_cache[path] = compute_cdl(stats, args.strength) if stats else None
        cdl = seen_cache[path]
        if not cdl:
            print(f"  · 跳過 {name} (無法分析畫面)")
            skipped += 1
            continue

        cdl = apply_hint(cdl, Path(path).stem)
        ok = item.SetCDL(dict(cdl))
        if ok and args.lut:
            item.SetLUT(1, args.lut)
        tag = f"  [{hints[Path(path).stem.lower()]['note']}]" if Path(path).stem.lower() in hints else ""
        print(f"  ✓ {name}  Slope={cdl['Slope']}  Sat={cdl['Saturation']}{tag}")
        done += 1

    print(f"\n完成：{done} 個片段已套用，{skipped} 個略過。")
    if not args.reset:
        print("到「調色」頁面即可在每個片段的第 1 個節點上再微調。")


if __name__ == "__main__":
    main()
