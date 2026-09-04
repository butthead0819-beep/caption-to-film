#!/usr/bin/env python3
"""階段 2：對「晃到不行」的影片做真正的畫面穩定（不是裁剪掉晃動段）。

用 ffmpeg 的 vid.stab 兩段式（vidstabdetect → vidstabtransform），輸出穩定後的
副本到 `<素材夾>/_stabilized/`，原檔不動。rebuild 會自動把該鏡頭 relink 過去
（跟 `_prepared/` 奇數尺寸修正同一套機制）。

需要有 vidstab 的 ffmpeg：`/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`
（主 ffmpeg 沒編 libvidstab；`brew install ffmpeg-full`）。

用法：
  .venv/bin/python scripts/stabilize_clips.py video_626...-sCfI3aj2.MP4 IMG_2474.MOV
  .venv/bin/python scripts/stabilize_clips.py --auto            # 掃素材夾、列晃動排名（不處理）
  .venv/bin/python scripts/stabilize_clips.py --auto --do       # 直接穩定晃動比例 > --shaky 的
  .venv/bin/python scripts/stabilize_clips.py X.MP4 --shakiness 10 --smoothing 45 --no-zoom
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.media_probe import VIDEO_EXTS  # noqa: E402

from scripts._config import MEDIA_DIR, STABILIZED_DIR  # noqa: E402

OUT_DIR = STABILIZED_DIR
FFMPEG_VIDSTAB = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"


def _ffmpeg() -> str:
    if Path(FFMPEG_VIDSTAB).exists():
        return FFMPEG_VIDSTAB
    # 退而求其次：主 ffmpeg 有沒有 vidstab
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    if "vidstabdetect" in r.stdout:
        return "ffmpeg"
    sys.exit("找不到有 vidstab 的 ffmpeg。跑 `brew install ffmpeg-full`。")


def stabilize(src: Path, ff: str, *, shakiness: int, accuracy: int,
              smoothing: int, zoom: bool, crf: int) -> Path | None:
    OUT_DIR.mkdir(exist_ok=True)
    dst = OUT_DIR / src.name
    trf = OUT_DIR / f"{src.stem}.trf"

    # 4K / 直式大解析度在成片裡本來就被裁切縮放 → 穩定工作解析度封頂 1920（長邊），快非常多
    down = "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))':flags=bicubic,"

    print(f"  [1/2] 分析晃動 {src.name} …")
    p1 = subprocess.run(
        [ff, "-nostdin", "-y", "-i", str(src),
         "-vf", f"{down}vidstabdetect=shakiness={shakiness}:accuracy={accuracy}:result={trf}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    if p1.returncode != 0 or not trf.exists():
        print(f"     ✗ vidstabdetect 失敗：{p1.stderr.strip()[-200:]}")
        return None

    # 探測位元深度 → 10-bit 來源用 HEVC main10（iPhone 4K HEVC 用 x264 會爆檔）
    _ffprobe = str(Path(ff).with_name("ffprobe")) if Path(ff).with_name("ffprobe").exists() else "ffprobe"
    probe = subprocess.run(
        [_ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True).stdout.strip()
    ten_bit = "10" in probe

    optzoom = 1 if zoom else 0
    vf = (f"{down}vidstabtransform=input={trf}:smoothing={smoothing}:optzoom={optzoom}"
          f":zoom=0:interpol=bicubic:crop=black,unsharp=5:5:0.8:3:3:0.4")
    codec = (["-c:v", "libx265", "-crf", str(crf + 2), "-preset", "medium", "-tag:v", "hvc1"]
             + (["-profile:v", "main10"] if ten_bit else []))
    print(f"  [2/2] 套用穩定 → {dst.name}（{'HEVC10' if ten_bit else 'HEVC'} crf{crf + 2}, smoothing={smoothing}）…")
    p2 = subprocess.run(
        [ff, "-nostdin", "-y", "-i", str(src), "-vf", vf, *codec,
         "-c:a", "copy", "-map_metadata", "0", str(dst)],
        capture_output=True, text=True)
    trf.unlink(missing_ok=True)
    if p2.returncode != 0 or not dst.exists():
        print(f"     ✗ vidstabtransform 失敗：{p2.stderr.strip()[-200:]}")
        return None
    mb = dst.stat().st_size / 1e6
    print(f"     ✓ {dst}  ({mb:.1f} MB)")
    return dst


def ensure_stabilized(video_paths, *, threshold: float = 0.30, ff: str | None = None,
                      shakiness: int = 8, accuracy: int = 15, smoothing: int = 30,
                      zoom: bool = True, crf: int = 18) -> dict[str, Path]:
    """規則：對「晃動比例 >= threshold」且還沒有穩定版的影片跑 vid.stab。

    冪等：`_stabilized/<name>` 已存在就跳過（不重跑分析、不重編碼）。
    回傳 {檔名: 穩定版路徑}。給 rebuild_all_projects.py --stabilize-clips 用。
    """
    import json as _json

    from backend.analyzers.motion_stability import analyze_video

    ff = ff or _ffmpeg()
    OUT_DIR.mkdir(exist_ok=True)
    cache_f = OUT_DIR / "_shaky.json"                       # 晃動比例快取，免每次 rebuild 重跑分析
    cache: dict[str, float] = {}
    if cache_f.exists():
        try:
            cache = _json.loads(cache_f.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    made: dict[str, Path] = {}
    seen: set[str] = set()
    for raw in video_paths:
        src = Path(raw)
        if src.name in seen or src.suffix.lower() not in VIDEO_EXTS or not src.exists():
            continue
        seen.add(src.name)
        dst = OUT_DIR / src.name
        if dst.exists():
            made[src.name] = dst
            continue
        frac = cache.get(src.name)
        if frac is None:
            try:
                frac = analyze_video(str(src)).shaky_fraction
            except Exception:  # noqa: BLE001
                continue
            cache[src.name] = round(frac, 3)
            cache_f.write_text(_json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
        if frac < threshold:
            continue
        print(f"   晃動 {frac:.0%} ≥ {threshold:.0%} → 穩定 {src.name}")
        out = stabilize(src, ff, shakiness=shakiness, accuracy=accuracy,
                        smoothing=smoothing, zoom=zoom, crf=crf)
        if out:
            made[src.name] = out
    return made


def _resolve(name: str) -> Path | None:
    p = Path(name)
    if p.is_absolute() and p.exists():
        return p
    for cand in (MEDIA_DIR / name, *(MEDIA_DIR / f"{Path(name).stem}{e}" for e in VIDEO_EXTS)):
        if cand.exists():
            return cand
    return None


def auto_rank() -> list[tuple[str, float]]:
    from backend.analyzers.motion_stability import analyze_video
    vids = sorted(p for p in MEDIA_DIR.iterdir()
                  if p.suffix.lower() in VIDEO_EXTS and p.is_file())
    rows = []
    print(f"分析 {len(vids)} 個影片的晃動比例（motion_stability，慢）…")
    for v in vids:
        try:
            rep = analyze_video(str(v))
            rows.append((v.name, rep.shaky_fraction))
        except Exception:  # noqa: BLE001
            rows.append((v.name, -1.0))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="要穩定的影片檔名（素材夾內）")
    ap.add_argument("--auto", action="store_true", help="掃素材夾列晃動排名")
    ap.add_argument("--do", action="store_true", help="配合 --auto：直接穩定晃動比例 > --shaky 的")
    ap.add_argument("--shaky", type=float, default=0.30, help="--auto --do 的晃動比例門檻（預設 0.30）")
    ap.add_argument("--shakiness", type=int, default=8, help="vidstabdetect 偵測強度 1-10（預設 8）")
    ap.add_argument("--accuracy", type=int, default=15, help="vidstabdetect 精度 1-15（預設 15）")
    ap.add_argument("--smoothing", type=int, default=30,
                    help="平滑窗（幀，越大越穩但越像空拍；30≈1秒，預設 30）")
    ap.add_argument("--no-zoom", action="store_true", help="不自動放大填黑邊（會看到晃動的黑框）")
    ap.add_argument("--crf", type=int, default=18, help="輸出畫質 CRF（越小越好，預設 18）")
    args = ap.parse_args()

    ff = _ffmpeg()
    targets: list[Path] = []

    if args.auto:
        rows = auto_rank()
        print("\n晃動比例排名：")
        for name, frac in rows:
            mark = "  ← 建議穩定" if frac >= args.shaky else ""
            print(f"  {frac:5.0%}  {name}{mark}" if frac >= 0 else f"   n/a   {name}")
        if not args.do:
            print("\n（只列排名。要處理：加 --do，或直接給檔名）")
            return
        targets = [p for name, frac in rows if frac >= args.shaky and (p := _resolve(name))]
    else:
        for n in args.names:
            r = _resolve(n)
            if r:
                targets.append(r)
            else:
                print(f"⚠️  找不到 {n}")

    if not targets:
        sys.exit("沒有要處理的檔案。")

    done = []
    for src in targets:
        print(f"\n▶ {src.name}")
        out = stabilize(src, ff, shakiness=args.shakiness, accuracy=args.accuracy,
                        smoothing=args.smoothing, zoom=not args.no_zoom, crf=args.crf)
        if out:
            done.append(out.name)

    print(f"\n✅ 穩定 {len(done)} 個 → {OUT_DIR}/")
    if done:
        print("   下一步：跑 rebuild_all_projects.py，會自動把這些鏡頭 relink 到穩定版。")
        for base in (Path.cwd(),):
            _ = base  # noqa
    # 同步一份到專案的 media 搜尋路徑不需要——rebuild 直接讀素材夾 _stabilized/


if __name__ == "__main__":
    main()
