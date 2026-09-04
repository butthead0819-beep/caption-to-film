"""Live Photo 處理：微動態 → 定格。

每個 Live Photo 鏡頭拆成兩段：
  A. 動態：取 Live Photo 短片 (MOV) 中「最穩定」的 ~1.4 秒，去掉頭尾漂移，
     不加變焦 (只做填滿)，不掛字幕。
  B. 定格：切到配對的靜態照片，補足鏡頭剩餘長度，走緩慢 Ken Burns，
     字幕與口白都落在這一段。

找不到配對照片時退回：只用去頭尾、夾到素材真實長度的單一動態片段。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import List, Optional

LIVE_SECONDS = 1.4          # 動態段目標長度
END_TRIM = 0.35            # 頭尾各去掉多少秒 (漂移/對焦拉風箱)
MIN_STILL_SECONDS = 1.2    # 定格段至少要有這麼長才值得拆兩段
MIN_LIVE_SECONDS = 0.6     # 動態段短於此就不值得保留動態


def _find_pair(stem: str, folder: Optional[Path], search_dirs):
    """嚴格依副檔名找同名的 Live 短片與靜態照片 (不做副檔名 fallback)。"""
    dirs = []
    if folder and str(folder) not in ("", "."):
        dirs.append(Path(folder))
    dirs += [Path(d) for d in (search_dirs or [])]

    def first(exts):
        for d in dirs:
            if not d.is_dir():
                continue
            for ext in exts:
                c = d / f"{stem}{ext}"
                if c.exists():
                    return c
        return None

    mov = first((".MOV", ".mov", ".MP4", ".mp4"))
    still = first((".JPG", ".jpg", ".jpeg", ".HEIC", ".heic", ".PNG", ".png"))
    return mov, still


def _stable_window(mov_path: str, mdur: float, live_len: float, analyze: bool = False) -> tuple:
    """在 MOV 內挑一段 live_len 長的區間 (回傳 in, out)。

    多數 Live Photo 只有 ~2 秒，取中間去頭尾即可；只有較長的短片才值得
    再跑晃動分析挑最穩的一段 (analyze=True)。
    """
    lo, hi = END_TRIM, max(END_TRIM, mdur - END_TRIM)
    center = (lo + hi) / 2
    if analyze:
        try:
            from ..analyzers.motion_stability import analyze_video

            rep = analyze_video(mov_path, min_stable_seconds=0.8)  # 固定值 → 快取可命中
            if rep.stable_ranges:
                sr = max(rep.stable_ranges, key=lambda r: r[1] - r[0])
                center = (sr[0] + sr[1]) / 2
        except Exception:
            pass
    a = min(max(lo, center - live_len / 2), max(lo, hi - live_len))
    return round(a, 3), round(a + live_len, 3)


def expand_live_photos(
    storyboard: List[dict],
    search_dirs: Optional[List[str]] = None,
    live_seconds: float = LIVE_SECONDS,
) -> List[str]:
    from ..util.media_probe import probe_video

    changes: List[str] = []
    out: List[dict] = []

    for idx, shot in enumerate(storyboard, start=1):
        if shot.get("skip") or not shot.get("is_live_photo") or "source_in" in shot:
            out.append(shot)
            continue

        raw = shot.get("file_path") or shot.get("media_file") or ""
        stem = Path(str(raw)).stem
        folder = Path(str(raw)).parent if str(raw) else None
        mov, still = _find_pair(stem, folder, search_dirs)
        if not mov:
            # 沒有配對短片 → 當一般照片處理
            shot["is_live_photo"] = False
            if still:
                shot["file_path"], shot["media_file"] = str(still), Path(still).name
            shot["media_type"] = "image"
            out.append(shot)
            continue

        total = float(shot.get("duration_seconds", 4.0))
        mdur = probe_video(str(mov))["duration_s"] or 0.0
        if mdur > 8.0:
            # 不是真正的 Live Photo (太長) → 當一般影片，不拆
            out.append(shot)
            continue
        trim = END_TRIM if mdur > (2 * END_TRIM + MIN_LIVE_SECONDS) else min(0.12, mdur * 0.1)
        live_len = max(0.0, min(live_seconds, mdur - 2 * trim))

        # 動態段：短片就取中間去頭尾；較長 (>2.6s) 才跑晃動分析挑最穩一段
        a, b = _stable_window(str(mov), mdur, live_len, analyze=mdur > 2.6) \
            if live_len >= MIN_LIVE_SECONDS else (trim, max(trim, mdur - trim))
        still_len = round(total - (b - a), 2)

        if live_len >= MIN_LIVE_SECONDS and still and still_len >= MIN_STILL_SECONDS:
            motion = copy.deepcopy(shot)
            motion.update({
                "file_path": str(mov), "media_file": Path(mov).name, "media_type": "video",
                "is_live_photo": False, "source_in": a, "source_out": b,
                "duration_seconds": round(b - a, 2), "voiceover": "",
                "visual_action": f"{shot.get('visual_action', '')} [Live 微動態]".strip(),
            })
            motion.pop("timed_subtitles", None)
            motion.pop("shake_cut_note", None)

            freeze = copy.deepcopy(shot)
            freeze.update({
                "file_path": str(still), "media_file": Path(still).name, "media_type": "image",
                "is_live_photo": False, "duration_seconds": still_len,
                "transition": "直切 (Cut)",
                "visual_action": f"{shot.get('visual_action', '')} [定格]".strip(),
            })
            for k in ("source_in", "source_out", "shake_cut_note"):
                freeze.pop(k, None)

            out.extend([motion, freeze])
            changes.append(f"Shot {idx:02d} ({stem}): 微動態 {b - a:.1f}s → 靜態定格 {still_len:.1f}s")
        else:
            # 退回：單一去頭尾動態片段，夾到素材真實長度
            single = copy.deepcopy(shot)
            keep = round(min(total, max(MIN_LIVE_SECONDS, mdur - 2 * trim)), 2)
            single.update({
                "file_path": str(mov), "media_file": Path(mov).name, "media_type": "video",
                "source_in": round(trim, 3),
                "source_out": round(trim + keep, 3), "duration_seconds": keep,
            })  # 保留 is_live_photo=True → normalize+rebuild 可重複執行不失真
            out.append(single)
            why = "無配對照片" if not still else ("鏡頭太短、無定格空間" if total < live_len + MIN_STILL_SECONDS else "動態段過短")
            changes.append(f"Shot {idx:02d} ({stem}): 去頭尾夾到 {keep:.1f}s ({why})")

    storyboard[:] = out
    return changes
