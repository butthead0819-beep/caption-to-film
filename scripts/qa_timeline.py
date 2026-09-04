#!/usr/bin/env python3
"""階段 8：匯出後的靜態校驗 — 在把 FCPXML/SRT 交給人類或丟進 Resolve 前先自檢。

專抓「Resolve 匯入後才發現」的已知地雷（見 film-edit skill / fcpxml-resolve.md）：
  1. <format> 缺 frameDuration        → Resolve 把該素材全部 Media Offline
  2. <media-rep src> 指向的檔不存在    → Media Offline
  3. 靜態圖 format 缺 colorSpace / asset 缺 uid / 奇數像素尺寸 → Media Offline
  4. 兩個 asset 同 name（MOV 與 JPG）  → Resolve 去重，其一 offline
  5. 出現 <title>（除非 --allow-titles）→ Resolve 忽略 Position、字幕置中壓畫面
  6. ref= 指到不存在的 asset / format
  7. 字幕 SRT：末則超出時間軸總長、cue 重疊、單則過短

用法：
  .venv/bin/python scripts/qa_timeline.py                       # 掃專案根目錄所有 *.fcpxml
  .venv/bin/python scripts/qa_timeline.py my_trip
  .venv/bin/python scripts/qa_timeline.py --fcpxml a.fcpxml --srt a.srt
  .venv/bin/python scripts/qa_timeline.py --audio 成片.wav       # 額外量響度 (pyloudnorm)

exit code 0 = 全部通過；1 = 有硬性錯誤。
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.media_probe import IMAGE_EXTS, probe_image  # noqa: E402

from scripts._config import MEDIA_DIR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Report:
    def __init__(self, label: str):
        self.label = label
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def err(self, m: str) -> None:
        self.errors.append(m)

    def warn(self, m: str) -> None:
        self.warnings.append(m)

    def note(self, m: str) -> None:
        self.notes.append(m)

    def print(self) -> None:
        icon = "❌" if self.errors else ("⚠️ " if self.warnings else "✅")
        print(f"\n{icon} {self.label}")
        for m in self.errors:
            print(f"   ❌ {m}")
        for m in self.warnings:
            print(f"   ⚠️  {m}")
        for m in self.notes:
            print(f"   ·  {m}")


def _rational_to_seconds(s: str | None) -> float:
    if not s:
        return 0.0
    s = s.strip().rstrip("s")
    if "/" in s:
        num, den = s.split("/")
        return float(num) / float(den) if float(den) else 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _src_to_path(src: str) -> Path:
    return Path(unquote(urlparse(src).path))


def check_fcpxml(path: Path, allow_titles: bool) -> Report:
    rep = Report(path.name)
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        rep.err(f"XML 無法解析：{e}")
        return rep
    root = tree.getroot()

    resources = root.find("resources")
    formats = {f.get("id"): f for f in resources.findall("format")} if resources is not None else {}
    assets = {a.get("id"): a for a in resources.findall("asset")} if resources is not None else {}

    # 1. format 缺 frameDuration
    no_fd = [fid for fid, f in formats.items() if not f.get("frameDuration")]
    if no_fd:
        rep.err(f"{len(no_fd)} 個 <format> 缺 frameDuration（Resolve 會標 Media Offline）：{', '.join(no_fd)}")
    else:
        rep.note(f"{len(formats)} 個 <format> 都有 frameDuration")

    # 2 + 3. 每個 asset：檔案存在？靜態圖 colorSpace/uid/偶數尺寸？
    missing, odd_dims, no_colorspace, no_uid = [], [], [], []
    name_map: dict[str, list[str]] = {}
    for aid, a in assets.items():
        name_map.setdefault(a.get("name", ""), []).append(aid)
        mr = a.find("media-rep")
        src = mr.get("src") if mr is not None else None
        p = _src_to_path(src) if src else None
        if p is None or not p.exists():
            missing.append(f"{aid} ({a.get('name')}) → {p}")
            continue
        is_img = p.suffix.lower() in IMAGE_EXTS or a.get("duration") in ("0s", "0/3000s")
        if is_img:
            fmt = formats.get(a.get("format"))
            if fmt is not None and not fmt.get("colorSpace"):
                no_colorspace.append(a.get("name"))
            if not a.get("uid"):
                no_uid.append(a.get("name"))
            try:
                dim = probe_image(str(p))
                if dim["width"] % 2 or dim["height"] % 2:
                    odd_dims.append(f"{a.get('name')} {dim['width']}x{dim['height']}")
            except Exception:  # noqa: BLE001
                pass

    if missing:
        rep.err(f"{len(missing)} 個 asset 的媒體檔不存在：")
        for m in missing:
            rep.err(f"    {m}")
    if odd_dims:
        rep.err(f"{len(odd_dims)} 張圖是奇數像素尺寸（Resolve 直接 offline）：{'; '.join(odd_dims)}")
    if no_colorspace:
        rep.warn(f"{len(no_colorspace)} 張靜態圖 format 缺 colorSpace：{', '.join(no_colorspace[:8])}")
    if no_uid:
        rep.warn(f"{len(no_uid)} 個靜態圖 asset 缺 uid：{', '.join(no_uid[:8])}")

    # 4. asset 同 name
    dups = {n: ids for n, ids in name_map.items() if n and len(ids) > 1}
    if dups:
        rep.err(f"{len(dups)} 組 asset 同 name（Resolve 去重 → 其一 offline，name 要帶副檔名）：")
        for n, ids in dups.items():
            rep.err(f"    '{n}' ← {', '.join(ids)}")

    # 5. <title>
    titles = list(root.iter("title"))
    if titles and not allow_titles:
        rep.err(f"{len(titles)} 個 <title>（Resolve 忽略 Position → 字幕置中）。字幕應只走 SRT 軌；"
                f"用 --allow-titles 表示這份是給 Final Cut 的")
    elif titles:
        rep.note(f"{len(titles)} 個 <title>（--allow-titles，視為給 FCP）")

    # 6. ref 完整性
    bad_ref = 0
    for clip in root.iter():
        if clip.tag in ("asset-clip", "video", "audio", "clip"):
            r = clip.get("ref")
            if r and r not in assets and r not in formats:
                bad_ref += 1
        if clip.tag == "format":
            continue
    for a in assets.values():
        if a.get("format") and a.get("format") not in formats:
            rep.err(f"asset {a.get('id')} 的 format={a.get('format')} 不存在")
    if bad_ref:
        rep.err(f"{bad_ref} 個 clip 的 ref= 指到不存在的 asset")

    # 時間軸總長
    seq = root.find(".//sequence")
    if seq is not None:
        total = _rational_to_seconds(seq.get("duration"))
        rep._timeline_seconds = total  # type: ignore[attr-defined]
        rep.note(f"時間軸總長 {total:.1f}s，{len(list(root.iter('asset-clip'))) + len(list(root.iter('video')))} 個 clip")
    return rep


_SRT_TIME = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d)")


def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    for b in blocks:
        lines = [ln for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        tl = next((ln for ln in lines if "-->" in ln), None)
        if not tl:
            continue
        a, _, c = tl.partition("-->")
        ma, mc = _SRT_TIME.search(a), _SRT_TIME.search(c)
        if not (ma and mc):
            continue
        def sec(m: re.Match) -> float:
            h, mi, s, ms = map(int, m.groups())
            return h * 3600 + mi * 60 + s + ms / 1000
        text = "\n".join(lines[lines.index(tl) + 1:])
        cues.append((sec(ma), sec(mc), text))
    return cues


def check_srt(path: Path, timeline_seconds: float | None) -> Report:
    rep = Report(path.name)
    cues = _parse_srt(path)
    if not cues:
        rep.warn("解析不到任何字幕 cue")
        return rep
    rep.note(f"{len(cues)} 則字幕，末則結束 {cues[-1][1]:.1f}s")

    # 允許字幕溢到片尾後 OVERFLOW_TAIL_SEC(1.5s) + 影格捨入餘裕
    if timeline_seconds and cues[-1][1] > timeline_seconds + 2.0:
        rep.err(f"末則字幕 {cues[-1][1]:.1f}s 超出時間軸總長 {timeline_seconds:.1f}s "
                f"（超 {cues[-1][1] - timeline_seconds:.1f}s → 字幕會飄到畫面結束後）")

    overlaps = sum(1 for i in range(1, len(cues)) if cues[i][0] < cues[i - 1][1] - 0.001)
    if overlaps:
        rep.err(f"{overlaps} 處字幕時間重疊")

    short = [i + 1 for i, (a, b, _) in enumerate(cues) if b - a < 1.0]
    if short:
        rep.warn(f"{len(short)} 則字幕短於 1.0s（閃一下看不完）：#{', #'.join(map(str, short[:10]))}")

    long_lines = [i + 1 for i, (_, _, t) in enumerate(cues)
                  if max((len(ln) for ln in t.splitlines()), default=0) > 20]
    if long_lines:
        rep.warn(f"{len(long_lines)} 則單行超過 20 全形字：#{', #'.join(map(str, long_lines[:10]))}")
    return rep


def check_loudness(path: Path) -> Report:
    rep = Report(f"響度 {path.name}")
    try:
        import numpy as np
        import pyloudnorm as pyln
        import soundfile as sf
    except Exception as e:  # noqa: BLE001
        rep.warn(f"缺套件，跳過：{e}")
        return rep
    try:
        data, rate = sf.read(str(path))
        meter = pyln.Meter(rate)
        loud = meter.integrated_loudness(data)
        rep.note(f"整合響度 {loud:.1f} LUFS（YouTube 目標 -14）")
        if loud > -12 or loud < -18:
            rep.warn(f"偏離 -14 LUFS 太多，建議 loudnorm 正規化")
    except Exception as e:  # noqa: BLE001
        rep.warn(f"讀取失敗：{e}")
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", help="專案 prefix（不含副檔名）；省略則掃根目錄全部 *.fcpxml")
    ap.add_argument("--fcpxml", help="直接指定 fcpxml 路徑")
    ap.add_argument("--srt", help="直接指定 srt 路徑（省略則找同名 _字幕.srt）")
    ap.add_argument("--audio", help="額外量這個音檔的響度（pyloudnorm）")
    ap.add_argument("--allow-titles", action="store_true", help="這份是給 Final Cut 的，不把 <title> 當錯")
    args = ap.parse_args()

    if args.fcpxml:
        targets = [Path(args.fcpxml)]
    elif args.prefix:
        targets = [ROOT / f"{args.prefix}.fcpxml"]
    else:
        targets = sorted(ROOT.glob("*.fcpxml"))

    all_reports: list[Report] = []
    for fx in targets:
        if not fx.exists():
            r = Report(fx.name)
            r.err("檔案不存在")
            all_reports.append(r)
            continue
        frep = check_fcpxml(fx, args.allow_titles)
        all_reports.append(frep)

        srt = Path(args.srt) if args.srt else fx.with_name(fx.stem + "_字幕.srt")
        if srt.exists():
            all_reports.append(check_srt(srt, getattr(frep, "_timeline_seconds", None)))
        elif args.srt:
            r = Report(srt.name)
            r.err("指定的 SRT 不存在")
            all_reports.append(r)

    if args.audio:
        all_reports.append(check_loudness(Path(args.audio)))

    for r in all_reports:
        r.print()

    n_err = sum(len(r.errors) for r in all_reports)
    n_warn = sum(len(r.warnings) for r in all_reports)
    print(f"\n{'='*50}\n{len(targets)} 個時間軸｜{n_err} 個錯誤｜{n_warn} 個警告")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
