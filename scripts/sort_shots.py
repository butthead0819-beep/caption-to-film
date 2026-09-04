#!/usr/bin/env python3
"""章節內按拍攝時間重排 storyboard（修「有些素材時間順序放錯」）。

保留章節（scene_title【…】或 scene_id）的順序與大結構，只把**每一章內部**的鏡頭
依 `taken` 時間 stable-sort。沒有拍攝時間的鏡頭（外部下載 / Live Photo 定格半）
會跟著它前面那顆有時間的鏡頭一起走，不會被丟掉。

用法：
  .venv/bin/python scripts/sort_shots.py                 # 預覽會怎麼動
  .venv/bin/python scripts/sort_shots.py --write         # 套用（先備份 .json）
  .venv/bin/python scripts/sort_shots.py --by scene_id   # 用 segment_scenes 的章節而非 scene_title
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import PREFIX  # noqa: E402
from backend.util.photos_meta import load_photos_meta, meta_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _key(shot: dict, by: str) -> object:
    if by == "scene_id" and shot.get("scene_id") is not None:
        return shot["scene_id"]
    t = shot.get("scene_title", "") or ""
    m = re.search(r"[【\[]([^】\]]+)[】\]]", t)
    return m.group(1).strip() if m else t.split("：")[0].strip()


def _taken(shot: dict, meta: dict):
    v = meta_for(shot, meta).get("taken")
    if not v:
        return None
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(v[:19], f)
        except ValueError:
            continue
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--by", choices=["scene_title", "scene_id"], default="scene_title")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    src = ROOT / f"{args.prefix}.json"
    data = json.loads(src.read_text("utf-8"))
    sb: list[dict] = data["storyboard"]
    meta = load_photos_meta()

    # 分章（連續同 key）
    chapters: list[list[dict]] = []
    prev = object()
    for s in sb:
        k = _key(s, args.by)
        if k != prev:
            chapters.append([])
            prev = k
        chapters[-1].append(s)

    new_sb: list[dict] = []
    moved_total = 0
    for ch in chapters:
        # 沒時間的鏡頭 → 借「前一顆有時間的」時間 + 1 秒（維持在它後面）；開頭沒時間的擺最前
        eff: list[tuple] = []
        carry = None
        for i, s in enumerate(ch):
            t = _taken(s, meta)
            if t:
                carry = t
            eff.append(((carry, i) if carry else (datetime.min, i), s))
        ordered = [s for _, s in sorted(eff, key=lambda x: x[0])]
        moved_total += sum(1 for a, b in zip(ch, ordered) if a is not b)
        # 印出這一章的異動
        if [id(x) for x in ch] != [id(x) for x in ordered]:
            cname = _key(ch[0], args.by)
            print(f"\n【{cname}】{len(ch)} 鏡 重排：")
            for s in ordered:
                t = _taken(s, meta)
                print(f"   {s.get('shot_index'):>4}  {s.get('media_file','?'):<28} "
                      f"{t.strftime('%m-%d %H:%M') if t else '(無時間，跟前一顆)'}")
        new_sb.extend(ordered)

    print(f"\n共 {moved_total} 顆鏡頭位置有變（{len(chapters)} 章）")
    if not args.write:
        print("（預覽。加 --write 套用）")
        return

    bak = src.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
    bak.write_text(src.read_text("utf-8"), "utf-8")
    data["storyboard"] = new_sb
    src.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"✅ 已寫回 {src.name}（備份 {bak.name}）→ 接著跑 rebuild_all_projects.py")


if __name__ == "__main__":
    main()
