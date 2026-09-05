"""影片晃動偵測：找出大幅手震／晃動的時間區段，供自動剪除使用。

作法 (不需 OpenCV，只用 ffmpeg + numpy)：
1. ffmpeg 以低解析度灰階抽幀 (預設 8 fps, 160x90)。
2. 相鄰影格做相位相關 (phase correlation) 估計位移向量。
3. 位移「變化量」的滑動標準差 = 晃動指標 (jitter)；
   平移穩定的運鏡 jitter 低，手震／劇烈晃動 jitter 高。
4. 只挖掉晃動段落，其餘穩定段全部保留 (一個鏡頭可切成多段)。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

SAMPLE_FPS = 8
SAMPLE_W = 160
SAMPLE_H = 90


@dataclass
class StabilityReport:
    path: str
    duration_s: float
    sample_fps: int
    jitter: List[float] = field(default_factory=list)      # 每個取樣點的晃動指標
    shaky_ranges: List[Tuple[float, float]] = field(default_factory=list)
    stable_ranges: List[Tuple[float, float]] = field(default_factory=list)
    shaky_fraction: float = 0.0
    best_stable_range: Optional[Tuple[float, float]] = None
    verdict: str = "keep"   # keep | trim | drop | error
    note: str = ""


def _extract_gray_frames(path: str, fps: int = SAMPLE_FPS) -> Optional[np.ndarray]:
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path,
        "-vf", f"fps={fps},scale={SAMPLE_W}:{SAMPLE_H},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    frame_size = SAMPLE_W * SAMPLE_H
    n = len(proc.stdout) // frame_size
    if n < 3:
        return None
    arr = np.frombuffer(proc.stdout[: n * frame_size], dtype=np.uint8)
    return arr.reshape(n, SAMPLE_H, SAMPLE_W).astype(np.float32)


def _phase_correlate(a: np.ndarray, b: np.ndarray) -> Tuple[float, float]:
    """回傳 b 相對 a 的位移 (dx, dy)，單位為取樣像素。"""
    win = np.hanning(a.shape[0])[:, None] * np.hanning(a.shape[1])[None, :]
    fa = np.fft.rfft2((a - a.mean()) * win)
    fb = np.fft.rfft2((b - b.mean()) * win)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-8
    corr = np.fft.irfft2(r, s=a.shape)
    peak = np.unravel_index(np.argmax(corr), corr.shape)
    dy, dx = peak
    if dy > a.shape[0] // 2:
        dy -= a.shape[0]
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return float(dx), float(dy)


@lru_cache(maxsize=512)
def analyze_video(
    path: str,
    shake_threshold: float = 3.2,
    min_stable_seconds: float = 1.5,
    min_shaky_seconds: float = 0.6,
    fps: int = SAMPLE_FPS,
) -> StabilityReport:
    """分析單一影片的晃動情形 (同一檔案+參數會快取，重複引用不重跑)。

    shake_threshold: jitter 超過此值即視為晃動 (取樣像素/幀，越小越嚴格；
                     160x90 取樣下 ~3 相當於明顯手震)。
    """
    p = Path(path)
    rep = StabilityReport(path=str(p), duration_s=0.0, sample_fps=fps)
    frames = _extract_gray_frames(str(p), fps=fps)
    if frames is None:
        rep.verdict = "error"
        rep.note = "無法抽幀 (檔案不存在或非影片)"
        return rep

    n = len(frames)
    rep.duration_s = round(n / fps, 3)

    # 每幀位移
    disp = np.zeros((n, 2), dtype=np.float32)
    for i in range(1, n):
        disp[i] = _phase_correlate(frames[i - 1], frames[i])

    # 晃動指標 = 位移「二階變化」的滑動 RMS (去掉穩定平移運鏡的影響)
    accel = np.diff(disp, axis=0, prepend=disp[:1])
    mag = np.sqrt((accel ** 2).sum(axis=1))
    win = max(3, int(round(fps * 0.5)))
    kernel = np.ones(win) / win
    jitter = np.sqrt(np.convolve(mag ** 2, kernel, mode="same"))
    rep.jitter = [round(float(x), 3) for x in jitter]

    shaky_mask = jitter > shake_threshold
    rep.shaky_fraction = round(float(shaky_mask.mean()), 3)

    # 忽略 < min_shaky_seconds 的瞬間抖動 (單幀尖峰不該把片段切碎)
    total_s = n / fps
    shaky_ranges = _merge_ranges(_mask_to_ranges(shaky_mask, fps), gap=0.5)
    shaky_ranges = [r for r in shaky_ranges if (r[1] - r[0]) >= min_shaky_seconds]
    rep.shaky_ranges = shaky_ranges

    # 穩定段 = 全長扣掉晃動段，再合併間隔很小的碎段，最後濾掉太短的
    stable = _complement(shaky_ranges, total_s)
    stable = _merge_ranges(stable, gap=0.6)
    rep.stable_ranges = [r for r in stable if (r[1] - r[0]) >= min_stable_seconds]

    if rep.stable_ranges:
        rep.best_stable_range = max(rep.stable_ranges, key=lambda r: r[1] - r[0])

    stable_total = sum(b - a for a, b in rep.stable_ranges)
    if not shaky_ranges or stable_total >= total_s * 0.92:
        rep.verdict = "keep"
    elif rep.stable_ranges:
        rep.verdict = "trim"
        rep.note = f"保留 {len(rep.stable_ranges)} 段穩定片段，剔除約 {total_s - stable_total:.1f}s 晃動"
    else:
        rep.verdict = "drop"
        rep.note = f"全片晃動比例 {rep.shaky_fraction:.0%}，無足夠穩定區間"
    return rep


def _complement(ranges: List[Tuple[float, float]], total: float) -> List[Tuple[float, float]]:
    """回傳 [0, total] 內不被 ranges 覆蓋的區段。"""
    out: List[Tuple[float, float]] = []
    cursor = 0.0
    for a, b in sorted(ranges):
        if a > cursor:
            out.append((round(cursor, 2), round(a, 2)))
        cursor = max(cursor, b)
    if cursor < total:
        out.append((round(cursor, 2), round(total, 2)))
    return out


def _merge_ranges(ranges: List[Tuple[float, float]], gap: float) -> List[Tuple[float, float]]:
    """合併間隔 <= gap 的相鄰區段。"""
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [list(ranges[0])]
    for a, b in ranges[1:]:
        if a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in out]


def _mask_to_ranges(mask: np.ndarray, fps: int, min_len: float = 0.0) -> List[Tuple[float, float]]:
    ranges: List[Tuple[float, float]] = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            ranges.append((start / fps, i / fps))
            start = None
    if start is not None:
        ranges.append((start / fps, len(mask) / fps))
    if min_len > 0:
        ranges = [r for r in ranges if (r[1] - r[0]) >= min_len]
    return [(round(a, 2), round(b, 2)) for a, b in ranges]


def apply_to_storyboard(
    storyboard: List[dict],
    search_dirs: Optional[List[str]] = None,
    shake_threshold: float = 3.2,
    min_stable_seconds: float = 1.5,
    drop_shaky: bool = True,
    max_segments: int = 8,
) -> List[dict]:
    """就地重寫 storyboard (以 slice assignment)：對每個影片鏡頭跑晃動分析，
    「只挖掉晃動段落、保留所有穩定段」，一個鏡頭可被切成多個子鏡頭。

    寫入欄位: source_in / source_out / duration_seconds / skip / shake_cut_note。
    回傳異動摘要清單。
    """
    import copy

    from ..util.media_probe import resolve_existing_path, is_video_path

    changes: List[str] = []
    new_storyboard: List[dict] = []

    for idx, shot in enumerate(storyboard, start=1):
        raw = shot.get("file_path") or shot.get("media_file") or ""
        # 已經被晃動剪除處理過的子鏡頭不再重切 (避免重複執行時越切越碎)
        if "source_in" in shot or "source_out" in shot:
            new_storyboard.append(shot)
            continue
        # 感觸畫布鏡頭：刻意放長來鋪 reflection 字幕，不做晃動剪除
        # (整支晃動交給 --stabilize-clips 的 vid.stab，長度不動)
        if shot.get("is_canvas"):
            new_storyboard.append(shot)
            continue
        resolved = resolve_existing_path(str(raw), search_dirs)
        if not resolved or not is_video_path(resolved):
            new_storyboard.append(shot)
            continue

        rep = analyze_video(str(resolved), shake_threshold=shake_threshold,
                            min_stable_seconds=min_stable_seconds)
        name = Path(str(resolved)).name

        if rep.verdict in ("keep", "error"):
            new_storyboard.append(shot)
            continue

        ranges = list(rep.stable_ranges)  # 已依 min_stable_seconds 過濾
        capped = False
        if len(ranges) > max_segments:
            ranges = sorted(sorted(ranges, key=lambda r: r[1] - r[0], reverse=True)[:max_segments])
            capped = True
        if not ranges:
            if drop_shaky:
                dropped = copy.deepcopy(shot)
                dropped["skip"] = True
                dropped["shake_cut_note"] = rep.note
                new_storyboard.append(dropped)
                changes.append(f"Shot {idx:02d} ({name}): 整段剔除 — {rep.note}")
            else:
                new_storyboard.append(shot)
            continue

        # 鏡頭被裁短 → 旁白也要按比例縮短（只保留前面完整的句子），
        # 不然剩下的秒數塞不下原本的全部旁白、字幕會擠爆。
        kept_sec = sum(b - a for a, b in ranges)
        vo_full = shot.get("voiceover") or ""
        vo_trimmed = _fit_text_to_seconds(vo_full, kept_sec)

        # 依各穩定段時長比例分配口白
        weights = [b - a for a, b in ranges]
        vo_parts = _distribute_text(vo_trimmed, weights)

        for j, ((a, b), vo_part) in enumerate(zip(ranges, vo_parts), start=1):
            sub = copy.deepcopy(shot)
            sub["source_in"] = round(a, 3)
            sub["source_out"] = round(b, 3)
            sub["duration_seconds"] = round(b - a, 2)
            sub["voiceover"] = vo_part
            sub.pop("timed_subtitles", None)
            sub["shake_cut_note"] = f"原片段 {a:.1f}–{b:.1f}s (已剔除晃動)"
            base_action = str(shot.get("visual_action", "")).split(" [穩定段")[0]
            sub["visual_action"] = f"{base_action} [穩定段 {j}/{len(ranges)}]"
            if j > 1:
                sub["transition"] = "直切 (Cut)"
            new_storyboard.append(sub)

        cut = rep.duration_s - sum(weights)
        changes.append(
            f"Shot {idx:02d} ({name}): 切成 {len(ranges)} 段穩定片段"
            f"{'（已取最長 %d 段）' % max_segments if capped else ''}，"
            f"共剔除約 {cut:.1f}s 晃動 (晃動比例 {rep.shaky_fraction:.0%})"
        )

    storyboard[:] = new_storyboard
    return changes


def _fit_text_to_seconds(text: str, seconds: float, cps: float = 6.0, slack: float = 1.15) -> str:
    """把口白裁到「這麼多秒 * 每秒字數」的長度，只砍整句、從頭保留。"""
    from ..engines.subtitle_engine import SubtitleEngine, _char_count

    text = (text or "").strip()
    if not text:
        return ""
    budget = max(8, int(seconds * cps * slack))
    if _char_count(text) <= budget:
        return text
    out, used = [], 0
    for seg in SubtitleEngine.split_into_segments(text):
        if out and used + _char_count(seg) > budget:
            break
        out.append(seg)
        used += _char_count(seg)
    return "".join(out) or text[:budget]


def _distribute_text(text: str, weights: List[float]) -> List[str]:
    """把一段口白依 weights 比例、保持原順序分配到 len(weights) 個區塊。"""
    from ..engines.subtitle_engine import SubtitleEngine

    n = len(weights)
    if n == 0:
        return []
    pieces = SubtitleEngine.split_into_segments(text) if text.strip() else []
    if not pieces:
        return [""] * n

    total_w = sum(weights) or 1.0
    cum_target, acc = [], 0.0
    for w in weights:
        acc += w / total_w
        cum_target.append(acc)

    piece_chars = [max(1, len(p)) for p in pieces]
    total_c = sum(piece_chars)
    out = [""] * n
    bin_i, used_c = 0, 0
    for p, pc in zip(pieces, piece_chars):
        out[bin_i] += p
        used_c += pc
        while bin_i < n - 1 and used_c / total_c >= cum_target[bin_i] - 1e-9:
            bin_i += 1
    return out
