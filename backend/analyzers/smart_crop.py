from pathlib import Path
import math
from typing import Dict, Any, Tuple, List, Optional
from PIL import Image, ImageFilter, ImageOps, ImageStat
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


class SmartCropAnalyzer:
    """
    純 Python / Pillow 顯著性影像智慧裁切與構圖分析器 (Smart Crop)
    
    原理：
    1. 邊緣能量分析 (Edge Gradient Energy)：透過高通濾波/邊緣檢測計算高頻細節分佈。
    2. 色彩與主體飽和度分析 (Color & Saliency Energy)：識別人眼偏好的色彩聚集與主體特徵。
    3. 三分法則與黃金焦點加權 (Rule-of-Thirds & Center Prior)：向經典構圖區域賦予加權。
    4. 最佳視窗掃描 (Optimal Window Search)：計算符合目標比例 (16:9, 9:16, 4:3, 1:1) 下能量總和最高之安全裁切框。
    5. 智慧 Ken Burns 平移推進起訖計算：根據主體焦點產生流暢平滑的鏡頭動態軌跡。
    """

    def __init__(self, sample_size: int = 400):
        self.sample_size = sample_size

    def analyze_crop(
        self,
        image_or_path: Any,
        target_aspect_ratio: str = "16:9",
        caption: str = ""
    ) -> Dict[str, Any]:
        """
        輸入圖片（PIL Image 或檔案路徑），輸出目標比例之最佳裁切框與 Ken Burns 建議
        """
        if isinstance(image_or_path, (str, Path, bytes)):
            img = Image.open(str(image_or_path))
        else:
            img = image_or_path

        orig_w, orig_h = img.size
        orig_ratio = orig_w / max(orig_h, 1)
        target_ratio = self._parse_aspect_ratio(target_aspect_ratio)

        # 縮小以進行高效能量計算
        scale = self.sample_size / max(orig_w, orig_h)
        small_w = max(int(orig_w * scale), 16)
        small_h = max(int(orig_h * scale), 16)
        
        # 轉換成 RGB 模式
        if img.mode != "RGB":
            rgb_img = img.convert("RGB")
        else:
            rgb_img = img
        small_img = rgb_img.resize((small_w, small_h), Image.Resampling.BILINEAR)

        # 1. 計算邊緣能量圖
        gray = small_img.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        
        # 2. 計算主體飽和度/膚色區域加權
        saliency_map = self._compute_saliency_map(small_img, edges, small_w, small_h)

        # 3. 搜尋最佳裁切框 (在小圖座標下搜尋，轉換為正規化 0.0 ~ 1.0)
        best_crop_norm = self._find_optimal_crop_box(saliency_map, small_w, small_h, orig_ratio, target_ratio)

        # 4. 推算焦點中心點 (Focal Center)
        focal_center = self._compute_focal_center(saliency_map, small_w, small_h)

        # 5. 計算 Ken Burns 運鏡起訖 (Start Box -> End Box)
        camera_motion = self._generate_ken_burns_motion(best_crop_norm, focal_center, target_aspect_ratio, caption)

        # 構圖與景別判斷
        shot_type = self._estimate_shot_type(orig_w, orig_h, best_crop_norm, caption)
        composition_desc = self._describe_composition(best_crop_norm, focal_center)

        return {
            "shot_type": shot_type,
            "visual_subject": caption if caption else "畫面視覺主體",
            "composition": composition_desc,
            "mood": "自然流暢",
            "crop_suggestion": {
                "target_aspect_ratio": target_aspect_ratio,
                "crop_box_normalized": best_crop_norm,  # [ymin, xmin, ymax, xmax]
                "crop_reason": f"SmartCrop 顯著性構圖分析：鎖定畫面主體並針對 {target_aspect_ratio} 裁切消除邊緣干擾"
            },
            "camera_motion_suggestion": camera_motion,
            "focal_center_normalized": focal_center
        }

    def _parse_aspect_ratio(self, ratio_str: str) -> float:
        mapping = {
            "16:9": 16.0 / 9.0,
            "9:16": 9.0 / 16.0,
            "4:3": 4.0 / 3.0,
            "3:4": 3.0 / 4.0,
            "1:1": 1.0,
            "21:9": 21.0 / 9.0
        }
        return mapping.get(ratio_str, 16.0 / 9.0)

    def _compute_saliency_map(
        self,
        img: Image.Image,
        edges: Image.Image,
        w: int,
        h: int
    ) -> List[List[float]]:
        """建立 2D 顯著性熱力圖（結合邊緣能量 + 色彩飽和度 + 構圖偏置）"""
        import numpy as np

        edge = np.asarray(edges, dtype=np.float32) / 255.0
        sat = np.asarray(img.convert("HSV").split()[1], dtype=np.float32) / 255.0
        if edge.shape != (h, w):
            edge = edge.reshape(h, w)
        if sat.shape != (h, w):
            sat = sat.reshape(h, w)

        ny = np.arange(h, dtype=np.float32) / float(h)
        nx = np.arange(w, dtype=np.float32) / float(w)
        y_bias = 1.0 - 0.5 * np.abs(ny - 0.4)   # 偏好人臉/三分線 (y ~0.33~0.6)
        x_bias = 1.0 - 0.4 * np.abs(nx - 0.5)   # 偏好中心與左右三分線
        weight = np.outer(y_bias, x_bias)

        return (edge * 0.65 + sat * 0.35) * weight

    def _find_optimal_crop_box(
        self,
        saliency: List[List[float]],
        w: int,
        h: int,
        orig_ratio: float,
        target_ratio: float
    ) -> List[float]:
        """滑動視窗搜尋在目標長寬比下的最高能量裁切框"""
        # 決定裁切框在正規化下的寬高
        if orig_ratio > target_ratio:
            # 原圖比目標更寬 (例如 16:9 原圖要轉 9:16，或 4:3 轉 1:1)
            # 高度全保留 (1.0)，寬度縮小為 target_ratio / orig_ratio
            box_h_norm = 1.0
            box_w_norm = min(1.0, target_ratio / orig_ratio)
        else:
            # 原圖比目標更高 (例如 9:16 直拍要轉 16:9)
            # 寬度全保留 (1.0)，高度縮小為 orig_ratio / target_ratio
            box_w_norm = 1.0
            box_h_norm = min(1.0, orig_ratio / target_ratio)

        import numpy as np

        box_px_w = max(1, int(box_w_norm * w))
        box_px_h = max(1, int(box_h_norm * h))

        sal = np.asarray(saliency, dtype=np.float64)
        integral = np.zeros((h + 1, w + 1), dtype=np.float64)
        integral[1:, 1:] = sal.cumsum(axis=0).cumsum(axis=1)

        step_x = max(1, (w - box_px_w) // 30) if w > box_px_w else 1
        step_y = max(1, (h - box_px_h) // 30) if h > box_px_h else 1
        ys = np.arange(0, h - box_px_h + 1, step_y)
        xs = np.arange(0, w - box_px_w + 1, step_x)

        e = (integral[np.ix_(ys + box_px_h, xs + box_px_w)]
             - integral[np.ix_(ys, xs + box_px_w)]
             - integral[np.ix_(ys + box_px_h, xs)]
             + integral[np.ix_(ys, xs)])
        iy, ix = np.unravel_index(int(np.argmax(e)), e.shape)
        best_y = int(ys[iy])
        best_x = int(xs[ix])

        ymin = round(best_y / float(h), 3)
        xmin = round(best_x / float(w), 3)
        ymax = round(min(1.0, ymin + box_h_norm), 3)
        xmax = round(min(1.0, xmin + box_w_norm), 3)

        return [ymin, xmin, ymax, xmax]

    def _compute_focal_center(self, saliency: List[List[float]], w: int, h: int) -> Tuple[float, float]:
        """計算影像能量重心的正規化座標 (cy, cx)"""
        import numpy as np

        sal = np.asarray(saliency, dtype=np.float64)
        total_energy = float(sal.sum())
        if total_energy <= 0.0001:
            return (0.5, 0.5)

        weighted_y = float((sal.sum(axis=1) * np.arange(h)).sum())
        weighted_x = float((sal.sum(axis=0) * np.arange(w)).sum())
        cx = round((weighted_x / total_energy) / float(w), 3)
        cy = round((weighted_y / total_energy) / float(h), 3)
        return (cy, cx)

    def _generate_ken_burns_motion(
        self,
        crop_box: List[float],
        focal_center: Tuple[float, float],
        target_aspect_ratio: str,
        caption: str
    ) -> Dict[str, Any]:
        """
        根據焦點中心與安全框推算流暢的 Ken Burns 起訖運鏡
        """
        ymin, xmin, ymax, xmax = crop_box
        box_w = xmax - xmin
        box_h = ymax - ymin
        fcy, fcx = focal_center

        # 縮放比例 15% (Zoom factor)
        zoom_factor = 0.85
        zoomed_w = box_w * zoom_factor
        zoomed_h = box_h * zoom_factor

        # 針對焦點進行聚焦框計算
        target_center_x = max(xmin + zoomed_w / 2.0, min(xmax - zoomed_w / 2.0, fcx))
        target_center_y = max(ymin + zoomed_h / 2.0, min(ymax - zoomed_h / 2.0, fcy))

        focused_box = [
            round(max(ymin, target_center_y - zoomed_h / 2.0), 3),
            round(max(xmin, target_center_x - zoomed_w / 2.0), 3),
            round(min(ymax, target_center_y + zoomed_h / 2.0), 3),
            round(min(xmax, target_center_x + zoomed_w / 2.0), 3)
        ]

        # 預設為全景推近 (Slow Zoom-in)
        motion_type = "Slow Zoom-in"
        start_box = crop_box
        end_box = focused_box
        motion_desc = "從安全全景緩慢向核心焦點推近 (Slow Zoom-in)"

        # 若原圖非常寬，則採左右平移 (Pan Left-to-Right)
        if box_w > 0.8 and (box_w / max(box_h, 0.01)) >= 1.6:
            motion_type = "Pan Left-to-Right"
            pan_w = box_w * 0.88
            start_box = [ymin, xmin, ymax, round(xmin + pan_w, 3)]
            end_box = [ymin, round(xmax - pan_w, 3), ymax, xmax]
            motion_desc = "鏡頭從左向右緩慢平移掃視整體風景 (Pan Left-to-Right)"

        return {
            "motion_type": motion_type,
            "start_box": start_box,
            "end_box": end_box,
            "motion_speed": "Slow",
            "motion_description": motion_desc
        }

    def _estimate_shot_type(self, w: int, h: int, crop_box: List[float], caption: str) -> str:
        box_area = (crop_box[2] - crop_box[0]) * (crop_box[3] - crop_box[1])
        if any(k in caption for k in ("特寫", "臉", "眼神", "表情", "近看")):
            return "特寫 (Close-up)"
        if box_area < 0.35:
            return "特寫 (Close-up)"
        elif box_area < 0.7:
            return "中景 (Medium Shot)"
        else:
            return "全景 (Wide Shot)"

    def _describe_composition(self, crop_box: List[float], focal_center: Tuple[float, float]) -> str:
        fcy, fcx = focal_center
        if abs(fcx - 0.5) < 0.08:
            return "中央對稱 / 焦點置中構圖"
        elif fcx < 0.45:
            return "左側三分法則黃金構圖"
        elif fcx > 0.55:
            return "右側三分法則黃金構圖"
        return "黃金比例焦點構圖"
