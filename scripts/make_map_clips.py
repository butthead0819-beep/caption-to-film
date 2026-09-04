#!/usr/bin/env python3
"""GPS → 衛星地圖 establishing 片段（等級 1：衛星靜圖 + Ken Burns zoom-in）。

見 `.claude/skills/film-edit/references/map-clips.md`。

讀 scripts/photos_meta.json 的 GPS → 依地點/距離/時間分「段(leg)」→ 每段抓一張 Esri
World Imagery 衛星圖、畫上路線與 pin → 存到 map_clips/，並輸出 manifest.json（每張建議
插在哪個 scene_title 前面 + zoom-in 的 ken_burns 參數）。另外做一張全路線總覽 + 海拔剖面。

用法:
  .venv/bin/python scripts/make_map_clips.py
  .venv/bin/python scripts/make_map_clips.py --storyboard my_trip.json
"""

import argparse
import io
import json
import math
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.poi import area_name
from backend.util.aerial import aerial_video
import re as _re


def _scene_caption(scene: str, bracket: bool = False) -> str:
    """「Day 4【某章名】：某地名」→ bracket=False→「某地名」；bracket=True→「某章名」。"""
    s = str(scene or "")
    b = _re.search(r"【(.+?)】", s)
    if bracket and b:
        return b.group(1).strip()
    m = _re.search(r"[:：]\s*(.+)$", s)
    if m:
        return _re.split(r"[（(]", m.group(1))[0].strip()
    return b.group(1).strip() if b else ""

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "scripts" / "photos_meta.json"
OUT_DIR = ROOT / "map_clips"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"

W, H = 1920, 1080
LEG_KM = 12.0         # 離上一段中心超過這距離 → 新的一段 (--leg-km 可調)
LEG_GAP_HOURS = 5.0   # 時間間隔超過這麼久 → 新的一段


def haversine(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def project(lat, lon, bbox):
    """經緯度 → 影像內正規化 (x, y)，bbox=(w,s,e,n)。"""
    w, s, e, n = bbox
    x = (lon - w) / (e - w)
    y = (merc_y(n) - merc_y(lat)) / (merc_y(n) - merc_y(s))
    return x, y


def fetch(bbox, tries=4):
    w, s, e, n = bbox
    params = {"bbox": f"{w},{s},{e},{n}", "bboxSR": 4326, "imageSR": 3857,
              "size": f"{W},{H}", "format": "jpg", "f": "image"}
    last = None
    for k in range(tries):
        try:
            r = requests.get(ESRI, params=params, timeout=45)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as ex:
            last = ex
            time.sleep(2 * (k + 1))
    raise last


def pad_bbox(pts, pad_ratio=0.35, min_span=0.02):
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    s, n, w, e = min(lats), max(lats), min(lons), max(lons)
    dy, dx = max(n - s, min_span), max(e - w, min_span)
    # 補到 16:9
    if dx / dy < W / H:
        dx = dy * W / H
    else:
        dy = dx * H / W
    cy, cx = (s + n) / 2, (w + e) / 2
    return (cx - dx * (0.5 + pad_ratio), cy - dy * (0.5 + pad_ratio),
            cx + dx * (0.5 + pad_ratio), cy + dy * (0.5 + pad_ratio))


def draw_route(img, pts, bbox, pin=None):
    d = ImageDraw.Draw(img, "RGBA")
    xy = [(x * W, y * H) for x, y in (project(la, lo, bbox) for la, lo, *_ in pts)]
    if len(xy) >= 2:
        d.line(xy, fill=(255, 80, 60, 235), width=6, joint="curve")
    for p in xy:
        d.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], fill=(255, 255, 255, 200))
    if pin:
        px, py = project(pin[0], pin[1], bbox)
        cx, cy = px * W, py * H
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], outline=(255, 255, 255, 255), width=4)
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(255, 80, 60, 255))
    return img


