"""共用的 photos_meta.json 載入器。

由 scripts/probe_folder_metadata.py（快、讀檔案內嵌）或
scripts/dump_photos_metadata.py（慢、讀 Apple Photos DB，多 ML 標籤/分數）產生。
消費者：highlight_engine / abroll_engine / effects_engine / script_engine / grading。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT = Path(__file__).resolve().parents[2] / "scripts" / "photos_meta.json"


@lru_cache(maxsize=8)
def load_photos_meta(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    p = Path(path) if path else _DEFAULT
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k).lower(): v for k, v in raw.items()}
    except Exception:
        return {}


def meta_for(storyboard_shot: Dict[str, Any], meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    raw = storyboard_shot.get("file_path") or storyboard_shot.get("media_file") or ""
    return meta.get(Path(str(raw)).stem.lower(), {})


def face_center(rec: Dict[str, Any]) -> Optional[tuple]:
    """人臉框重心 → (cy, cx) 正規化（配合 smart_crop / effects_engine 的 focus 慣例）。"""
    faces = rec.get("faces") or []
    pts = [(f.get("y"), f.get("x")) for f in faces if f.get("x") is not None and f.get("y") is not None]
    if not pts:
        return None
    cy = sum(p[0] for p in pts) / len(pts)
    cx = sum(p[1] for p in pts) / len(pts)
    return (round(cy, 4), round(cx, 4))
