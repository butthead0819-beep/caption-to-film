"""單一剪輯專案設定 — 所有 pipeline 腳本從這裡讀「素材夾」與「專案 prefix」。

設定一支影片，二選一：
  1. 在 repo 根目錄放 `edit_project.json`（複製 `edit_project.example.json`）：
       { "media_dir": "/path/to/影片素材夾", "prefix": "my_trip" }
  2. 設環境變數：EDIT_MEDIA_DIR=/path  EDIT_PREFIX=my_trip

沒設任何一個就會直接報錯。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_cfg: dict = {}
_cfg_file = ROOT / "edit_project.json"
if _cfg_file.exists():
    try:
        _cfg = json.loads(_cfg_file.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        _cfg = {}

_media = os.environ.get("EDIT_MEDIA_DIR") or _cfg.get("media_dir")
_prefix = os.environ.get("EDIT_PREFIX") or _cfg.get("prefix")

if not _media or not _prefix:
    sys.exit(
        "尚未設定剪輯專案。\n"
        "  cp edit_project.example.json edit_project.json  然後填 media_dir / prefix\n"
        "  或設環境變數 EDIT_MEDIA_DIR 與 EDIT_PREFIX"
    )

MEDIA_DIR = Path(_media).expanduser()
PREFIX = _prefix

PREPARED_DIR = MEDIA_DIR / "_prepared"        # 奇數尺寸圖修正副本
STABILIZED_DIR = MEDIA_DIR / "_stabilized"    # 畫面穩定副本
PHOTOS_META = ROOT / "scripts" / "photos_meta.json"

# fcpxml / srt / md 同時寫進「repo 根」與「素材夾」兩個地方
OUT_DIRS = [ROOT, MEDIA_DIR]
# 素材檔搜尋路徑（穩定版 > 修正版 > 原檔）
SEARCH_DIRS = [str(STABILIZED_DIR), str(PREPARED_DIR), str(MEDIA_DIR)]
