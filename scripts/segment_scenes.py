#!/usr/bin/env python3
"""階段 1：把 storyboard 切成「小章節（scene）」，機械訊號提案 + 人可微調。

章節是旁白預算（regenerate_voiceover）、審閱包分組、地圖片段插入點的依據。
純靠 Gemini 寫的 scene_title 在敘事片還行，蒙太奇片會碎成 73 段 → 這支用
photos_meta 的實際訊號重切：

  階層： Day（拍攝時間間隔）
          └ 地點（GPS 群集，用 place.name / area_name 命名）
               └ [選用 --by-label] 主題標籤大幅改變再細分

寫入每個 shot：`scene_id`(int, 連續) + `scene_name`(str)；`--rewrite-title` 另把
scene_title 改成 `Day{d}【{name}】`。

輸出預設到 `<prefix>_scenes.json`（副本，不動正本）；`--in-place` 才寫正本 .json。

用法：
  .venv/bin/python scripts/segment_scenes.py my_trip            # 預覽
  .venv/bin/python scripts/segment_scenes.py <prefix> --write                        # 寫副本
  .venv/bin/python scripts/segment_scenes.py <prefix> --write --in-place --rewrite-title
  .venv/bin/python scripts/segment_scenes.py <prefix> --day-gap 5 --move-km 1.2 --by-label
  .venv/bin/python scripts/segment_scenes.py <prefix> --montage                       # 整支一章
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.photos_meta import load_photos_meta, meta_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def haversine_km(a: tuple, b: tuple) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _parse_dt(s: str | None):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def _place_name(rec: dict) -> str | None:
    p = rec.get("place")
    if isinstance(p, dict) and p.get("name"):
        return p["name"]
    return None


def segment(storyboard: list[dict], meta: dict, *,
            day_gap_h: float | None, by_gps: bool, move_km: float,
            by_label: bool, montage: bool, min_scene_sec: float,
            max_scene_sec: float = 60.0) -> list[dict]:
    """回傳 [{scene_id, day, name, shots:[shot,...]}]。

    預設：一個曆日一章（多日旅程片的自然章節單位）。
      --by-gps  → 一天內地點跳超過 move_km 再切
      --day-gap → 同一曆日內時間間隔超過 N 小時也切（預設不啟用）
      --by-label→ 主題標籤大幅改變也切
    最後把短於 min_scene_sec 的碎章往前併。
    """
    if montage:
        return [{"scene_id": 1, "day": 1, "name": "蒙太奇", "shots": list(storyboard)}]

    # 曆日 → 第幾天（依實際日期排名，不受 storyboard 亂序影響）
    all_dates = sorted({d.date() for d in
                        (_parse_dt(meta_for(s, meta).get("taken")) for s in storyboard) if d})
    day_rank = {d: i + 1 for i, d in enumerate(all_dates)}

    raw: list[dict] = []
    day = 0
    prev_dt = None
    prev_date = None
    prev_labels: set = set()
    anchor_gps = None
    cur: dict | None = None

    for shot in storyboard:
        rec = meta_for(shot, meta)
        dt = _parse_dt(rec.get("taken"))
        g = rec.get("gps") or {}
        gps = (g.get("lat"), g.get("lon")) if g.get("lat") is not None else None
        labels = set(rec.get("labels") or [])

        new_day = cur is None
        if dt:
            if prev_date and dt.date() != prev_date:
                new_day = True
            if day_gap_h and prev_dt and (dt - prev_dt).total_seconds() / 3600.0 >= day_gap_h:
                new_day = True

        moved = (by_gps and gps and anchor_gps
                 and haversine_km(anchor_gps, gps) >= move_km)
        label_shift = (by_label and prev_labels and labels
                       and _jaccard(prev_labels, labels) < 0.3)

        day = day_rank.get(dt.date(), day) if dt else (day or 1)
        if cur is None or new_day or moved or label_shift:
            cur = {"scene_id": len(raw) + 1, "day": day, "shots": [],
                   "_places": {}, "_labels": {}}
            raw.append(cur)
            anchor_gps = gps or anchor_gps

        cur["shots"].append(shot)
        nm = _place_name(rec)
        if nm:
            cur["_places"][nm] = cur["_places"].get(nm, 0) + 1
        for lb in labels:
            cur["_labels"][lb] = cur["_labels"].get(lb, 0) + 1
        if dt:
            prev_dt, prev_date = dt, dt.date()
        if labels:
            prev_labels = labels

    def _secs(sc: dict) -> float:
        return sum(float(s.get("duration_seconds", 0) or 0) for s in sc["shots"])

    def _absorb(dst: dict, src: dict) -> None:
        dst["shots"].extend(src["shots"])
        for k in ("_places", "_labels"):
            for kk, vv in src[k].items():
                dst[k][kk] = dst[k].get(kk, 0) + vv

    # 收斂到目標長度：反覆把「最短且 < min」的章併進較短的鄰居，
    # 直到每章都 >= min_scene_sec 或只剩 floor 章。允許跨日併（4 秒的「一天」不該獨立成章）。
    merged = [{"day": sc["day"], "shots": list(sc["shots"]),
               "_places": dict(sc["_places"]), "_labels": dict(sc["_labels"])}
              for sc in raw]
    floor = max(3, round(sum(_secs(s) for s in merged) / max(1, max_scene_sec)))
    while len(merged) > floor:
        cand = [i for i in range(len(merged)) if _secs(merged[i]) < min_scene_sec]
        if not cand:
            break
        i = min(cand, key=lambda k: _secs(merged[k]))
        left = _secs(merged[i - 1]) if i > 0 else float("inf")
        right = _secs(merged[i + 1]) if i < len(merged) - 1 else float("inf")
        j = i - 1 if left <= right else i + 1
        lo, hi = min(i, j), max(i, j)
        _absorb(merged[lo], merged[hi])
        del merged[hi]

    # 過長的章：只在「有內部訊號」時對切（日期跳 / place 換 / label 大變），否則放著（單一地點長場景 OK）
    out: list[dict] = []
    for sc in merged:
        if _secs(sc) <= max_scene_sec or len(sc["shots"]) < 4:
            out.append(sc)
            continue
        shots = sc["shots"]
        cut = None
        acc, half = 0.0, _secs(sc) / 2
        for k in range(1, len(shots)):
            acc += float(shots[k - 1].get("duration_seconds", 0) or 0)
            prev, rec = meta_for(shots[k - 1], meta), meta_for(shots[k], meta)
            dp, dr = _parse_dt(prev.get("taken")), _parse_dt(rec.get("taken"))
            day_diff = dp and dr and dp.date() != dr.date()
            place_diff = (_place_name(rec) and _place_name(prev)
                          and _place_name(rec) != _place_name(prev))
            if (day_diff or place_diff) and half * 0.5 <= acc <= half * 1.5:
                cut = k
                break
        if cut:
            a = {"day": sc["day"], "shots": shots[:cut], "_places": {}, "_labels": {}}
            b = {"day": sc["day"], "shots": shots[cut:], "_places": {}, "_labels": {}}
            for part in (a, b):
                for s in part["shots"]:
                    r = meta_for(s, meta)
                    nm = _place_name(r)
                    if nm:
                        part["_places"][nm] = part["_places"].get(nm, 0) + 1
                    for lb in (r.get("labels") or []):
                        part["_labels"][lb] = part["_labels"].get(lb, 0) + 1
            out.extend([a, b])
        else:
            out.append(sc)
    merged = out

    from backend.util.poi import area_name

    for i, sc in enumerate(merged, start=1):
        sc["scene_id"] = i
        # 命名：行政區(area_name, 穩) → 多數 place.name → 主要 label → Day N
        name = None
        g = next((meta_for(s, meta).get("gps") for s in sc["shots"]
                  if meta_for(s, meta).get("gps")), None)
        if g:
            try:
                name = area_name(g["lat"], g["lon"])
            except Exception:
                name = None
        if not name and sc["_places"]:
            name = max(sc["_places"], key=sc["_places"].get)
        if not name and sc["_labels"]:
            name = max(sc["_labels"], key=sc["_labels"].get)
        sc["name"] = name or f"Day{sc['day']} 片段"
        sc.pop("_places", None)
        sc.pop("_labels", None)
    return merged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?")
    ap.add_argument("--json", help="直接指定 storyboard json")
    ap.add_argument("--meta", default=str(ROOT / "scripts" / "photos_meta.json"))
    ap.add_argument("--day-gap", type=float, default=None,
                    help="同一曆日內時間間隔超過 N 小時也切（預設只用曆日）")
    ap.add_argument("--by-gps", action="store_true", help="一天內地點跳超過 --move-km 再切一刀")
    ap.add_argument("--move-km", type=float, default=3.0, help="--by-gps 時，幾公里算新地點（預設 3）")
    ap.add_argument("--by-label", action="store_true", help="主題標籤大幅改變也切一刀")
    ap.add_argument("--min-scene-sec", type=float, default=12.0,
                    help="短於此秒數的碎章併進較短的鄰居（可跨日），預設 12")
    ap.add_argument("--max-scene-sec", type=float, default=60.0,
                    help="長於此秒數且有內部訊號（換日/換地點）的章對切，預設 60")
    ap.add_argument("--montage", action="store_true", help="整支片子當一個 scene（蒙太奇）")
    ap.add_argument("--write", action="store_true", help="寫檔（否則只預覽）")
    ap.add_argument("--in-place", action="store_true", help="寫回正本 .json（預設寫 <prefix>_scenes.json 副本）")
    ap.add_argument("--rewrite-title", action="store_true", help="把 scene_title 也改成 Day{d}【name】")
    args = ap.parse_args()

    src = Path(args.json) if args.json else ROOT / f"{args.prefix}.json"
    if not src.exists():
        sys.exit(f"找不到 {src}")
    data = json.loads(src.read_text("utf-8"))
    storyboard = data.get("storyboard", [])
    meta = load_photos_meta(args.meta)
    if not meta:
        print(f"⚠️  {args.meta} 空的 → 只能靠既有 scene_title，建議先跑 probe_folder_metadata.py")

    montage = args.montage or (
        sum(1 for s in storyboard if "蒙太奇" in (s.get("scene_title") or "")) > len(storyboard) * 0.5
    )
    if montage and not args.montage:
        print("· 偵測到蒙太奇片（多數 scene_title 含「蒙太奇」）→ 整支當一章")

    # 非時序警告：storyboard 沒依拍攝時間排 → 曆日切法會亂跳
    if not montage:
        dts = [_parse_dt(meta_for(s, meta).get("taken")) for s in storyboard]
        seq = [d for d in dts if d]
        inv = sum(1 for a, b in zip(seq, seq[1:]) if b < a)
        if seq and inv > len(seq) * 0.15:
            print(f"⚠️  storyboard 非時序（{inv}/{len(seq)-1} 處時間回跳）→ 曆日章節會亂。"
                  f"敘事片建議直接用 Gemini 的 scene_title；或先用 rebuild --chrono 產時序版再切。")

    scenes = segment(storyboard, meta, day_gap_h=args.day_gap, by_gps=args.by_gps,
                     move_km=args.move_km, by_label=args.by_label, montage=montage,
                     min_scene_sec=args.min_scene_sec, max_scene_sec=args.max_scene_sec)

    print(f"\n{src.stem}：{len(storyboard)} 鏡頭 → {len(scenes)} 章")
    for sc in scenes:
        secs = sum(float(s.get("duration_seconds", 0) or 0) for s in sc["shots"])
        d = sc.get("day", "")
        print(f"  #{sc['scene_id']:2d}  Day{d}  {len(sc['shots']):3d} 鏡 / ~{secs:4.0f}s   {sc['name']}")

    for sc in scenes:
        title = f"Day{sc.get('day','')}【{sc['name']}】"
        for s in sc["shots"]:
            s["scene_id"] = sc["scene_id"]
            s["scene_name"] = sc["name"]
            if args.rewrite_title:
                s["scene_title"] = title

    if not args.write:
        print("\n（預覽，未寫檔。加 --write）")
        return

    out = src if args.in_place else src.with_name(src.stem + "_scenes.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n✅ 寫入 {out}"
          + ("（正本）" if args.in_place else "（副本，rebuild 用 --json 指它，或之後 --in-place）"))


if __name__ == "__main__":
    main()
