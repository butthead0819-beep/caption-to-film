"""影片低頻取樣與多模態分析模組 (Video Low-Frequency Sampler & Multimodal Analyzer)

針對大量長影片素材，避免直接上傳 raw 影片至 LLM 造成龐大 Token 消耗（Gemini 原生預設 1 fps，
1 小時約 100 萬 tokens）。

本模組在本地端以 ffmpeg 進行按需降頻抽幀（預設每 10 秒 1 幀，0.1 fps），將畫面壓縮為低解析度 JPEG 影格，
附帶時間戳送入 Gemini 多模態 API。可節省 90% 以上 Token 與 API 費用，並取得：
- 影片整體事件描述 (summary)
- 精華推薦瞬間與理由 (best_highlight)
- 場景標籤 (scene_labels, 對齊 grading_engine)
- 情感氛圍 (mood)
- 是否有人物入鏡 (has_people)
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 對齊 grading_engine._PRESETS + abroll/highlight 用得到的場景詞表
DEFAULT_VOCAB = (
    "sunrise sunset dusk dawn golden-hour daylight night "
    "beach ocean sea coast wave lake river reflection waterfall harbor "
    "forest tree grass field rice mountain hill valley cliff "
    "road highway bridge tunnel "
    "urban street building architecture sign temple shrine market "
    "food meal drink dessert "
    "snow fog mist cloud rain "
    "bicycle vehicle train "
    "portrait selfie group-photo people crowd animal dog "
    "indoor vehicle-interior sky panorama"
)


def get_video_duration(video_path: Path | str) -> float:
    """透過 ffprobe 取得影片時長（秒）。若探測失敗則回傳 0.0。"""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return float(res.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def compute_sample_timestamps(duration_s: float, interval_s: float = 10.0) -> List[float]:
    """計算取樣時間戳清單（秒）。
    
    規則：
    - 時長 <= 0：回傳空清單 []
    - 短片（時長 <= interval_s）：只取正中間 1 幀 [round(dur / 2.0, 2)]
    - 長片（時長 > interval_s）：
      從 0.0 開始每隔 interval_s 取 1 幀（例如 0.0, 10.0, 20.0...），
      每個取樣點覆蓋一個區間，確保全片無遺漏且不產生冗餘影格。
    """
    dur = float(duration_s)
    if dur <= 0:
        return []
    
    # 影片短於或等於一個抽樣週期：直接抽中點單幀
    if dur <= interval_s:
        return [round(dur / 2.0, 2)]

    points: List[float] = []
    curr = 0.0
    while curr < dur:
        points.append(round(curr, 2))
        curr += interval_s

    return points


def extract_frame_bytes(
    video_path: Path | str,
    timestamp_s: float,
    max_dimension: int = 1024,
    jpeg_quality: int = 85,
) -> Optional[bytes]:
    """在本地端使用 ffmpeg 抽取單一影格，並縮放至指定尺寸以節省頻寬與 Token。
    回傳 JPEG bytes；失敗回傳 None。
    """
    p = Path(video_path)
    if not p.exists():
        return None

    # scale='min(1024,iw)':-2 保持原始寬高比並確保偶數邊長
    scale_filter = f"scale='min({max_dimension},iw)':-2"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-ss",
        str(max(0.0, timestamp_s)),
        "-i",
        str(p),
        "-frames:v",
        "1",
        "-vf",
        scale_filter,
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        str(max(2, min(31, int((100 - jpeg_quality) / 3)))),  # mjpeg quality scale
        "-",
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
        return res.stdout if res.stdout else None
    except Exception:
        return None


def extract_sampled_frames(
    video_path: Path | str,
    interval_s: float = 10.0,
    max_dimension: int = 1024,
) -> List[Tuple[float, bytes]]:
    """依據間隔抽取影片的序列影格。
    回傳清單：[(時間戳秒數, jpeg_bytes), ...]
    """
    dur = get_video_duration(video_path)
    if dur <= 0:
        return []

    timestamps = compute_sample_timestamps(dur, interval_s=interval_s)
    results: List[Tuple[float, bytes]] = []

    for ts in timestamps:
        b = extract_frame_bytes(video_path, ts, max_dimension=max_dimension)
        if b:
            results.append((ts, b))

    return results


def estimate_token_savings(duration_s: float, num_sampled_frames: int) -> Dict[str, Any]:
    """計算對比官方 1 fps 原生影片直接上傳的 Token 與費用節省估算。
    
    官方標準 (Gemini API)：
    - 影片原生直傳解碼為 1 fps，每秒約 258 tokens。
    - 單張靜態影格約 258 tokens。
    """
    dur = max(0.0, float(duration_s))
    raw_1fps_tokens = int(round(dur * 258))
    sampled_tokens = int(num_sampled_frames * 258)
    
    saved_tokens = max(0, raw_1fps_tokens - sampled_tokens)
    pct_saved = (saved_tokens / raw_1fps_tokens * 100.0) if raw_1fps_tokens > 0 else 0.0

    return {
        "duration_s": round(dur, 2),
        "sampled_frames_count": num_sampled_frames,
        "estimated_sampled_tokens": sampled_tokens,
        "estimated_raw_1fps_tokens": raw_1fps_tokens,
        "saved_tokens": saved_tokens,
        "saved_percentage": round(pct_saved, 1),
    }


def analyze_video_sampled(
    client: Any,
    video_path: Path | str,
    interval_s: float = 10.0,
    max_dimension: int = 1024,
    model: str = "gemini-2.5-flash",
    vocab: str = DEFAULT_VOCAB,
) -> Dict[str, Any]:
    """抽取影片 10 秒 1 幀的多圖序列並呼叫 Gemini 進行多模態時間軸分析。"""
    from google.genai import types

    p = Path(video_path)
    dur = get_video_duration(p)
    frames = extract_sampled_frames(p, interval_s=interval_s, max_dimension=max_dimension)
    
    savings = estimate_token_savings(dur, len(frames))

    if not frames:
        return {
            "file": str(p),
            "error": "無法從影片提取影格",
            "token_savings": savings,
        }

    # 組裝 Prompt 內容
    contents: List[Any] = []
    contents.append(
        f"你是一位專業紀錄片剪輯師。這是一段名為《{p.name}》的影片（總長約 {dur:.1f} 秒）。\n"
        f"我們依據時間軸抽取了以下關鍵影格序列：\n"
    )

    for ts, b in frames:
        mins = int(ts // 60)
        secs = ts % 60
        contents.append(f"【影格時間碼 {mins:02d}:{secs:04.1f} ({ts:.1f}s)】")
        contents.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))

    prompt_instruction = f"""