def zoom_kb(px, py, end_scale=2.6):
    return {
        "type": "zoom",
        "start": {"scale": 1.0, "x": 0.0, "y": 0.0},
        "end": {"scale": end_scale,
                "x": round(-(px - 0.5) * end_scale, 4),
                "y": round((py - 0.5) * end_scale, 4)},
        "fit_mode": False,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard", default=None, help="用它的順序與 scene_title")
    ap.add_argument("--meta", default=str(META))
    ap.add_argument("--leg-km", type=float, default=LEG_KM, help="分段距離門檻 km（大=段少）")
    ap.add_argument("--per-day", action="store_true",
                    help="一天一張地圖（依 scene_title 的 Day N / 第X篇 分），約 8 張")
    ap.add_argument("--aerial", action="store_true",
                    help="有地名的段先試 Google Aerial View 空拍影片（拿不到才退回衛星靜圖）")
    ap.add_argument("--aerial-wait", type=int, default=0,
                    help="Aerial View 沒現成影片時，觸發 render 並輪詢最多幾秒（0=不等）")
    args = ap.parse_args()

    meta = json.loads(Path(args.meta).read_text("utf-8")) if Path(args.meta).exists() else {}
    meta = {k.lower(): v for k, v in meta.items()}

    order = []
    if args.storyboard and Path(args.storyboard).exists():
        for shot in json.loads(Path(args.storyboard).read_text("utf-8")).get("storyboard", []):
            stem = Path(str(shot.get("file_path") or shot.get("media_file") or "")).stem.lower()
            order.append((stem, shot.get("scene_title", "")))
    else:
        order = [(k, "") for k in meta]

    pts = []
    for stem, scene in order:
        g = (meta.get(stem) or {}).get("gps")
        if not g:
            continue
        pl = (meta.get(stem) or {}).get("place") or {}
        pts.append({"stem": stem, "scene": scene, "lat": g["lat"], "lon": g["lon"],
                    "alt": g.get("alt"), "place": pl.get("name"),
                    "taken": (meta.get(stem) or {}).get("taken")})
    if len(pts) < 2:
        print("GPS 點太少，先跑 scripts/probe_folder_metadata.py")
        return

    OUT_DIR.mkdir(exist_ok=True)

    # 分段（記錄每段結束在 pts 的 index）
    legs, cur = [], [0]
    day_re = _re.compile(r"(Day\s*\d+|第[一二三四五六七八九十\d]+篇|序章|尾聲)")

    def daykey(p):
        m = day_re.search(str(p["scene"]))
        return m.group(1).replace(" ", "") if m else None

    for j in range(1, len(pts)):
        c, p = pts[cur[-1]], pts[j]
        if args.per_day:
            split = daykey(p) and daykey(p) != daykey(pts[cur[0]])
        else:
            far = haversine((c["lat"], c["lon"]), (p["lat"], p["lon"])) > args.leg_km
            newplace = p["place"] and c["place"] and p["place"] != c["place"]
            gap = (p["taken"] and c["taken"]
                   and abs(_dt(p["taken"]) - _dt(c["taken"])) > LEG_GAP_HOURS * 3600)
            split = far or newplace or gap
        if split:
            legs.append(cur)
            cur = [j]
        else:
            cur.append(j)
    legs.append(cur)
    print(f"共分 {len(legs)} 段，開始抓衛星圖...", flush=True)

    manifest = []
    allpts = [(p["lat"], p["lon"]) for p in pts]

    for i, leg_idx in enumerate(legs, 1):
      try:
        leg = [pts[j] for j in leg_idx]
        lpts = [(p["lat"], p["lon"]) for p in leg]
        bbox = pad_bbox(lpts if len(lpts) > 1 else lpts * 2)
        img = fetch(bbox)
        centroid = (sum(a for a, _ in lpts) / len(lpts), sum(b for _, b in lpts) / len(lpts))
        sofar = allpts[:leg_idx[-1] + 1]           # 到目前為止的整條路線
        draw_route(img, sofar, bbox, pin=centroid)
        # caption：分鏡章節名最可靠（人寫的）→ Apple place → 行政區地名
        place = (_scene_caption(leg[0]["scene"], bracket=args.per_day) or leg[0]["place"]
                 or area_name(*centroid) or "")
        safe = (place or f"leg{i:02d}").replace("/", "-").replace(" ", "")
        px, py = project(*centroid, bbox)

        # 先試 Aerial View 空拍影片
        aerial = None
        if args.aerial and place:
            aerial = aerial_video(f"{place}, Taiwan", str(OUT_DIR / f"aerial_{i:02d}_{safe}.mp4"),
                                  poll_seconds=args.aerial_wait)
        if aerial:
            print(f"  [{i}/{len(legs)}] {place}  ✈️ Aerial View", flush=True)
            manifest.append({
                "video": aerial, "kind": "aerial", "insert_before_scene": leg[0]["scene"],
                "place": place, "caption": place,
            })
            continue

        fn = OUT_DIR / f"leg_{i:02d}_{safe}.jpg"
        img.save(fn, quality=90)
        print(f"  [{i}/{len(legs)}] {place or safe}", flush=True)
        manifest.append({
            "image": str(fn), "kind": "leg", "insert_before_scene": leg[0]["scene"],
            "place": place, "duration_seconds": 3.5,
            "ken_burns": zoom_kb(px, py),
            "caption": place,
        })
      except Exception as ex:
        print(f"  [{i}/{len(legs)}] 跳過（{type(ex).__name__}）", flush=True)

    # 全路線總覽
    bbox = pad_bbox(allpts, pad_ratio=0.15)
    ov = draw_route(fetch(bbox), [(a, b) for a, b in allpts], bbox)
    ov_fn = OUT_DIR / "route_overview.jpg"
    ov.save(ov_fn, quality=90)
    manifest.insert(0, {"image": str(ov_fn), "kind": "overview", "insert_before_scene": "",
                        "duration_seconds": 4.0,
                        "ken_burns": {"type": "zoom",
                                      "start": {"scale": 1.0, "x": 0.0, "y": 0.0},
                                      "end": {"scale": 1.18, "x": 0.0, "y": 0.0}, "fit_mode": False},
                        "caption": "路線總覽"})

    # 海拔剖面 (角落疊圖用，透明底)
    alts = [(j, p["alt"]) for j, p in enumerate(pts) if p["alt"] is not None]
    if len(alts) > 3:
        ep = Image.new("RGBA", (900, 260), (0, 0, 0, 0))
        d = ImageDraw.Draw(ep)
        xs = [a[0] for a in alts]
        ys = [a[1] for a in alts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        pxs = [(20 + (x - x0) / max(1, x1 - x0) * 860,
                240 - (y - y0) / max(1, y1 - y0) * 210) for x, y in alts]
        d.line([(20, 245), (880, 245)], fill=(255, 255, 255, 120), width=2)
        d.line(pxs, fill=(255, 200, 60, 255), width=4)
        d.polygon(pxs + [(pxs[-1][0], 245), (pxs[0][0], 245)], fill=(255, 200, 60, 60))
        d.text((20, 6), f"海拔 {int(y0)}–{int(y1)} m", fill=(255, 255, 255, 230))
        ep.save(OUT_DIR / "elevation.png")

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    print(f"✅ {len(legs)} 段地圖 + 總覽 + 海拔剖面 → {OUT_DIR}/")
    print(f"   manifest.json 列出每張建議插入位置與 ken_burns；拖進 Resolve 或之後接 --with-maps")


def _dt(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


if __name__ == "__main__":
    main()
