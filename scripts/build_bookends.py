#!/usr/bin/env python3
"""片頭 / 片尾：把它們變成獨立段落，自動挑素材、生片名卡/片尾卡，寫回正本 storyboard。

設計見 `.claude/skills/film-edit/references/bookends.md`。

流程位置：排序 / 精華篩選 → make_map_clips（要有 route_overview）→ **build_bookends** → --regen-vo

用法：
  .venv/bin/python scripts/build_bookends.py <prefix>            # dry-run：只印自動挑的鏡頭
  .venv/bin/python scripts/build_bookends.py <prefix> --write    # 寫回正本 .json（冪等、先備份）

冪等：本腳本產生的鏡頭都打 `_bookend_generated: true`，重跑時先全刪再重建；
借用的 body 鏡頭只是被拷貝成短版（body 原鏡頭不動），outro 是把 body 最後一顆 pop 出來。
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.engines.highlight_engine import score_storyboard  # noqa: E402
from backend.util.photos_meta import load_photos_meta, meta_for  # noqa: E402
from scripts._config import MEDIA_DIR, PREFIX, SEARCH_DIRS  # noqa: E402
from scripts.rebuild_all_projects import normalize_storyboard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BOOKENDS_DIR = ROOT / "bookends"
ROUTE_OVERVIEW = ROOT / "map_clips" / "route_overview.jpg"

_CJK_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]

_DEFAULT_CFG = {
    "film_title": "", "film_subtitle": "", "dedication": "", "sign_off": "攝於 2026",
    "open_count": 4, "recap_max": 8, "outro_sec": 8.0, "bloopers": True,
}


# ── 讀寫 ────────────────────────────────────────────────────────────────────
def load_script(prefix: str) -> tuple[dict, Path]:
    for cand in (ROOT / f"{prefix}.json", MEDIA_DIR / f"{prefix}.json"):
        if cand.exists():
            return json.loads(cand.read_text("utf-8")), cand
    sys.exit(f"找不到 {prefix}.json")


def _stem(shot: dict) -> str:
    return Path(str(shot.get("file_path") or shot.get("media_file") or "")).stem.lower()


def _taken(rec: dict) -> _dt.datetime | None:
    t = rec.get("taken")
    if not t:
        return None
    try:
        return _dt.datetime.fromisoformat(str(t).replace("Z", ""))
    except ValueError:
        return None


def haversine_km(a: tuple, b: tuple) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ── 選片準則（專案無關） ────────────────────────────────────────────────────
def _is_wide(shot: dict) -> bool:
    return any(k in str(shot.get("shot_type") or "") for k in ("全景", "遠景", "Wide", "wide"))


def _labels(rec: dict) -> set:
    return {str(x).lower() for x in (rec.get("labels") or [])}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def pick_cold_open(body: list[dict], meta: dict, n: int, used: set) -> list[int]:
    """片頭風景蒙太奇：地理 + 場景類型分散的貪婪挑選。回傳 body 索引。"""
    trip = sorted((t for t in (_taken(meta_for(s, meta)) for s in body) if t))
    early_cut = trip[len(trip) // 3] if trip else None

    cand: list[tuple[float, int]] = []
    for i, s in enumerate(body):
        if i in used or s.get("_hl", {}).get("exclude") or s.get("_hl", {}).get("shake", 0) <= -1.0:
            continue
        if "[Live 微動態]" in (s.get("visual_action") or ""):
            continue
        rec = meta_for(s, meta)
        score = float(s.get("highlight_score") or 0)
        if _is_wide(s):
            score += 0.8
        if rec.get("has_people") is False:
            score += 0.6
        if rec.get("favorite"):
            score += 0.5
        tk = _taken(rec)
        if early_cut and tk and tk <= early_cut:
            score += 0.3
        cand.append((score, i))

    cand.sort(reverse=True)
    chosen: list[int] = []
    chosen_places: list = []
    chosen_labels: set = set()
    pool = cand[:]
    while pool and len(chosen) < n:
        best_j, best_s = None, -1e9
        for k, (base, i) in enumerate(pool):
            rec = meta_for(body[i], meta)
            pen = 0.0
            gps = rec.get("gps")
            place = (rec.get("place") or {}).get("name")
            for cp, cg in chosen_places:
                if place and cp == place:
                    pen -= 1.2
                elif gps and cg and haversine_km((gps["lat"], gps["lon"]), cg) < 20:
                    pen -= 1.2
            if _jaccard(_labels(rec), chosen_labels) > 0.3:
                pen -= 0.8
            if base + pen > best_s:
                best_s, best_j = base + pen, k
        _, i = pool.pop(best_j)
        rec = meta_for(body[i], meta)
        chosen.append(i)
        gps = rec.get("gps")
        chosen_places.append(((rec.get("place") or {}).get("name"),
                              (gps["lat"], gps["lon"]) if gps else None))
        chosen_labels |= _labels(rec)

    chosen.sort(key=lambda i: _taken(meta_for(body[i], meta)) or _dt.datetime.min)
    return chosen


def pick_recap(body: list[dict], meta: dict, max_n: int, used: set) -> list[int]:
    """回顧蒙太奇：每個曆日取 highlight_score 最高的一顆。"""
    by_day: dict[Any, list[int]] = {}
    for i, s in enumerate(body):
        if i in used or s.get("_hl", {}).get("exclude"):
            continue
        rec = meta_for(s, meta)
        tk = _taken(rec)
        key = tk.date() if tk else s.get("scene_id", i)
        by_day.setdefault(key, []).append(i)
    best = [max(idxs, key=lambda k: float(body[k].get("highlight_score") or 0))
            for _, idxs in sorted(by_day.items(), key=lambda kv: str(kv[0]))]
    return best[:max_n]


def pick_return(body: list[dict], meta: dict, used: set) -> int | None:
    for i, s in enumerate(body):
        if any(k in {str(x).lower() for x in (meta_for(s, meta).get("keywords") or [])}
               for k in ("返家", "home", "回家")):
            return i
    # 啟發式：最後一個曆日的最後一顆影片
    vids = [i for i, s in enumerate(body)
            if i not in used and s.get("media_type") in ("video", "live_photo")]
    return vids[-1] if vids else None


def pick_bloopers(body: list[dict], meta: dict, n: int, used: set) -> list[int]:
    tagged = [i for i, s in enumerate(body)
              if any(k in {str(x).lower() for x in (meta_for(s, meta).get("keywords") or [])}
                     for k in ("花絮", "ng", "blooper"))]
    if tagged:
        return tagged[:n]
    out = []
    for i, s in enumerate(body):
        if i in used or s.get("keep", True) is False:
            continue
        rec = meta_for(s, meta)
        sc = float(s.get("highlight_score") or 0)
        mood = str(rec.get("mood") or "")
        if -0.5 <= sc <= 0.5 and rec.get("has_people") and \
                any(m in mood for m in ("搞笑", "尷尬", "驚訝", "無奈", "逗")):
            out.append(i)
    return out[:n]


# ── 卡片 ────────────────────────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    for p in _CJK_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_card(out: Path, lines: list[tuple[str, int]], bg: Path | None) -> None:
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1920, 1080
    if bg and bg.exists():
        im = Image.open(bg).convert("RGB").resize((W, H))
        im = im.filter(ImageFilter.GaussianBlur(6))
        ov = Image.new("RGB", (W, H), (0, 0, 0))
        im = Image.blend(im, ov, 0.45)
    else:
        im = Image.new("RGB", (W, H), (12, 12, 14))
    d = ImageDraw.Draw(im)
    # 每行自動縮字級塞進 85% 畫面寬
    fitted = []
    for text, sz in lines:
        while sz > 20:
            f = _font(sz)
            if d.textbbox((0, 0), text, font=f)[2] <= W * 0.85:
                break
            sz -= 4
        fitted.append((text, sz, _font(sz)))
    total = sum(sz + 24 for _, sz, _ in fitted)
    y = (H - total) // 2
    for text, sz, f in fitted:
        w = d.textbbox((0, 0), text, font=f)[2]
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            d.text(((W - w) // 2 + dx, y + dy), text, font=f, fill=(0, 0, 0))
        d.text(((W - w) // 2, y), text, font=f, fill=(245, 245, 245))
        y += sz + 24
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, quality=92)


def make_cards(cfg: dict, km: float, date_range: str) -> tuple[Path, Path]:
    title = cfg["film_title"] or cfg.get("_project_title") or "未命名"
    sub = cfg["film_subtitle"]
    if not cfg["film_title"] and "：" in title:      # project_title「主標：副標」自動拆
        title, sub = title.split("：", 1)
    tp = BOOKENDS_DIR / "title_card.jpg"
    ep = BOOKENDS_DIR / "end_card.jpg"
    bg = ROUTE_OVERVIEW if ROUTE_OVERVIEW.exists() else None
    if not bg:
        print(f"  ⚠️  沒有 {ROUTE_OVERVIEW.name}，卡片用純黑底。先跑 make_map_clips.py --per-day")
    _draw_card(tp, [(title, 130)] + ([(sub, 52)] if sub else []), bg)
    end_lines = [(f"環島 約 {km:.0f} 公里 · {date_range}", 56)]
    if cfg["sign_off"]:
        end_lines.append((cfg["sign_off"], 44))
    if cfg["dedication"]:
        end_lines.append((cfg["dedication"], 64))
    _draw_card(ep, end_lines, bg)
    return tp, ep


ROUTE_BURN = ROOT / "map_clips" / "route_burn.mp4"


def _probe_dur(path: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        return round(float(out.stdout.strip()), 2)
    except Exception:  # noqa: BLE001
        return 11.0


# ── 組裝 ────────────────────────────────────────────────────────────────────
def _card_shot(path: Path, idx: int, segment: str, dur: float, caption: str) -> dict:
    vid = path.suffix.lower() in (".mp4", ".mov", ".m4v")
    return {
        "shot_index": idx, "scene_title": f"__{segment}__", "media_file": path.name,
        "media_type": "video" if vid else "photo", "is_live_photo": False, "live_photo_usage": None,
        "duration_seconds": dur, "shot_type": "全景 (Wide Shot)",
        "visual_description": caption, "crop_focus": "畫面中央",
        "camera_motion": "Static" if vid else "Slow Zoom-in",
        "voiceover": "", "bgm_cue": "", "sfx_cue": "", "transition": "Cross Dissolve",
        "file_path": str(path), "voiceover_kind": "none", "is_canvas": False,
        "segment": segment, "_bookend_generated": True,
    }


def _clip_copy(shot: dict, segment: str, dur: float) -> dict:
    c = copy.deepcopy(shot)
    for k in ("_hl", "keep", "keep_reason", "highlight_score", "timed_subtitles",
              "ken_burns", "source_in", "source_out", "skip"):
        c.pop(k, None)
    c["duration_seconds"] = round(dur, 2)
    c["voiceover"] = ""
    c["segment"] = segment
    c["_bookend_generated"] = True
    c["transition"] = "Cut"
    return c


def build(script: dict, cfg: dict) -> dict:
    meta = load_photos_meta()
    sb = normalize_storyboard(script.get("storyboard", []))
    # 冪等：清掉上次產生的鏡頭 + segment tag
    sb = [s for s in sb if not s.get("_bookend_generated")]
    for s in sb:
        s.pop("segment", None)
    score_storyboard(sb, search_dirs=SEARCH_DIRS)

    body = sb
    # 正片最後一顆保留給 outro（定格情緒句點），不讓其他 segment 挑走
    used: set = {len(body) - 1} if body else set()

    open_idx = pick_cold_open(body, meta, cfg["open_count"], used)
    used |= set(open_idx)
    ret_idx = pick_return(body, meta, used)          # return 比 recap 具體 → 先挑
    if ret_idx is not None:
        used.add(ret_idx)
    recap_idx = pick_recap(body, meta, cfg["recap_max"], used)
    used |= set(recap_idx)
    bloop_idx = pick_bloopers(body, meta, 3, used) if cfg["bloopers"] else []

    # 里程 + 日期範圍
    gps_pts = sorted(
        ((_taken(meta_for(s, meta)), meta_for(s, meta).get("gps")) for s in body),
        key=lambda x: x[0] or _dt.datetime.min)
    track = [(t, g) for t, g in gps_pts if g]
    km = sum(haversine_km((track[i][1]["lat"], track[i][1]["lon"]),
                          (track[i + 1][1]["lat"], track[i + 1][1]["lon"]))
             for i in range(len(track) - 1))
    days = [t.date() for t, _ in gps_pts if t]
    dr = f"{days[0]:%Y.%m.%d}–{days[-1]:%m.%d}" if days else cfg["sign_off"]
    cfg["_project_title"] = script.get("project_title", "")
    tp, ep = make_cards(cfg, km, dr)

    # 先把索引解析成鏡頭物件（之後會 pop body，索引會失效）
    def sname(s):
        return Path(str(s.get("media_file"))).stem
    open_shots = [body[i] for i in open_idx]
    recap_shots = [body[i] for i in recap_idx]
    ret_shot = body[ret_idx] if ret_idx is not None else None
    bloop_shots = [body[i] for i in bloop_idx]

    open_durs = [3.5, 3.0, 2.5, 2.5, 2.5, 2.5, 2.5, 2.5]

    def recap_dur(k: int, m: int) -> float:
        if m <= 3:
            return 1.4
        return 2.2 if k < m - 3 else (1.4, 1.0, 0.6)[k - (m - 3)]

    n = max((int(s.get("shot_index") or 0) for s in body), default=0)
    head = [_clip_copy(s, "cold_open", open_durs[min(k, len(open_durs) - 1)])
            for k, s in enumerate(open_shots)]
    # 片名段：有燃燒火線動畫（route_burn.py 產）就用它，否則用靜態片名卡
    if ROUTE_BURN.exists():
        head.append(_card_shot(ROUTE_BURN, n + 1, "title", _probe_dur(ROUTE_BURN),
                               cfg["film_title"] or cfg["_project_title"]))
        print(f"片名段：route_burn.mp4（{_probe_dur(ROUTE_BURN)}s 燃燒火線）")
    else:
        head.append(_card_shot(tp, n + 1, "title", 4.0, cfg["film_title"] or cfg["_project_title"]))
        print("片名段：靜態片名卡（要動畫版先跑 route_burn.py --title ...）")

    outro = body.pop() if body else None
    if outro is not None:
        outro["segment"] = "outro"
        outro["duration_seconds"] = float(cfg["outro_sec"])

    tail = [_clip_copy(s, "recap", recap_dur(k, len(recap_shots)))
            for k, s in enumerate(recap_shots)]
    if outro is not None:
        tail.append(outro)
    if ret_shot is not None and ret_shot is not outro:
        r = _clip_copy(ret_shot, "return", 6.0)
        r["voiceover"] = ret_shot.get("voiceover") or ""
        r["transition"] = "Cross Dissolve"
        tail.append(r)
    tail.append(_card_shot(ep, n + 2, "endcard", 5.0, cfg["dedication"] or dr))
    tail += [_clip_copy(s, "bloopers", 2.0) for s in bloop_shots]

    script["storyboard"] = head + body + tail
    script.setdefault("bookend_config", {}).update({k: v for k, v in cfg.items()
                                                    if not k.startswith("_")})

    print(f"\n片頭蒙太奇 {len(open_shots)} 顆：" + "、".join(sname(s) for s in open_shots))
    print(f"片尾回顧 {len(recap_shots)} 顆：" + "、".join(sname(s) for s in recap_shots))
    print(f"返家：{sname(ret_shot) if ret_shot is not None and ret_shot is not outro else '（無，跳過）'}")
    print(f"花絮 {len(bloop_shots)} 顆：" + ("、".join(sname(s) for s in bloop_shots) or "（無，跳過）"))
    print(f"里程估算：{km:.0f} km｜日期：{dr}")
    print(f"outro（定格情緒句點）：{sname(outro) if outro else '—'}，拉長到 {cfg['outro_sec']}s、保留昇華旁白")
    return script


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", default=PREFIX)
    ap.add_argument("--write", action="store_true", help="寫回正本 .json（否則只 dry-run 印）")
    ap.add_argument("--open-count", type=int)
    ap.add_argument("--recap-max", type=int)
    ap.add_argument("--outro-sec", type=float)
    ap.add_argument("--no-bloopers", action="store_true")
    args = ap.parse_args()

    script, path = load_script(args.prefix)
    cfg = dict(_DEFAULT_CFG)
    cfg.update(script.get("bookend_config") or {})
    if args.open_count:
        cfg["open_count"] = args.open_count
    if args.recap_max:
        cfg["recap_max"] = args.recap_max
    if args.outro_sec:
        cfg["outro_sec"] = args.outro_sec
    if args.no_bloopers:
        cfg["bloopers"] = False

    script = build(script, cfg)

    if args.write:
        bak = path.with_suffix(f".json.bak-{_dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, bak)
        for base in (ROOT, MEDIA_DIR):
            p = base / f"{args.prefix}.json"
            if p.exists() or base == ROOT:
                p.write_text(json.dumps(script, ensure_ascii=False, indent=1), "utf-8")
        print(f"\n✅ 寫回 {args.prefix}.json（備份 {bak.name}）。接著跑 build_review_packet / --regen-vo")
    else:
        print("\n（dry-run，未寫檔。加 --write 套用）")


if __name__ == "__main__":
    main()
