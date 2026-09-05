import os
import io
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from PIL import Image
from google import genai
from google.genai import types

from ..config import config
from .smart_crop import SmartCropAnalyzer


class VisionAnalyzer:
    """
    分析照片/影片的構圖、焦點、主體與景別，
    並針對目標輸出比例 (16:9, 9:16 等) 提供智慧裁切框與 Ken Burns 鏡頭動態建議。
    """

    def __init__(self, api_key: Optional[str] = None, use_ai: bool = False):
        self.api_key = api_key or config.gemini_api_key
        self.use_ai = use_ai
        self.client = None
        self.smart_crop = SmartCropAnalyzer()
        # 僅在顯式指定 use_ai=True 時才初始化 GenAI Client 進行多模態裁切
        if self.use_ai and self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[Warning] GenAI Client 初始化失敗: {e}")

    def analyze_media(self, media_item: Dict[str, Any], target_aspect_ratio: str = "16:9") -> Dict[str, Any]:
        """
        對單一媒體項目進行構圖裁切分析。
        預設使用本機 SmartCrop 顯著性演算法（省下 100% 圖片 Token）。
        """
        file_path = media_item["file_path"]
        caption = media_item.get("caption", "")
        media_type = media_item.get("media_type", "image")
        width = media_item.get("width", 1920)
        height = media_item.get("height", 1080)
        is_image = media_item.get("is_image", True)
        
        # 1. 僅在明確啟用 use_ai 時透過 Gemini 多模態 API 分析
        if self.use_ai and self.client and is_image:
            try:
                analysis = self._analyze_with_gemini(file_path, caption, target_aspect_ratio, media_item)
                if analysis:
                    return analysis
            except Exception as e:
                print(f"[Warning] Gemini 視覺分析失敗 ({file_path}): {e}，切換為 SmartCrop 顯著性演算法")

        # 2. SmartCrop 顯著性構圖與智慧裁切計算 (針對實際圖片進行邊緣與色彩能量檢測)
        if is_image and file_path and Path(file_path).exists():
            try:
                sc_res = self.smart_crop.analyze_crop(file_path, target_aspect_ratio=target_aspect_ratio, caption=caption)
                if media_type == "live_photo":
                    sc_res["live_photo_advice"] = "擷取前段動作最生動的 1.5~2 秒作為動態 B-Roll 分鏡"
                return sc_res
            except Exception as e:
                print(f"[Warning] SmartCrop 計算失敗 ({file_path}): {e}，使用啟發式幾何裁切")

        # 3. Fallback: 啟發式幾何構圖推算
        return self._heuristic_crop_analysis(width, height, target_aspect_ratio, caption, media_type)

    def _analyze_with_gemini(self, file_path: str, caption: str, target_aspect_ratio: str, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """利用 Gemini 多模態進行精準構圖、主體識別與裁切分析"""
        img = Image.open(file_path)
        # 縮小尺寸以加快上傳速度 (最大邊長 1280)
        img.thumbnail((1280, 1280))
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG', quality=85)
        img_bytes = img_byte_arr.getvalue()

        prompt = f"""
你是一位頂級電影導演與資深剪輯師。請分析這張照片的視覺構圖，並針對剪輯目標比例【{target_aspect_ratio}】提供取景與裁切建議。

照片資訊：
- 使用者回憶/說明備註："{caption}"
- 原圖尺寸：寬 {meta.get('width')} x 高 {meta.get('height')}
- 媒體類型：{meta.get('media_type')} (若是 Live Photo，代表有 1.5s~3s 動態)
- 拍攝地點：{meta.get('location', {}).get('short_location', '未知') if meta.get('location') else '未知'}

請回傳 JSON 格式（不要包含任何 markdown 外框以外的雜訊）：
{{
  "shot_type": "特寫 (Close-up) / 中景 (Medium Shot) / 全景 (Wide Shot) / 大特寫 (Extreme Close-up)",
  "visual_subject": "畫面主體詳細描述（例如：站在湖邊微笑的女孩、擺盤精緻的拉麵、日落餘暉下的神社）",
  "composition": "構圖特色（如：三分法則、中央對稱、引導線構圖、框架構圖）",
  "mood": "畫面氛圍（如：溫馨懷舊、歡樂明亮、平靜放鬆、壯闊震撼）",
  "crop_suggestion": {{
    "target_aspect_ratio": "{target_aspect_ratio}",
    "crop_box_normalized": [ymin, xmin, ymax, xmax],  // 範圍 0.0 到 1.0 的正規化座標
    "crop_reason": "裁切建議說明（如何保留主體黃金比例、避開干擾雜物）"
  }},
  "camera_motion_suggestion": {{
    "motion_type": "Static / Slow Zoom-in / Slow Zoom-out / Pan Left-to-Right / Pan Right-to-Left / Tilt Up / Tilt Down",
    "start_box": [ymin, xmin, ymax, xmax],
    "end_box": [ymin, xmin, ymax, xmax],
    "motion_speed": "Slow / Medium / Dynamic",
    "motion_description": "Ken Burns 鏡頭動態細節描述"
  }},
  "live_photo_advice": "如果是 Live Photo，建議如何剪輯這 2 秒動態（例如：保留水波流動、人物轉頭瞬間等；若非 Live Photo 則為 null）"
}}
"""

        response = self.client.models.generate_content(
            model=config.vision_model,
            contents=[
                types.Part.from_bytes(
                    data=img_bytes,
                    mime_type="image/jpeg",
                ),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        if response.text:
            try:
                result = json.loads(response.text)
                return result
            except json.JSONDecodeError:
                pass
        return None

    def _heuristic_crop_analysis(self, width: int, height: int, target_aspect_ratio: str, caption: str, media_type: str) -> Dict[str, Any]:
        """無網路/API 時的啟發式裁切與構圖推算"""
        w = max(width, 1)
        h = max(height, 1)
        orig_ratio = w / h

        # 解析目標比例
        if target_aspect_ratio == "16:9":
            target_ratio = 16.0 / 9.0
        elif target_aspect_ratio == "9:16":
            target_ratio = 9.0 / 16.0
        elif target_aspect_ratio == "4:3":
            target_ratio = 4.0 / 3.0
        elif target_aspect_ratio == "1:1":
            target_ratio = 1.0
        else:
            target_ratio = 16.0 / 9.0

        # 計算中央安全裁切框 (Normalized: 0.0 ~ 1.0)
        if orig_ratio > target_ratio:
            # 原圖較寬 -> 裁切左右兩側，高度全保留
            crop_w = target_ratio / orig_ratio
            xmin = (1.0 - crop_w) / 2.0
            xmax = xmin + crop_w
            ymin, ymax = 0.0, 1.0
        else:
            # 原圖較高 (例如直拍要轉 16:9) -> 裁切上下，保留中央略偏上 (人臉/主體通常在上方 1/3 ~ 2/3)
            crop_h = orig_ratio / target_ratio
            # 偏上 15% 取景以避免切到頭頂
            ymin = max(0.0, (1.0 - crop_h) * 0.35)
            ymax = min(1.0, ymin + crop_h)
            xmin, xmax = 0.0, 1.0

        crop_box = [round(ymin, 3), round(xmin, 3), round(ymax, 3), round(xmax, 3)]

        # 推算 Ken Burns 起始與結束框 (微幅推進 10%)
        start_box = crop_box
        scale_in = 0.9
        sh = (ymax - ymin) * scale_in
        sw = (xmax - xmin) * scale_in
        end_ymin = ymin + (ymax - ymin - sh) / 2.0
        end_xmin = xmin + (xmax - xmin - sw) / 2.0
        end_box = [round(end_ymin, 3), round(end_xmin, 3), round(end_ymin + sh, 3), round(end_xmin + sw, 3)]

        shot_type = "中景 (Medium Shot)" if "人" in caption or "我" in caption else "全景 (Wide Shot)"

        return {
            "shot_type": shot_type,
            "visual_subject": caption if caption else "主體畫面",
            "composition": "三分法則 / 黃金焦點",
            "mood": "溫馨自然",
            "crop_suggestion": {
                "target_aspect_ratio": target_aspect_ratio,
                "crop_box_normalized": crop_box,
                "crop_reason": f"針對 {target_aspect_ratio} 畫面進行安全置中與三分構圖微調"
            },
            "camera_motion_suggestion": {
                "motion_type": "Slow Zoom-in",
                "start_box": start_box,
                "end_box": end_box,
                "motion_speed": "Slow",
                "motion_description": "從全景緩慢平滑推近主體"
            },
            "live_photo_advice": "建議擷取動作最生動的 1.5 秒作為動態分鏡" if media_type == "live_photo" else None
        }
