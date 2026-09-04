"""媒體檔案探測工具：讀取影片/圖片的真實尺寸、時長、影格率與音軌資訊。

FCPXML 匯入若缺少正確的 format 寬高、時長與 media-rep，Final Cut / DaVinci
會直接把素材標記為 Offline。本模組提供穩定的探測結果供匯出器使用。
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".hevc"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".gif", ".bmp"}


def _register_heif() -> None:
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
    except Exception:
        pass


@lru_cache(maxsize=2048)
def probe_image(path: str) -> Dict:
    """回傳 {"width": int, "height": int}；失敗時退回 1920x1080。"""
    _register_heif()
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)  # 套用 EXIF 旋轉，取得顯示尺寸
            w, h = im.size
        if w > 0 and h > 0:
            return {"width": int(w), "height": int(h)}
    except Exception:
        pass
    return {"width": 1920, "height": 1080}


@lru_cache(maxsize=2048)
def probe_video(path: str) -> Dict:
    """回傳影片主要視訊軌資訊：
    {"width", "height", "duration_s", "fps", "has_audio"}。
    多視訊軌 (例如 iPhone HEVC + 縮圖軌) 時取畫面積最大的那一軌。
    """
    result = {"width": 1920, "height": 1080, "duration_s": 0.0, "fps": 30.0, "has_audio": True}
    try:
        from pymediainfo import MediaInfo

        mi = MediaInfo.parse(path)
        video_tracks = [t for t in mi.tracks if t.track_type == "Video"]
        audio_tracks = [t for t in mi.tracks if t.track_type == "Audio"]
        general = next((t for t in mi.tracks if t.track_type == "General"), None)

        if video_tracks:
            def area(t):
                try:
                    return (int(t.width or 0)) * (int(t.height or 0))
                except Exception:
                    return 0

            vt = max(video_tracks, key=area)
            if vt.width and vt.height:
                result["width"] = int(vt.width)
                result["height"] = int(vt.height)
            dur_ms = vt.duration or (general.duration if general else None)
            if dur_ms:
                result["duration_s"] = round(float(dur_ms) / 1000.0, 3)
            if vt.frame_rate:
                try:
                    result["fps"] = float(vt.frame_rate)
                except Exception:
                    pass
        result["has_audio"] = len(audio_tracks) > 0
        if result["duration_s"] <= 0:
            result["duration_s"] = _ffprobe_duration(path)
    except Exception:
        result["duration_s"] = _ffprobe_duration(path)
    return result


def _ffprobe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return round(float(out.stdout.strip()), 3)
    except Exception:
        return 0.0


def resolve_existing_path(raw_path: str, search_dirs: Optional[list] = None) -> Optional[Path]:
    """把腳本記錄的路徑對應到「真的存在」的檔案。

    - 原路徑存在 → 直接回傳。
    - 否則嘗試同名的其他常見副檔名 (大小寫、jpg/heic/mov 等)。
    - 否則在 search_dirs 內以檔名 (stem) 搜尋。
    - 全部失敗回傳 None (呼叫端應保留原路徑並標記需重新連結)。
    """
    if not raw_path:
        return None
    p = Path(raw_path)
    if p.exists():
        return p

    candidate_exts = [
        ".mov", ".MOV", ".mp4", ".MP4", ".m4v", ".M4V",
        ".jpg", ".JPG", ".jpeg", ".JPEG", ".heic", ".HEIC",
        ".png", ".PNG", ".webp", ".WEBP", ".tif", ".TIFF",
    ]
    for ext in candidate_exts:
        c = p.with_suffix(ext)
        if c.exists():
            return c

    dirs = list(search_dirs or [])
    if p.parent and str(p.parent) not in ("", "."):
        dirs.insert(0, p.parent)
    stem = p.stem
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for ext in candidate_exts:
            c = d / f"{stem}{ext}"
            if c.exists():
                return c
    return None


def is_video_path(path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS
