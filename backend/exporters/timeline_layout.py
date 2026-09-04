"""時間軸佈局：算出每個鏡頭在成片時間軸上的精確起訖秒數。

FCPXML exporter 與 SRT exporter **必須用同一份佈局**，不然字幕會跟畫面對不上
（晃動剪掉的影格、影片夾到素材真實長度、影格對齊的捨入…都要一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util.media_probe import probe_video, resolve_existing_path, is_video_path

FPS = 30
_DROP_VO = ("", "null", "無", "(純畫面與環境音)", "[留白，專注於現場環境音與配樂]")


def _frames(sec: float) -> int:
    return max(1, int(round(float(sec) * FPS)))


def clip_seconds(shot: Dict[str, Any], is_video: bool, media_dur: Optional[float]) -> float:
    src_in = float(shot.get("source_in", 0.0) or 0.0)
    src_out = shot.get("source_out")
    if src_out is not None and is_video:
        sec = max(1.0 / FPS, float(src_out) - src_in)
    else:
        sec = float(shot.get("duration_seconds", 4.0))
    if is_video and media_dur:
        sec = min(sec, max(1.0 / FPS, media_dur - src_in))
    return sec


def timeline_layout(script_data: Dict[str, Any],
                    search_dirs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """回傳 [{shot, start, dur, frame_start}]，start/dur 為秒、影格對齊、已濾掉 skip。"""
    out: List[Dict[str, Any]] = []
    frame_cursor = 0
    for shot in script_data.get("storyboard", []):
        if shot.get("skip"):
            continue
        raw = shot.get("file_path") or shot.get("media_file") or ""
        resolved = resolve_existing_path(str(raw), search_dirs)
        path = resolved if resolved else Path(str(raw))
        is_vid = is_video_path(path)
        media_dur = None
        if is_vid and resolved:
            try:
                media_dur = probe_video(str(resolved)).get("duration_s") or None
            except Exception:
                media_dur = None
        sec = clip_seconds(shot, is_vid, media_dur)
        f = _frames(sec)
        out.append({
            "shot": shot,
            "start": frame_cursor / FPS,
            "dur": f / FPS,
            "frame_start": frame_cursor,
        })
        frame_cursor += f
    return out