請仔細觀察上述影格的時間變化與人物動作，回傳嚴格的 JSON 格式（不要包含額外 markdown 外框）：
{{
  "summary": "一句話總結這段影片發生的主要事件與動作情節",
  "best_highlight": {{
    "start_sec": 0.0,
    "end_sec": 3.0,
    "reason": "為什麼這幾秒是全片最精華、最值得剪進正片或做成 B-Roll 的理由"
  }},
  "scene_labels": ["最多 5 個，優先從此詞表挑選，可加 1~2 個表內沒有的英文小寫詞: {vocab}"],
  "mood": "一個中文氛圍詞（如 溫馨/壯闊/寧靜/歡樂/疲憊/緊張）",
  "has_people": true/false,
  "time_of_day": "dawn|morning|noon|afternoon|golden-hour|dusk|night|unknown"
}}
"""
    contents.append(prompt_instruction)

    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )

    result_data = {}
    if resp.text:
        try:
            # 清理可能的 markdown 外框
            clean_text = resp.text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean_text = "\n".join(lines).strip()
            result_data = json.loads(clean_text)
        except Exception as e:
            result_data = {"raw_response": resp.text, "parse_error": str(e)}

    result_data["file"] = str(p)
    result_data["duration_s"] = round(dur, 2)
    result_data["token_savings"] = savings
    return result_data
