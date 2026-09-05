#!/usr/bin/env python3
"""
iMovie Script & Storyboard Generator - CLI 工具
用法範例:
  python cli.py --input /path/to/photos --prompt "京都旅行賞楓 Vlog" --style "溫馨感人" --ratio "16:9" --output script.md
"""

import os
import sys
import argparse
from pathlib import Path

# 將當前路徑加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.extractors.metadata_extractor import MetadataExtractor
from backend.extractors.geocoding import ReverseGeocoder
from backend.extractors.livephoto_matcher import LivePhotoMatcher
from backend.extractors.apple_photos_extractor import ApplePhotosExtractor
from backend.analyzers.vision_analyzer import VisionAnalyzer
from backend.engines.script_engine import ScriptEngine
from backend.exporters.markdown_exporter import MarkdownExporter
from backend.exporters.json_exporter import JSONExporter
from backend.exporters.fcpxml_exporter import FCPXMLExporter


def main():
    parser = argparse.ArgumentParser(description="智慧 iOS 照片/影片說明欄讀取與影片分鏡腳本生成器")
    parser.add_argument("-i", "--input", default=None, help="照片/影片所在的資料夾路徑")
    parser.add_argument("-a", "--album", default=None, help="直接指定 macOS Apple Photos 相簿名稱")
    parser.add_argument("--list-albums", action="store_true", help="列出本機所有 Apple Photos 相簿")
    parser.add_argument("-p", "--prompt", default="請幫我編寫一段流暢感人的生活紀錄片剪輯腳本", help="自訂需求 Prompt（例如風格、氛圍、重點聚焦）")
    parser.add_argument("-s", "--style", default="自然感人旅行Vlog", help="影片風格 (如: 溫馨感人, 輕快Vlog, 復古電影, 節奏Reels)")
    parser.add_argument("-r", "--ratio", default="16:9", choices=["16:9", "9:16", "4:3", "1:1"], help="目標影片比例 (預設 16:9)")
    parser.add_argument("-d", "--duration", type=int, default=None, help="目標總片長（秒）")
    parser.add_argument("-o", "--output", default="movie_script.md", help="輸出檔案路徑 (副檔名支援 .md, .json, .fcpxml)")
    parser.add_argument("--reflection", "--notes-file", default=None, help="指定旅程總心得長文檔案路徑 (.md 或 .txt)")
    parser.add_argument("--api-key", default=None, help="Gemini API Key (若未指定則讀取環境變數)")
    parser.add_argument("--auto-stabilize-cut", action="store_true", help="自動偵測並剪除大幅晃動的影片片段")
    parser.add_argument("--shake-threshold", type=float, default=3.2, help="晃動偵測門檻 (越小越嚴格，預設 3.2)")
    parser.add_argument("--no-effects", action="store_true", help="不要自動加 Ken Burns 運鏡與填滿目標比例")
    parser.add_argument("--use-vision-ai", action="store_true", help="對每張靜態照片調用 Gemini 視覺多模態分析（預設關閉，使用本地免費 SmartCrop 顯著性裁切）")

    args = parser.parse_args()

    geocoder = ReverseGeocoder()
    apple_extractor = ApplePhotosExtractor(geocoder=geocoder)

    if args.list_albums:
        if not apple_extractor.is_available():
            print("❌ 錯誤: 當前環境不支援存取 Apple Photos（需要 macOS 系統）")
            sys.exit(1)
        print("📸 正在讀取 Apple Photos 相簿清單...")
        albums = apple_extractor.list_albums()
        print(f"共發現 {len(albums)} 個相簿：")
        for alb in albums:
            print(f"  - 【{alb['title']}】 ({alb['count']} 張照片)")
        sys.exit(0)

    if not args.input and not args.album:
        print("❌ 錯誤: 請使用 -i/--input 指定本機資料夾，或使用 -a/--album 指定 Apple Photos 相簿名稱！")
        parser.print_help()
        sys.exit(1)

    # 讀取或自動搜尋旅程心得長文
    reflection_text = None
    if args.reflection:
        ref_path = Path(args.reflection)
        if ref_path.exists() and ref_path.is_file():
            with open(ref_path, "r", encoding="utf-8") as rf:
                reflection_text = rf.read().strip()
                print(f"📖 已載入指定心得檔案: {ref_path.name}")
    elif args.input:
        in_dir = Path(args.input)
        if in_dir.exists() and in_dir.is_dir():
            for p in in_dir.rglob("*"):
                if p.is_file() and any(k in p.name.lower() for k in ("心得", "reflection", "journal", "story", "感想")) and p.suffix.lower() in (".md", ".txt"):
                    try:
                        with open(p, "r", encoding="utf-8") as rf:
                            reflection_text = rf.read().strip()
                            if reflection_text:
                                print(f"📖 自動探索並載入旅程心得文件: {p.name}")
                                break
                    except Exception:
                        pass

    print("=" * 60)
    print("🎬 iMovie Script & Storyboard Generator 正在啟動...")
    if args.album:
        print(f"🍎 Apple 相簿來源: {args.album}")
    else:
        print(f"📂 資料夾來源: {Path(args.input).resolve()}")
    print(f"🎯 需求 Prompt: {args.prompt}")
    print(f"🎨 風格: {args.style} | 比例: {args.ratio}")
    if reflection_text:
        print(f"💡 旅程心得精神主軸: 已融入 ({len(reflection_text)} 字)")
    print("=" * 60)

    # 1. 掃描與提取 Metadata
    print("\n🔍 步驟 1/4: 掃描檔案、提取 iOS 說明欄與配對 Live Photo...")
    if args.album:
        items = apple_extractor.scan_album(args.album, max_photos=150, resolve_location=True)
    else:
        input_dir = Path(args.input)
        if not input_dir.exists() or not input_dir.is_dir():
            print(f"❌ 錯誤: 資料夾不存在: {args.input}")
            sys.exit(1)
        metadata_extractor = MetadataExtractor()
        matcher = LivePhotoMatcher(metadata_extractor=metadata_extractor, geocoder=geocoder)
        items = matcher.process_directory(str(input_dir), resolve_location=True)
    if not items:
        print("❌ 警告: 在該目錄下未找到支援的照片或影片檔案。")
        sys.exit(1)

    print(f"✅ 成功辨識 {len(items)} 個多媒體項目 (按時間軸排列):")
    for idx, it in enumerate(items, 1):
        caption_preview = f"「{it['caption']}」" if it.get('caption') else "（無說明備註）"
        live_tag = " [Live Photo 🎥]" if it.get('is_live_photo') else ""
        loc_str = f" @ {it['location']['short_location']}" if it.get('location') else ""
        print(f"  {idx:02d}. {it['file_name']}{live_tag}{loc_str} -> 說明: {caption_preview}")

    # 2. 視覺構圖與裁切分析
    mode_desc = "Gemini 多模態視覺" if args.use_vision_ai else "本地 SmartCrop 顯著性能量演算法 (省下 100% 圖片 Token)"
    print(f"\n📐 步驟 2/4: 進行構圖分析與 {args.ratio} 取景裁切建議 (Crop Advisor) [模式: {mode_desc}]...")
    vision_analyzer = VisionAnalyzer(api_key=args.api_key, use_ai=args.use_vision_ai)
    analyzed_items = []
    for it in items:
        analysis = vision_analyzer.analyze_media(it, target_aspect_ratio=args.ratio)
        it_copy = dict(it)
        it_copy["analysis"] = analysis
        analyzed_items.append(it_copy)
    print("✅ 視覺焦點與 Ken Burns 鏡頭動態分析完成！")

    # 3. 劇本與口白編譯
    print("\n✍️ 步驟 3/4: 融合理想回憶與視覺特徵，編譯分鏡腳本與口白...")
    script_engine = ScriptEngine(api_key=args.api_key)
    script_data = script_engine.generate_script(
        media_items_with_analysis=analyzed_items,
        user_prompt=args.prompt,
        target_duration_seconds=args.duration,
        style=args.style,
        target_aspect_ratio=args.ratio,
        story_context=reflection_text
    )
    print("✅ 劇本編譯完成！標題:", script_data.get("project_title"))

    # 3.5 後處理：Live Photo 定格 + 晃動剪除 + 運鏡 + 分時字幕
    storyboard = script_data.get("storyboard", [])
    search_dirs = [str(Path(args.input).resolve())] if args.input else []

    from backend.engines.livephoto_engine import expand_live_photos
    lp = expand_live_photos(storyboard, search_dirs=search_dirs)
    if lp:
        print(f"\n📷 步驟 3.4: Live Photo 微動態→定格，處理 {len(lp)} 個鏡頭。")

    if args.auto_stabilize_cut:
        print("\n🎥 步驟 3.5: 偵測影片晃動並自動剪除...")
        from backend.analyzers.motion_stability import apply_to_storyboard
        changes = apply_to_storyboard(
            storyboard, search_dirs=search_dirs, shake_threshold=args.shake_threshold
        )
        if changes:
            for c in changes:
                print(f"   ✂️  {c}")
        else:
            print("   ✅ 未偵測到需剪除的大幅晃動片段。")

    if not args.no_effects:
        print("\n🎥 步驟 3.6: 自動 Ken Burns 運鏡與填滿目標比例...")
        from backend.engines.effects_engine import apply_effects
        fx = apply_effects(storyboard, search_dirs=search_dirs, target_ratio=args.ratio)
        print(f"   ✅ 已為 {len(fx)} 個鏡頭加入運鏡/縮放。")

    from backend.engines.subtitle_engine import SubtitleEngine
    for shot in storyboard:
        if not shot.get("timed_subtitles") and (shot.get("voiceover") or "").strip():
            shot["timed_subtitles"] = SubtitleEngine.generate_timed_subtitles(
                shot["voiceover"], float(shot.get("duration_seconds", 4.0))
            )

    # 4. 匯出成果
    print("\n💾 步驟 4/4: 匯出分鏡腳本...")
    out_path = Path(args.output)
    ext = out_path.suffix.lower()

    if ext == ".json":
        json_exporter = JSONExporter()
        json_exporter.export(script_data, str(out_path))
    elif ext in (".xml", ".fcpxml"):
        fcpxml_exporter = FCPXMLExporter()
        fcpxml_exporter.export(script_data, str(out_path), search_dirs=search_dirs)
    else:
        md_exporter = MarkdownExporter()
        md_exporter.export(script_data, str(out_path))

    print(f"🎉 大功告成！腳本已成功儲存至: {out_path.resolve()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
