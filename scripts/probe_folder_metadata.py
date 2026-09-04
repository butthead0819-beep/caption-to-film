#!/usr/bin/env python3
"""掃描「素材資料夾」內的檔案，讀內嵌 metadata → scripts/photos_meta.json。

不開 Apple Photos 資料庫（快）。拿得到 GPS / 拍攝時間 / 人臉框 / 關鍵字。
Apple ML 場景標籤 + 美學分數要另外用 scripts/dump_photos_metadata.py 補（會 merge）。

用法:
  .venv/bin/python scripts/probe_folder_metadata.py
  .venv/bin/python scripts/probe_folder_metadata.py -i /path/to/folder --merge
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.folder_metadata import probe_folder

from scripts._config import MEDIA_DIR  # noqa: E402
DEFAULT_FOLDER = str(MEDIA_DIR)
OUT = Path(__file__).resolve().parent / "photos_meta.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", default=DEFAULT_FOLDER)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--merge", action="store_true",
                    help="與現有 photos_meta.json 合併（保留對方已有的 labels/score 等）")
    ap.add_argument("--geocode", action="store_true",
                    help="用 GPS 查地名填 place（Google Places / Overpass / Nominatim，有磁碟快取）")
    args = ap.parse_args()

    meta = probe_folder(args.input)

    if args.geocode:
        from backend.util.poi import area_name
        done = 0
        for rec in meta.values():
            g = rec.get("gps")
            if g and not rec.get("place"):
                nm = area_name(g["lat"], g["lon"])   # 行政區級，穩定；不用路邊 POI
                if nm:
                    rec["place"] = {"name": nm}
                    done += 1
        print(f"   geocode 填了 {done} 個地名（行政區級）")

    out_path = Path(args.out)
    if args.merge and out_path.exists():
        old = json.loads(out_path.read_text(encoding="utf-8"))
        for k, v in meta.items():
            if k in old:
                # 只補空欄位，不覆蓋對方（例如 dump 來的 labels / score）
                for fk, fv in v.items():
                    if not old[k].get(fk) and fv:
                        old[k][fk] = fv
            else:
                old[k] = v
        meta = old

    out_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def cnt(pred):
        return sum(1 for v in meta.values() if pred(v))
    print(
        f"✅ {out_path}｜{len(meta)} 筆\n"
        f"   GPS {cnt(lambda v: v.get('gps'))}｜"
        f"拍攝時間 {cnt(lambda v: v.get('taken'))}｜"
        f"人臉框 {cnt(lambda v: v.get('faces'))}｜"
        f"人物 {cnt(lambda v: v.get('persons'))}｜"
        f"關鍵字 {cnt(lambda v: v.get('keywords'))}｜"
        f"全景 {cnt(lambda v: v.get('kind', {}).get('panorama'))}｜"
        f"ML標籤 {cnt(lambda v: v.get('labels'))} (需 dump 補)"
    )


if __name__ == "__main__":
    main()
