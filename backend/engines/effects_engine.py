"""自動 Ken Burns 運鏡 + 目標比例填滿 (framing) 引擎。

對每個鏡頭算出 `<adjust-transform>` 要用的 scale / position 起訖值：
  - 照片：用 SmartCrop 顯著性焦點決定推鏡方向；橫幅全景改用左右平移。
  - 影片：只做「填滿目標比例」的幾何縮放，預設不加動態。

輸出寫進 shot["ken_burns"] = {
  "type": "zoom" | "pan" | "static",
  "start": {"scale": float, "x": float, "y": float},   # x/y 為畫面寬高的比例 (-0.5~0.5)
  "end":   {"scale": float, "x": float, "y": float},
}
FCPXML 匯出器負責把 x/y 乘上序列寬高換成像素。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

_RATIO = {"16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0, "21:9": 21 / 9}


def _parse_ratio(s: str) -> float:
    return _RATIO.get(s, 16 / 9)


def compute_ken_burns(
    w: int,
    h: int,
    target_ratio: str = "16:9",
    focus: Tuple[float, float] = (0.5, 0.5),
    animate: bool = True,
    zoom_amount: float = 0.14,
) -> Dict:
    """依素材原始寬高 + 目標比例 + 焦點 (cy, cx) 算出 Ken Burns 起訖。"""
    ratio = _parse_ratio(target_ratio)
    a = (w / max(h, 1)) or ratio
    fcy, fcx = focus

    # 填滿目標比例所需的基礎縮放，以及素材貼齊畫面後佔畫面的寬高比例
    if a >= ratio:                       # 素材較寬 → 左右會被裁
        frac_w, frac_h = 1.0, ratio / a
        s_fill = a / ratio
        fill_bw, fill_bh = ratio / a, 1.0
    else:                                # 素材較高 → 上下會被裁
        frac_w, frac_h = a / ratio, 1.0
        s_fill = ratio / a
        fill_bw, fill_bh = 1.0, a / ratio

    # 比例落差過大 (例如直式素材塞進橫式時間軸)：硬填會放到極糊，
    # 改成「貼齊」不裁切，只給極輕微的推鏡。
    fit_mode = s_fill > 1.8
    base_scale = 1.0 if fit_mode else s_fill
    base_bw = 1.0 if fit_mode else fill_bw

    def place(box_cx: float, box_cy: float, box_w: float) -> Dict:
        scale = base_scale * (base_bw / box_w)
        x = -(box_cx - 0.5) * frac_w * scale + 0.0
        y = (box_cy - 0.5) * frac_h * scale + 0.0
        return {"scale": round(scale, 4), "x": round(x, 4) + 0.0, "y": round(y, 4) + 0.0}

    if not animate:
        st = place(0.5, 0.5, base_bw)
        return {"type": "static", "start": st, "end": dict(st), "fit_mode": fit_mode}

    wideness = a / ratio if a >= ratio else h / w * ratio if h > w else 0.0
    if not fit_mode and wideness >= 1.35 and base_bw <= 0.8:
        # 橫幅全景 → 左右平移 (箱寬維持填滿寬度，只移動中心)
        start = place(base_bw / 2, 0.5, base_bw)
        end = place(1.0 - base_bw / 2, 0.5, base_bw)
        return {"type": "pan", "start": start, "end": end, "fit_mode": fit_mode}

    # 一般照片 → 從中景緩慢推近焦點
    start_w = base_bw / (1.0 + zoom_amount * 0.35)
    end_w = base_bw / (1.0 + zoom_amount)
    cx = min(max(fcx, end_w / 2), 1.0 - end_w / 2)
    cy = min(max(fcy, end_w / 2), 1.0 - end_w / 2)
    start = place(0.5, 0.5, start_w)
    end = place(cx, cy, end_w)
    return {"type": "zoom", "start": start, "end": end, "fit_mode": fit_mode}


def apply_effects(
    storyboard: List[dict],
    search_dirs: Optional[List[str]] = None,
    target_ratio: str = "16:9",
    animate_photos: bool = True,
    animate_videos: bool = False,
    photos_meta_path: Optional[str] = None,
) -> List[str]:
    """就地為每個鏡頭補上 ken_burns 設定。回傳異動摘要。

    有 photos_meta.json 時：照片若有 Apple 人臉框，推鏡焦點直接對準人臉重心
    （比顯著性演算法猜得準）。
    """
    from ..analyzers.smart_crop import SmartCropAnalyzer
    from ..util.media_probe import probe_image, probe_video, resolve_existing_path, is_video_path
    from ..util.photos_meta import load_photos_meta, meta_for, face_center

    meta = load_photos_meta(photos_meta_path)
    n_face_focus = 0

    # 批次用途：低取樣 (焦點/平移不需要高精度) + 逐檔快取，速度快數十倍
    sc = SmartCropAnalyzer(sample_size=220)
    focus_cache: Dict[str, Tuple[float, float]] = {}
    changes: List[str] = []

    for idx, shot in enumerate(storyboard, start=1):
        if shot.get("skip"):
            continue
        raw = shot.get("file_path") or shot.get("media_file") or ""
        resolved = resolve_existing_path(str(raw), search_dirs)
        if not resolved:
            continue

        is_vid = is_video_path(resolved)
        focus: Tuple[float, float] = (0.5, 0.5)
        motion_label = "Static"

        if is_vid:
            info = probe_video(str(resolved))
            w, h = info["width"], info["height"]
            animate = animate_videos
        else:
            info = probe_image(str(resolved))
            w, h = info["width"], info["height"]
            animate = animate_photos
            key = str(resolved)
            if key not in focus_cache:
                fc_face = face_center(meta_for(shot, meta))
                try:
                    a_ = sc.analyze_crop(key, target_ratio, "")
                    fc = a_.get("focal_center_normalized") or (0.5, 0.5)
                    mlabel = a_.get("camera_motion_suggestion", {}).get("motion_type", "Slow Zoom-in")
                except Exception:
                    fc, mlabel = (0.5, 0.5), "Slow Zoom-in"
                if fc_face is not None:
                    fc = fc_face            # Apple 人臉框 > 顯著性猜測
                    n_face_focus += 1
                focus_cache[key] = ((float(fc[0]), float(fc[1])), mlabel)
            focus, motion_label = focus_cache[key]

        # 顯著性 / 人臉焦點（cy, cx）→ 存成 (cx, cy) 給 render_video 的 framing 對焦裁切用，
        # 不然 4:3 照片一律置中裁，人的頭常被切掉
        if not is_vid:
            shot["kb_focus"] = [round(float(focus[1]), 3), round(float(focus[0]), 3)]

        kb = compute_ken_burns(w, h, target_ratio, focus=focus, animate=animate)
        s = kb["start"]
        is_identity = (
            kb["type"] == "static"
            and abs(s["scale"] - 1.0) < 0.02 and abs(s["x"]) < 1e-3 and abs(s["y"]) < 1e-3
        )
        if is_identity:
            shot.pop("ken_burns", None)
            shot["camera_motion"] = "Static"
            continue
        shot["ken_burns"] = kb
        shot["camera_motion"] = motion_label if kb["type"] != "static" else "Static"

        if kb["type"] != "static":
            changes.append(
                f"Shot {idx:02d} ({Path(str(resolved)).name}): {kb['type']} "
                f"scale {kb['start']['scale']}→{kb['end']['scale']}"
            )
        elif abs(kb["start"]["scale"] - 1.0) > 0.02:
            changes.append(
                f"Shot {idx:02d} ({Path(str(resolved)).name}): 縮放填滿 {kb['start']['scale']}x"
            )
    if n_face_focus:
        changes.append(f"（其中 {n_face_focus} 張用 Apple 人臉框對焦）")
    return changes
