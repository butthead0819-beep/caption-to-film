"""依 Apple ML 場景標籤 → 每個素材一組調色偏移建議 (grade hint)。

寫成 sidecar `<prefix>_grade_hints.json`，`scripts/resolve_auto_grade.py --hints <path>`
會把它「疊在」灰世界白平衡結果之上（乘 slope、加 offset、乘飽和）。

preset 值是「相對」倍率／偏移，故意保守；在 Resolve 色彩頁還能再微調。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

# label 關鍵字 → {slope 乘, offset 加(RGB), sat 乘, 說明}
_PRESETS: List[Dict[str, Any]] = [
    {"keys": {"sunset", "sunrise", "dusk", "dawn", "golden hour"},
     "slope": [1.03, 1.00, 0.95], "offset": [0.008, 0.002, -0.004], "sat": 1.06,
     "note": "暖調 + 微抬黑位 + 壓藍"},
    {"keys": {"beach", "ocean", "sea", "coast", "wave", "surf", "harbor"},
     "slope": [0.98, 1.00, 1.03], "offset": [-0.003, 0.0, 0.004], "sat": 1.08,
     "note": "微冷 + 提藍綠飽和"},
    {"keys": {"lake", "river", "reflection", "waterfall"},
     "slope": [0.99, 1.01, 1.02], "offset": [0.0, 0.002, 0.002], "sat": 1.05,
     "note": "清透水色"},
    {"keys": {"forest", "tree", "grass", "field", "mountain", "hill", "valley", "rice"},
     "slope": [0.99, 1.02, 0.99], "offset": [0.0, 0.003, 0.0], "sat": 1.05,
     "note": "綠意 + 微對比"},
    {"keys": {"night", "nighttime", "dark", "evening", "low light"},
     "slope": [0.97, 0.98, 1.02], "offset": [-0.004, -0.003, 0.002], "sat": 0.95,
     "note": "冷陰影 + 壓亮 + 降飽和"},
    {"keys": {"food", "meal", "dish", "restaurant", "dessert", "drink"},
     "slope": [1.02, 1.00, 0.98], "offset": [0.004, 0.001, -0.002], "sat": 1.10,
     "note": "暖 + 高飽和讓食物討喜"},
    {"keys": {"snow", "fog", "mist", "cloud", "overcast"},
     "slope": [1.00, 1.00, 1.01], "offset": [0.004, 0.004, 0.005], "sat": 0.97,
     "note": "抬灰階、留空氣感"},
]


def _match(labels: List[str]) -> Dict[str, Any] | None:
    low = {str(x).lower() for x in labels}
    for p in _PRESETS:
        if low & p["keys"]:
            return p
    return None


def build_grade_hints(storyboard: List[Dict[str, Any]],
                      photos_meta_path: str | None = None) -> Dict[str, Dict[str, Any]]:
    from ..util.photos_meta import load_photos_meta, meta_for

    meta = load_photos_meta(photos_meta_path)
    hints: Dict[str, Dict[str, Any]] = {}
    for shot in storyboard:
        rec = meta_for(shot, meta)
        labels = rec.get("labels") or []
        # 也吃 time-of-day：清晨/夜間標記
        taken = rec.get("taken") or ""
        if taken[11:13].isdigit():
            hr = int(taken[11:13])
            if hr <= 5 or hr >= 20:
                labels = list(labels) + ["night"]
            elif 5 < hr <= 7 or 17 <= hr < 20:
                labels = list(labels) + ["golden hour"]
        p = _match(labels)
        if not p:
            continue
        stem = Path(str(shot.get("file_path") or shot.get("media_file") or "")).stem
        hints[stem] = {
            "slope_mul": p["slope"], "offset_add": p["offset"], "sat_mul": p["sat"],
            "labels": labels[:6], "note": p["note"],
        }
    return hints


def write_grade_hints(storyboard: List[Dict[str, Any]], out_path: str,
                      photos_meta_path: str | None = None) -> int:
    hints = build_grade_hints(storyboard, photos_meta_path)
    Path(out_path).write_text(json.dumps(hints, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(hints)
