"""把 DaVinci Resolve 不吃的靜態圖片，前處理成乾淨版本。

Resolve 匯入**奇數尺寸**的圖片（寬或高不是 2 的倍數）會直接 Media Offline
——下載來的截圖 / 全景裁切 / 網路圖常常是奇數尺寸（1477、2599…）。
這裡把它們裁掉 1px 變偶數、轉 sRGB、存成 baseline JPEG 到 `_prepared/`，
再讓 exporter 連到新檔。iPhone 原生照片一律偶數，不會被動到。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}


def needs_prep(path: str) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        return bool(w % 2 or h % 2)
    except Exception:
        return False


def prepare_still(path: str, out_dir: str) -> Optional[str]:
    """回傳可用的圖片路徑。
    - HEIC/HEIF → 一律轉 sRGB baseline JPEG（下游 ffmpeg / Resolve / iMovie 都吃、且快）。
    - 奇數尺寸 → 裁 1px 變偶數（Resolve 才不會 Media Offline）。
    - 其他 → 回傳原路徑。失敗 → 回傳原路徑。"""
    p = Path(path)
    if p.suffix.lower() not in _EXT:
        return path
    is_heic = p.suffix.lower() in (".heic", ".heif")
    try:
        from PIL import Image, ImageOps

        _register_heif()
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            odd = bool(w % 2 or h % 2)
            if not is_heic and not odd:
                return path
            if odd:
                im = im.crop((0, 0, w - (w % 2), h - (h % 2)))
            # 4K 照片經 Ken Burns supersample 渲染極慢 → 長邊封頂 2560（1080p + 2x zoom 綽綽有餘）
            long_edge = max(im.size)
            if long_edge > 2560:
                k = 2560 / long_edge
                im = im.resize((int(im.width * k) & ~1, int(im.height * k) & ~1), Image.LANCZOS)
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            dst = out / f"{p.stem}.jpg"
            if dst.exists() and dst.stat().st_mtime >= p.stat().st_mtime:
                return str(dst)                 # 已轉過、來源沒更新 → 直接用
            im.convert("RGB").save(dst, "JPEG", quality=92, subsampling=0)
        return str(dst)
    except Exception:
        return path


def _register_heif() -> None:
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
    except Exception:
        pass
