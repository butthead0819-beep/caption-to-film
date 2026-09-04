#!/usr/bin/env python3
"""gen_vo_gemini 之後的人工微調。每一條都標理由。改完 rebuild（不要 --regen-vo）。

用法：.venv/bin/python examples/reference-film/patch_vo.py --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent

# shot_index -> 新 voiceover 文字（"" = 刪這句改留白）。voiceover_kind 不動。
# 這裡放你人工覆寫的句子，例：
PATCH: dict[int, str] = {
    # 18: "改寫後的這一句。",   # 理由：原句比喻太重，旁白要平實
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    d = json.loads((ROOT / f"{PREFIX}.json").read_text("utf-8"))
    last_of: dict[int, int] = {}
    for k, s in enumerate(d["storyboard"]):
        last_of[s.get("shot_index")] = k

    hits = 0
    for k, s in enumerate(d["storyboard"]):
        i = s.get("shot_index")
        if i not in PATCH or last_of.get(i) != k:
            continue
        text = PATCH[i]
        old = s.get("voiceover", "")
        if not text:
            s["voiceover"] = ""
            s.pop("voiceover_kind", None)
            print(f"  #{i:>3} 刪  ← {old!r}")
        else:
            s["voiceover"] = text
            print(f"  #{i:>3} {text}   (was {old!r})")
        s.pop("timed_subtitles", None)
        hits += 1

    print(f"\n改了 {hits} 句")
    if not args.write:
        print("（預覽，加 --write）")
        return
    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    print("✅ 寫回 → rebuild（不要 --regen-vo）→ render_video.py")


if __name__ == "__main__":
    main()
