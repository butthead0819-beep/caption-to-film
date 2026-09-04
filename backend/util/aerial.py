"""Google Aerial View API → 一段地點的電影感空拍環繞影片 (MP4)。

比自己拉衛星圖 Ken Burns 高級（真 3D 環繞），但：
  - 只有 Google 有 3D 影像的地區才有（美國為主、逐步擴張；台灣鄉間幾乎沒有，
    都市可能有）→ 拿不到就回 None，呼叫端退回衛星靜圖。
  - 影片是預先算好的固定環繞，不能自訂相機路徑。
  - render 可能要數分鐘；lookupVideo 命中現成的最快。

需要：GOOGLE_MAPS_API_KEY 或 GOOGLE_API_KEY，且該 key 的「API 限制」要放行
「Aerial View API」，且該 GCP 專案有啟用它。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

try:
    from ..config import config as _cfg  # noqa: F401  觸發 .env
except Exception:
    pass

_BASE = "https://aerialview.googleapis.com/v1"


def _key() -> Optional[str]:
    return os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _pick_mp4(payload: dict) -> Optional[str]:
    """從 lookupVideo 回應挑一個 landscape MP4 直連。"""
    uris = (payload.get("uris") or {})
    # 格式：{"<KEY>": {"landscapeUri": "...", "portraitUri": "..."}}
    for v in uris.values():
        if isinstance(v, dict) and v.get("landscapeUri"):
            return v["landscapeUri"]
    for v in uris.values():
        if isinstance(v, dict):
            for u in v.values():
                if isinstance(u, str) and ".mp4" in u:
                    return u
    return None


def aerial_video(address: str, out_path: str, *, poll_seconds: int = 0) -> Optional[str]:
    """抓 address 的空拍影片存到 out_path。

    poll_seconds>0：若尚未算好，觸發 renderVideo 並輪詢最多這麼久。
    回傳存檔路徑或 None（無此地區 3D 影像 / API 未開）。
    """
    key = _key()
    if not key:
        return None
    try:
        import requests
    except Exception:
        return None

    def lookup():
        r = requests.get(f"{_BASE}/videos:lookupVideo",
                         params={"key": key, "address": address}, timeout=25)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})

    sc, data = lookup()
    if sc == 403:
        return None  # API 未開 / key 被限制
    state = (data or {}).get("state")

    if state != "ACTIVE" and poll_seconds > 0:
        try:
            requests.post(f"{_BASE}/videos:renderVideo", params={"key": key},
                          json={"address": address}, timeout=25)
        except Exception:
            pass
        deadline = time.time() + poll_seconds
        while time.time() < deadline:
            time.sleep(15)
            sc, data = lookup()
            state = (data or {}).get("state")
            if state == "ACTIVE":
                break

    if state != "ACTIVE":
        return None

    url = _pick_mp4(data)
    if not url:
        return None
    try:
        vid = requests.get(url, timeout=120)
        vid.raise_for_status()
        Path(out_path).write_bytes(vid.content)
        return out_path
    except Exception:
        return None
