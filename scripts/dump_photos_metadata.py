#!/usr/bin/env python3
"""把 Apple Photos 相簿的可剪輯 metadata 匯出成快取檔。

PhotosDB() 開一次很慢（大相簿要幾分鐘），所以不放進 rebuild pipeline，
改由這支腳本產生 `scripts/photos_meta.json`，highlight_engine / abroll_engine /
effects_engine 有就讀、沒有就略過。

每筆 (以原始檔名 stem 當鍵，大小寫無關)：
  favorite      相簿愛心
  keywords      使用者關鍵字
  title/descr   Photos App 說明欄 (比 EXIF ImageDescription 可靠)
  persons       入鏡人物名字 list
  faces         人臉框 [{name, x, y, w, h}]  (正規化 0..1，中心點+寬高) — 給 smart crop
  score         Apple ML 綜合分 (overall，約 -1..1)
  score_detail  {curation, promotion, highlight_visibility, behavioral, ...} 子分數
  labels        Apple ML 場景標籤 (bicycle / sunset / food / mountain ...)
  activities    Apple ML 活動 (Cycling / Hiking ...)
  place         {name, city, area, country}  逆地理編碼
  gps           {lat, lon, alt}
  taken         拍攝時間 ISO
  kind          {panorama, screenshot, slow_mo, time_lapse, hdr, burst, live}
  edited        使用者有沒有修過圖 (hasadjustments)
  exclude       hidden / intrash → 建議排除

用法:
  .venv/bin/python scripts/dump_photos_metadata.py
  .venv/bin/python scripts/dump_photos_metadata.py --album "my_trip"
"""

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "photos_meta.json"

# score 子分數：值越高越「精華」的那幾個
_SCORE_KEYS = (
    "overall", "curation", "promotion", "highlight_visibility", "behavioral",
    "interesting_subject", "well_timed_shot", "well_chosen_subject",
    "pleasant_composition", "pleasant_lighting", "sharply_focused_subject",
)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _faces(p):
    out = []
    for fi in (getattr(p, "face_info", None) or []):
        try:
            # osxphotos 提供正規化中心座標與大小 (相對於原圖，已含旋轉補正)
            cx = getattr(fi, "center_x", None)
            cy = getattr(fi, "center_y", None)
            w = getattr(fi, "size", None)
            if cx is None or cy is None:
                continue
            out.append({
                "name": getattr(fi, "name", None) or "",
                "x": round(float(cx), 4),
                "y": round(float(cy), 4),
                "w": round(float(w), 4) if w is not None else None,
            })
        except Exception:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--album", default=None, help="只匯出指定相簿（預設全庫）")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    try:
        import osxphotos
    except ImportError:
        print("需要 osxphotos（.venv/bin/pip install osxphotos）", file=sys.stderr)
        sys.exit(1)

    print("開啟 Apple Photos 資料庫中（大相簿可能要數分鐘）...", flush=True)
    db = osxphotos.PhotosDB()
    photos = db.photos(albums=[args.album]) if args.album else db.photos()
    print(f"讀到 {len(photos)} 張，擷取 metadata...", flush=True)

    meta: dict = {}
    for p in photos:
        s = p.score
        score_detail = {k: _num(getattr(s, k, None)) for k in _SCORE_KEYS if s} if s else {}
        score_detail = {k: v for k, v in score_detail.items() if v is not None}

        si = getattr(p, "search_info", None)
        labels = sorted(set((getattr(si, "labels", None) or []) + (p.labels or []))) if si else list(p.labels or [])
        activities = list(getattr(si, "activities", None) or []) if si else []

        place = None
        pl = getattr(p, "place", None)
        if pl:
            place = {
                "name": getattr(pl, "name", None),
                "city": (pl.address.city if getattr(pl, "address", None) else None),
                "country": (pl.address.country if getattr(pl, "address", None) else None),
            }

        loc = p.location if p.location and p.location[0] is not None else (None, None)
        gps = None
        if loc[0] is not None:
            gps = {"lat": round(float(loc[0]), 6), "lon": round(float(loc[1]), 6),
                   "alt": round(float(p.altitude), 1) if p.altitude is not None else None}

        rec = {
            "favorite": bool(p.favorite),
            "keywords": list(p.keywords or []),
            "title": p.title or "",
            "descr": p.description or "",
            "persons": [x for x in (p.persons or []) if x and x != "_UNKNOWN_"],
            "faces": _faces(p),
            "score": _num(getattr(s, "overall", None)) if s else None,
            "score_detail": score_detail,
            "labels": labels,
            "activities": activities,
            "place": place,
            "gps": gps,
            "taken": p.date.isoformat() if p.date else None,
            "kind": {
                "panorama": bool(getattr(p, "panorama", False)),
                "screenshot": bool(getattr(p, "screenshot", False)),
                "slow_mo": bool(getattr(p, "slow_mo", False)),
                "time_lapse": bool(getattr(p, "time_lapse", False)),
                "hdr": bool(getattr(p, "hdr", False)),
                "burst": bool(getattr(p, "burst", False)),
                "live": bool(getattr(p, "live_photo", False)),
            },
            "edited": bool(getattr(p, "hasadjustments", False)),
            "exclude": bool(getattr(p, "hidden", False) or getattr(p, "intrash", False)),
        }
        for name in filter(None, (p.original_filename, p.filename)):
            meta[Path(name).stem.lower()] = rec

    Path(args.out).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def cnt(pred):
        return sum(1 for v in meta.values() if pred(v))
    print(
        f"✅ 寫入 {args.out}｜{len(meta)} 筆\n"
        f"   favorite {cnt(lambda v: v['favorite'])}｜"
        f"有人物 {cnt(lambda v: v['persons'])}｜"
        f"有人臉框 {cnt(lambda v: v['faces'])}｜"
        f"有 Apple 分數 {cnt(lambda v: v['score'] is not None)}｜"
        f"有 ML 標籤 {cnt(lambda v: v['labels'])}｜"
        f"有 GPS {cnt(lambda v: v['gps'])}｜"
        f"連拍 {cnt(lambda v: v['kind']['burst'])}｜"
        f"全景 {cnt(lambda v: v['kind']['panorama'])}"
    )


if __name__ == "__main__":
    main()
