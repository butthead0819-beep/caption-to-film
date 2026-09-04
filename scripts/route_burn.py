#!/usr/bin/env python3
"""路線圖「燃燒火線」動畫 → map_clips/route_burn.mp4（給片名卡用）。

見 `.claude/skills/film-edit/references/map-clips.md` /「片頭火線」節。

把環島 GPS 軌跡逐幀畫成一條燒過去的導火線：
  - 火頭：白核→黃→橙疊圓 + 高斯發光 + 閃爍火花
  - 尾巴：燃完的餘燼灰，離火頭越遠越暗、會淡（floor 留一條淡淡的鬼線）
  - 點燃：火頭經過每個 Day 起點 / 名字地點 → 擴散光環 + pin 亮起 + 地名/Day 標籤淡入保留
  - 鏡頭：先跟拍火頭（zoom ~1.9），最後 ~1.8s 拉遠到全島 → 整圈亮起 → 片名淡入

需要：PIL + numpy + ffmpeg（都裝好了）。衛星底圖用 Esri World Imagery（跟 make_map_clips 同源）。

用法：
  .venv/bin/python scripts/route_burn.py                       # 用預設 storyboard 的順序
  .venv/bin/python scripts/route_burn.py --storyboard <p>.json --title "逐光而行" --seconds 10
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import re as _re  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "scripts" / "photos_meta.json"
OUT_DIR = ROOT / "map_clips"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"

OUT_W, OUT_H = 1920, 1080
OVERSAMPLE = 2                       # 底圖抓 2x 給跟拍 zoom 用
FOLLOW_ZOOM = 1.9
N_SAMPLES = 1600
_DAY_RE = _re.compile(r"(Day\s*\d+|第[一二三四五六七八九十\d]+篇|序章|尾聲)")
_CJK_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _font(size: int):
    from PIL import ImageFont
    for p in _CJK_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _dt(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", ""))
    except ValueError:
        return None


def fetch(bbox, w, h, tries=4):
    params = {"bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}", "bboxSR": 4326,
              "imageSR": 3857, "size": f"{w},{h}", "format": "jpg", "f": "image"}
    last = None
    for k in range(tries):
        try:
            r = requests.get(ESRI, params=params, timeout=60)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(2 * (k + 1))
    raise last


def pad_bbox(pts, pad_ratio=0.12):
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    s, n, w, e = min(lats), max(lats), min(lons), max(lons)
    dy, dx = max(n - s, 0.02), max(e - w, 0.02)
    if dx / dy < OUT_W / OUT_H:
        dx = dy * OUT_W / OUT_H
    else:
        dy = dx * OUT_H / OUT_W
    cy, cx = (s + n) / 2, (w + e) / 2
    return (cx - dx * (0.5 + pad_ratio), cy - dy * (0.5 + pad_ratio),
            cx + dx * (0.5 + pad_ratio), cy + dy * (0.5 + pad_ratio))


# ── 讀 GPS 軌跡 ─────────────────────────────────────────────────────────────
def load_track(storyboard: Path | None, meta_path: Path):
    meta = {k.lower(): v for k, v in json.loads(meta_path.read_text("utf-8")).items()} \
        if meta_path.exists() else {}
    order: list[tuple[str, dict]] = []
    if storyboard and storyboard.exists():
        for s in json.loads(storyboard.read_text("utf-8")).get("storyboard", []):
            stem = Path(str(s.get("file_path") or s.get("media_file") or "")).stem.lower()
            order.append((stem, s))
    else:
        order = [(k, {}) for k in meta]

    pts = []
    for stem, s in order:
        m = meta.get(stem) or {}
        g = m.get("gps")
        if not g:
            continue
        pts.append({
            "lat": g["lat"], "lon": g["lon"], "scene": s.get("scene_title", ""),
            "place": (m.get("place") or {}).get("name"), "taken": _dt(m.get("taken")),
            "scene_id": s.get("scene_id"), "scene_name": s.get("scene_name"),
        })
    # 去掉連續重複點
    out = [pts[0]] if pts else []
    for p in pts[1:]:
        if abs(p["lat"] - out[-1]["lat"]) > 1e-5 or abs(p["lon"] - out[-1]["lon"]) > 1e-5:
            out.append(p)
    return out


def _daykey(p):
    """章節單位優先序：scene_id（segment_scenes 切的）> Day N > 拍攝日。"""
    if p.get("scene_id") is not None:
        return f"s{p['scene_id']}"
    m = _DAY_RE.search(str(p["scene"]))
    if m:
        return m.group(1).replace(" ", "")
    return p["taken"].date().isoformat() if p["taken"] else "?"


def _clean_place(name: str) -> str:
    """「臺東縣臺東市·台東體育館」→「臺東市」；「屏東縣萬巒鄉」→「萬巒鄉」。"""
    n = _re.split(r"[·•]", str(name or ""))[0].strip()
    m = _re.match(r"^.{2,3}[縣市](.+[鄉鎮市區])$", n)
    return m.group(1) if m else n


# ── 幾何：投影 + 依弧長重取樣，每天等時 ──────────────────────────────────────
def build_geometry(pts, bbox, bw, bh):
    def proj(lat, lon):
        x = (lon - bbox[0]) / (bbox[2] - bbox[0]) * bw
        y = (merc_y(bbox[3]) - merc_y(lat)) / (merc_y(bbox[3]) - merc_y(bbox[1])) * bh
        return x, y

    raw = np.array([proj(p["lat"], p["lon"]) for p in pts], dtype=float)   # (K,2) 世界像素
    seg = np.sqrt(((raw[1:] - raw[:-1]) ** 2).sum(1))
    cum = np.concatenate([[0], np.cumsum(seg)])
    total = cum[-1] or 1.0

    # 每個 raw 點屬於哪一天 → 每天分到相等的「取樣配額」
    keys = [_daykey(p) for p in pts]
    days: list[list[int]] = []
    for i, k in enumerate(keys):
        if not days or keys[days[-1][0]] != k:
            days.append([i])
        else:
            days[-1].append(i)

    per_day = max(4, N_SAMPLES // max(1, len(days)))
    samples = []          # (K,2) 重取樣後的世界像素座標
    day_bounds = []       # 每天在 samples 裡的 [起,迄)
    for grp in days:
        i0, i1 = grp[0], min(grp[-1] + 1, len(raw) - 1)
        if i1 <= i0:
            i1 = min(i0 + 1, len(raw) - 1)
        d0, d1 = cum[i0], cum[i1]
        start = len(samples)
        for t in np.linspace(d0, d1, per_day, endpoint=False):
            j = int(np.searchsorted(cum, t) - 1)
            j = max(0, min(j, len(raw) - 2))
            f = (t - cum[j]) / (seg[j] or 1.0)
            samples.append(raw[j] + f * (raw[j + 1] - raw[j]))
        day_bounds.append((start, len(samples)))
    samples.append(raw[-1])
    return np.array(samples), day_bounds, days, keys, raw


def _chapter_name(scene: str) -> str:
    """只取【】章節名（方案1 敘事片風格）；沒有就回空字串，讓 waypoints 退回 scene_name。"""
    b = _re.search(r"【(.+?)】", str(scene or ""))
    return b.group(1).strip() if b else ""


def waypoints(pts, keys, raw, samples):
    """每個章節一個點燃標籤：「Day N · 地名」。Day = 實際曆日序（不是章節數），地名用【】或清過的 scene_name。"""
    dates = sorted({p["taken"].date() for p in pts if p["taken"]})
    day_of = {d: i + 1 for i, d in enumerate(dates)}
    out = []
    for i, p in enumerate(pts):
        if i and keys[i] == keys[i - 1]:
            continue
        name = _chapter_name(p["scene"]) or _clean_place(p.get("scene_name") or p.get("place") or "")
        dn = day_of.get(p["taken"].date()) if p["taken"] else None
        label = " · ".join(x for x in (f"Day {dn}" if dn else "", name) if x)
        if not label:
            continue
        si = int(np.argmin(((samples - raw[i]) ** 2).sum(1)))
        if out and out[-1][1] == label:
            continue
        out.append((si, label))
    return sorted(out)


# ── 逐幀繪製 ────────────────────────────────────────────────────────────────
def render(base: Image.Image, samples, day_bounds, wps, cfg, frames_dir: Path):
    bw, bh = base.size
    fps, secs = cfg["fps"], cfg["seconds"]
    n_frames = int(fps * secs)
    hold = int(fps * 1.0)                       # 結尾整圈亮起定住
    pull = cfg["pull_frames"]                   # 拉遠phase 長度
    follow_end = n_frames - pull

    # head sample index per frame（每天等時：把每天的 sample 範圍鋪平到等長的 frame 段）
    fb_per_day = follow_end / max(1, len(day_bounds))
    head_at = np.zeros(n_frames, dtype=int)
    for di, (s0, s1) in enumerate(day_bounds):
        f0, f1 = int(di * fb_per_day), int((di + 1) * fb_per_day)
        for f in range(f0, min(f1, follow_end)):
            u = (f - f0) / max(1, f1 - f0)
            head_at[f] = int(s0 + u * (s1 - s0))
    head_at[follow_end:] = len(samples) - 1

    img_center = np.array([bw / 2, bh / 2])
    ignited: dict[int, int] = {}                # sample_idx → 起火的 frame
    font_lbl = _font(38)

    for f in range(n_frames + hold):
        ff = min(f, n_frames - 1)
        hi = head_at[ff]
        head = samples[hi]

        # 相機：跟拍 → 拉遠
        if ff < follow_end:
            center, zoom = head.copy(), FOLLOW_ZOOM
        else:
            u = (ff - follow_end) / max(1, pull)
            u = 1 - (1 - u) ** 3                # ease-out
            center = head + (img_center - head) * u
            zoom = FOLLOW_ZOOM + (1.0 - FOLLOW_ZOOM) * u
        if f >= n_frames:
            center, zoom = img_center, 1.0

        cw, chh = bw / zoom, bh / zoom
        x0 = float(np.clip(center[0] - cw / 2, 0, bw - cw))
        y0 = float(np.clip(center[1] - chh / 2, 0, bh - chh))

        def to_screen(pt):
            return ((pt[0] - x0) / cw * OUT_W, (pt[1] - y0) / chh * OUT_H)

        frame = base.crop((round(x0), round(y0), round(x0 + cw), round(y0 + chh))) \
            .resize((OUT_W, OUT_H), Image.BILINEAR)
        d = ImageDraw.Draw(frame, "RGBA")
        glow = Image.new("RGBA", (OUT_W // 2, OUT_H // 2), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow, "RGBA")
        rnd = random.Random(f)

        # 尾巴：餘燼灰，離火頭越遠越暗會淡（floor 留鬼線）
        scr = [to_screen(samples[k]) for k in range(hi + 1)]
        step = 1 if len(scr) < 600 else 2
        for k in range(step, hi + 1, step):
            age = hi - k
            if age < 22:
                col, wdt = (255, 190, 70), 7
            elif age < 110:
                t = (age - 22) / 88
                col = (int(255 - 105 * t), int(150 - 70 * t), int(60 - 5 * t))
                wdt = 6 - int(2 * t)
            else:
                col, wdt = (150, 85, 62), 4                    # 燃完的餘燼（暖灰，不變純灰）
            a = max(100, int(235 * math.exp(-age / 900)))      # 會淡但整條路仍看得見
            if age >= 110:
                a = int(a * (0.86 + 0.14 * rnd.random()))      # 餘燼微閃
            d.line([scr[k - step], scr[k]], fill=col + (a,), width=max(3, wdt), joint="curve")

        # 點燃事件
        for si, label in wps:
            if si <= hi and si not in ignited:
                ignited[si] = f
            if si in ignited:
                age = (f - ignited[si]) / fps
                sx, sy = to_screen(samples[si])
                if age < 0.55:                                  # 擴散光環
                    r = 4 + age / 0.55 * 60
                    aa = int(255 * (1 - age / 0.55))
                    d.ellipse([sx - r, sy - r, sx + r, sy + r],
                              outline=(255, 205, 90, aa), width=max(1, int(4 * (1 - age / 0.55))))
                # 常亮 pin
                d.ellipse([sx - 5, sy - 5, sx + 5, sy + 5], fill=(255, 150, 60, 255))
                gd.ellipse([sx / 2 - 10, sy / 2 - 10, sx / 2 + 10, sy / 2 + 10],
                           fill=(255, 140, 50, 120))
                # 標籤淡入保留
                la = int(min(1.0, age / 0.3) * 235)
                if la > 0 and f < n_frames:
                    tx, ty = sx + 14, sy - 46
                    tw = d.textbbox((0, 0), label, font=font_lbl)[2]
                    tx = min(tx, OUT_W - tw - 20)
                    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                        d.text((tx + dx, ty + dy), label, font=font_lbl, fill=(0, 0, 0, la))
                    d.text((tx, ty), label, font=font_lbl, fill=(255, 245, 235, la))

        # 火頭（跟拍時才畫；拉遠後火線已到終點）
        if f < n_frames:
            hx, hy = to_screen(head)
            for r, col in ((34, (255, 120, 40, 55)), (18, (255, 170, 60, 110)),
                           (9, (255, 230, 150, 200)), (4, (255, 255, 245, 255))):
                d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=col)
            gd.ellipse([hx / 2 - 26, hy / 2 - 26, hx / 2 + 26, hy / 2 + 26],
                       fill=(255, 140, 45, 150))
            for _ in range(rnd.randint(3, 6)):                  # 火花
                a = rnd.uniform(0, 6.28)
                dist = rnd.uniform(6, 26)
                ex, ey = hx + math.cos(a) * dist, hy + math.sin(a) * dist
                d.line([(hx, hy), (ex, ey)], fill=(255, 220, 140, rnd.randint(90, 200)), width=1)

        frame = Image.alpha_composite(
            frame.convert("RGBA"),
            glow.resize((OUT_W, OUT_H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9)))

        # 片名：拉遠完成後淡入
        if cfg["title"] and f >= n_frames - int(fps * 1.6):
            ta = int(min(1.0, (f - (n_frames - fps * 1.6)) / (fps * 1.2)) * 255)
            dd = ImageDraw.Draw(frame, "RGBA")
            title = cfg["title"]
            sz = 150
            while sz > 40 and dd.textbbox((0, 0), title, font=_font(sz))[2] > OUT_W * 0.8:
                sz -= 6
            ft = _font(sz)
            tw = dd.textbbox((0, 0), title, font=ft)[2]
            cx, cy = (OUT_W - tw) // 2, OUT_H // 2 - sz // 2
            for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
                dd.text((cx + dx, cy + dy), title, font=ft, fill=(0, 0, 0, ta))
            dd.text((cx, cy), title, font=ft, fill=(248, 248, 248, ta))

        frame.convert("RGB").save(frames_dir / f"f{f:05d}.png")
    return n_frames + hold


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard", default=None)
    ap.add_argument("--meta", default=str(META))
    ap.add_argument("--title", default="", help="拉遠後淡入的片名（空=不加，給 build_bookends 傳）")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default=str(OUT_DIR / "route_burn.mp4"))
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("找不到 ffmpeg")

    pts = load_track(Path(args.storyboard) if args.storyboard else None, Path(args.meta))
    if len(pts) < 3:
        sys.exit("GPS 點太少，先跑 scripts/probe_folder_metadata.py --geocode")

    bbox = pad_bbox([(p["lat"], p["lon"]) for p in pts])
    bw, bh = OUT_W * OVERSAMPLE, OUT_H * OVERSAMPLE
    print(f"抓衛星底圖 {bw}×{bh} …", flush=True)
    base = fetch(bbox, bw, bh)
    base = Image.blend(base, Image.new("RGB", base.size, (8, 10, 14)), 0.42)   # 壓暗給火線對比

    samples, day_bounds, days, keys, raw = build_geometry(pts, bbox, bw, bh)
    wps = waypoints(pts, keys, raw, samples)
    print(f"{len(pts)} GPS 點｜{len(days)} 天｜{len(wps)} 個點燃標籤：" +
          "、".join(l for _, l in wps), flush=True)

    cfg = {"fps": args.fps, "seconds": args.seconds, "title": args.title,
           "pull_frames": int(args.fps * 1.8)}
    with tempfile.TemporaryDirectory() as td:
        fd = Path(td)
        t0 = time.time()
        total = render(base, samples, day_bounds, wps, cfg, fd)
        print(f"  {total} 幀 / {time.time() - t0:.0f}s，編碼中 …", flush=True)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(args.fps), "-i", str(fd / "f%05d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-movflags", "+faststart", args.out,
        ], check=True, capture_output=True)
    print(f"✅ {args.out}（{total / args.fps:.1f}s）")


if __name__ == "__main__":
    main()
