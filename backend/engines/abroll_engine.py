"""A/B-roll 角色分類：決定每個鏡頭是 A-roll (扛敘事) 還是 B-roll (補畫面)。

見 `.claude/skills/film-edit/references/montage-and-abroll.md`。

規則（已有 shot["role"] 就尊重不覆蓋）：
  A-roll  有旁白，或 scene_title/visual 明顯是人物同步聲鏡頭
  B-roll  無旁白的風景 / 空景 / 細節 / Live Photo 定格 / 蒙太奇標記鏡頭

exporter 的 abroll 模式會把「A-roll 旁白唸完前的連續 B-roll」疊成 lane-1 靜音 connected clip。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

# Apple ML 標籤 → 角色傾向
_ML_AROLL = {"people", "person", "face", "selfie", "portrait", "child", "baby", "dog", "pet",
             "eating", "food", "meal", "hand", "smile"}
_ML_BROLL = {"sky", "cloud", "sunset", "sunrise", "landscape", "mountain", "ocean", "sea",
             "beach", "coast", "lake", "river", "field", "road", "highway", "horizon",
             "scenery", "panorama", "building", "architecture", "sign", "street"}

# A/B-roll 的關鍵觀念：旁白（在本專案 = 字幕）可以蓋在 B-roll 上，所以「有沒有旁白」
# 不是分類依據。A-roll = 有人物 / 同步聲 / 關鍵動作的鏡頭；B-roll = 風景 / 空景 / 細節 / 定格。
_AROLL_HINTS = (
    "自拍", "合照", "合影", "對望", "訪談", "說話", "採訪", "受訪", "面對鏡頭", "打招呼",
    "身影", "踩動", "踩踏", "換胎", "補胎", "拆輪", "動手", "喝", "吃", "享用", "咬",
    "微笑", "帥氣", "表情", "特寫", "臉", "人物", "兩人",
)
_BROLL_HINTS = (
    "蒙太奇", "b-roll", "空景", "全景", "遠景", "壯闊", "epic", "wide", "倒影", "波光",
    "風景", "街景", "招牌", "牌樓", "朝霞", "夕陽", "晨光", "稻田", "山脈", "湖水", "海景",
    "公路", "地標", "屋頂", "光線灑", "延伸至", "天空",
)


def _classify_one(s: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
    if s.get("role") in ("a-roll", "b-roll"):
        return s["role"]

    # metadata 最準：有入鏡人物 / 人臉 → A-roll
    if meta:
        if meta.get("persons") or meta.get("faces") or meta.get("has_people"):
            return "a-roll"
        labels = {str(x).lower() for x in (meta.get("labels") or [])}
        if labels & _ML_AROLL and not (labels & _ML_BROLL):
            return "a-roll"
        if labels & _ML_BROLL and not (labels & _ML_AROLL):
            return "b-roll"

    text = " ".join(
        str(s.get(k, "")) for k in
        ("scene_title", "visual_action", "visual_description", "shot_type", "visual_subject")
    ).lower()
    a = sum(h in text for h in _AROLL_HINTS)
    b = sum(h in text for h in _BROLL_HINTS)
    if a > b:
        return "a-roll"
    if b > a:
        return "b-roll"
    if s.get("media_type") in ("video", "live_photo") or not s.get("is_image", True):
        return "a-roll"
    return "b-roll"


def classify_roles(storyboard: List[Dict[str, Any]],
                   photos_meta_path: Optional[str] = None) -> Dict[str, int]:
    from ..util.photos_meta import load_photos_meta
    meta = load_photos_meta(photos_meta_path)
    counts = {"a-roll": 0, "b-roll": 0}
    for s in storyboard:
        stem = Path(str(s.get("file_path") or s.get("media_file") or "")).stem.lower()
        role = _classify_one(s, meta.get(stem))
        s["role"] = role
        counts[role] += 1
    return counts
