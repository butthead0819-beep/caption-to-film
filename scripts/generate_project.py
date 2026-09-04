#!/usr/bin/env python3
"""從素材夾 + 心得長文，用 Gemini 編一份新的 storyboard（`<prefix>.json`）。

= `cli.py` 的編劇部分，但**構圖分析不打 Gemini**（VisionAnalyzer 傳空 key → 只用 SmartCrop），
只有「storyboard 生成」那一次呼叫 Gemini，省額度。

用法：
  .venv/bin/python scripts/generate_project.py --reflection "<素材夾>/reflection.md" \
      --prompt "一支溫馨的家庭旅行紀錄片" --duration 540
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402
from backend.analyzers.vision_analyzer import VisionAnalyzer  # noqa: E402
from backend.engines.script_engine import ScriptEngine  # noqa: E402
from backend.exporters.json_exporter import JSONExporter  # noqa: E402
from backend.exporters.markdown_exporter import MarkdownExporter  # noqa: E402
from backend.extractors.geocoding import ReverseGeocoder  # noqa: E402
from backend.extractors.livephoto_matcher import LivePhotoMatcher  # noqa: E402
from backend.extractors.metadata_extractor import MetadataExtractor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reflection", default=None, help="心得長文 .md/.txt（省略則在素材夾內自動找）")
    ap.add_argument("--prompt", default="一支寫實、有情感、不煽情的個人紀錄片")
    ap.add_argument("--style", default="生活紀錄片")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--duration", type=int, default=None, help="目標片長（秒）；省略讓 Gemini 決定")
    args = ap.parse_args()

    ref = None
    rp = Path(args.reflection) if args.reflection else None
    if not rp:
        for p in MEDIA_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".md", ".txt") and any(
                    k in p.name.lower() for k in ("心得", "reflection", "story", "感想")):
                rp = p
                break
    if rp and rp.exists():
        ref = rp.read_text("utf-8").strip()
        print(f"📖 心得長文：{rp.name}（{len(ref)} 字）")

    print("🔍 掃描素材 + 配對 Live Photo …")
    geo = ReverseGeocoder()
    matcher = LivePhotoMatcher(metadata_extractor=MetadataExtractor(), geocoder=geo)
    items = matcher.process_directory(str(MEDIA_DIR), resolve_location=False)  # 地名改從 photos_meta 補（快、有快取）
    print(f"   {len(items)} 個多媒體項目")

    # 從 photos_meta 補地名（probe_folder_metadata --geocode 已填、有磁碟快取）
    try:
        from backend.util.photos_meta import load_photos_meta
        pm = load_photos_meta()
        filled = 0
        for it in items:
            stem = Path(it.get("file_path") or it.get("file_name") or "").stem.lower()
            place = (pm.get(stem) or {}).get("place")
            nm = place.get("name") if isinstance(place, dict) else None
            if nm and not it.get("location"):
                it["location"] = {"short_location": nm, "full_location": nm}
                filled += 1
        print(f"   補地名 {filled} 個（來自 photos_meta）")
    except Exception as e:  # noqa: BLE001
        print(f"   地名補充略過：{e}")

    print("📐 構圖分析（SmartCrop，不打 Gemini）…")
    va = VisionAnalyzer(api_key="")
    va.client = None                 # ← 一定要，VisionAnalyzer("") 會 fallthrough 到 config key
    analyzed = []
    for it in items:
        d = dict(it)
        d["analysis"] = va.analyze_media(it, target_aspect_ratio=args.ratio)
        analyzed.append(d)

    print("✍️  Gemini 編劇（1 次呼叫）…")
    se = ScriptEngine()
    if not se.client:
        sys.exit("沒有 Gemini client（缺 GEMINI_API_KEY / GOOGLE_API_KEY）")
    script = se.generate_script(
        media_items_with_analysis=analyzed,
        user_prompt=args.prompt,
        target_duration_seconds=args.duration,
        style=args.style,
        target_aspect_ratio=args.ratio,
        story_context=ref,
    )
    n = len(script.get("storyboard", []))
    if n == 0:
        sys.exit("storyboard 空的 —— Gemini 可能失敗或額度用完")
    print(f"✅ 「{script.get('project_title')}」／{n} 鏡頭")

    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(JSONExporter().export(script), "utf-8")
        (base / f"{PREFIX}_分鏡腳本.md").write_text(MarkdownExporter().export(script), "utf-8")
    print(f"→ 寫入 {PREFIX}.json（專案 + 素材夾）。接著跑 rebuild_all_projects.py")


if __name__ == "__main__":
    main()
