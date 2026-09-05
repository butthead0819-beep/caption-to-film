"""字幕命名 preset —— 把散在 subtitle_engine / fcpxml_exporter / render_video 的字幕
數值收成一份可調的 JSON。

換風格：在 repo 根放 `subtitle_preset.json`（或設 `SUBTITLE_PRESET` 指到別的檔）。
沒有就用下面的預設（= 目前寫死的值，中文 16:9 紀錄片）。

分兩區：
  timing —— subtitle_engine 用（切分 / 顯示秒數）
  style  —— fcpxml_exporter 的 <title> + render_video 燒字幕 共用（字體 / 描邊 / 位置）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS: dict = {
    "timing": {
        "reading_cps": 6.0,          # 中文舒適閱讀 字/秒
        "min_cue_sec": 1.2,          # 排版下限
        "min_readable_sec": 2.0,     # 看得懂的下限（絕不壓比這短）
        "max_cue_sec": 6.0,
        "overflow_tail_sec": 1.5,    # 可溢到鏡頭結束後多久
        "cue_gap_sec": 0.10,
        "max_chars_per_line": 16,    # 16:9；9:16 直式改 12
        "max_lines": 2,
    },
    "style": {
        "font": "PingFang TC",       # 或 思源黑體 / Noto Sans CJK TC
        "bold": True,
        "size_1080": 70,             # 1080p 字級（使用者定：44 太小 → 70）；其他解析度等比
        "fill": "FFFFFF",            # 場景註解（第一人稱故事書）+ 串接旁白 = 白
        "narration_fill": "F0DFA8",  # 舊「旁白軌」= 柔和暖金（相容用）
        "reflection_fill": "E8C88C",  # 感觸軌（第一人稱人生體悟，鋪在放長的畫布鏡頭）= 琥珀
        "reflection_size_mul": 1.18,  # 感觸字比一般大
        "reflection_center": True,    # 感觸水平置中，不擠在下緣跟資訊軌打架
        "reflection_y_frac": 0.80,    # 垂直位置（螢幕高比例）：畫面下方 ~20%，不擋畫面主體
        "outline": "000000",         # 黑描邊
        "outline_px_1080": 3,        # 中文筆劃密，別超過 4
        "shadow": True,
        "margin_v_frac": 0.06,       # 距畫面下緣（畫面高的比例）—— 置中靠下、字幕安全區內
        "box": False,               # 背景色塊（只有全白過曝畫面才開）
        # 右下角資訊軌（Day / 地名 / 海拔）—— 持續到該章結束、看得清楚但不搶畫面
        "meta_size_1080": 40,        # 使用者定：30 太小 → 40
        "meta_fill": "E8E8E8",       # 接近白、在半透明黑底上清楚
        "meta_alpha": 0.08,          # 文字自身透明度（0=實心）：幾乎實心
        "meta_box": True,            # 半透明黑色底框
        "meta_box_alpha": 0.45,      # 底框透明度（0.45 ≈ 55% 不透明黑）
        "meta_box_pad_1080": 12,     # 底框內距（px @1080）
        "meta_margin_frac": 0.035,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load() -> dict:
    p = Path(os.environ.get("SUBTITLE_PRESET") or (_ROOT / "subtitle_preset.json"))
    if p.exists():
        try:
            return _deep_merge(DEFAULTS, json.loads(p.read_text("utf-8")))
        except Exception:
            pass
    return DEFAULTS


PRESET = load()
TIMING = PRESET["timing"]
STYLE = PRESET["style"]
