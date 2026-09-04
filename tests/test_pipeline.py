import os
import sys
import unittest
from pathlib import Path
from PIL import Image
import piexif

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.extractors.metadata_extractor import MetadataExtractor
from backend.extractors.livephoto_matcher import LivePhotoMatcher
from backend.analyzers.vision_analyzer import VisionAnalyzer
from backend.engines.script_engine import ScriptEngine
from backend.exporters.markdown_exporter import MarkdownExporter
from backend.exporters.json_exporter import JSONExporter
from backend.exporters.fcpxml_exporter import FCPXMLExporter


class TestMovieScriptPipeline(unittest.TestCase):
    
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "sample_media"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        # 建立一張模擬帶有 EXIF ImageDescription (iOS 說明欄) 與 GPS 的照片
        self.img_path = self.test_dir / "IMG_1001.JPG"
        img = Image.new("RGB", (3000, 2000), color=(73, 109, 137))
        
        # 寫入 EXIF 與 UserComment
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        exif_dict["0th"][piexif.ImageIFD.ImageDescription] = "今天在海邊看夕陽，風很舒服。".encode('utf-8')
        exif_dict["0th"][piexif.ImageIFD.Make] = "Apple"
        exif_dict["0th"][piexif.ImageIFD.Model] = "iPhone 15 Pro"
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = "2024:08:26 18:30:00"
        
        exif_bytes = piexif.dump(exif_dict)
        img.save(self.img_path, exif=exif_bytes)

        # 建立同名的 Live Photo MOV 模擬短片
        self.mov_path = self.test_dir / "IMG_1001.MOV"
        with open(self.mov_path, "wb") as f:
            f.write(b"mock_mov_content")

    def test_metadata_extraction_and_caption(self):
        extractor = MetadataExtractor()
        meta = extractor.extract(str(self.img_path))
        
        self.assertEqual(meta["file_name"], "IMG_1001.JPG")
        self.assertEqual(meta["caption"], "今天在海邊看夕陽，風很舒服。")
        self.assertEqual(meta["camera"]["make"], "Apple")
        self.assertEqual(meta["camera"]["model"], "iPhone 15 Pro")

    def test_live_photo_matching(self):
        matcher = LivePhotoMatcher()
        items = matcher.process_directory(str(self.test_dir), resolve_location=False)
        
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["is_live_photo"])
        self.assertEqual(item["media_type"], "live_photo")
        self.assertEqual(item["live_video"]["file_name"], "IMG_1001.MOV")

    def test_vision_and_crop_analysis(self):
        analyzer = VisionAnalyzer()
        media_item = {
            "file_path": str(self.img_path),
            "caption": "今天在海邊看夕陽，風很舒服。",
            "media_type": "live_photo",
            "is_live_photo": True,
            "width": 3000,
            "height": 2000
        }
        
        # 測試 16:9 與 9:16 的裁切建議
        analysis_16_9 = analyzer.analyze_media(media_item, target_aspect_ratio="16:9")
        self.assertIn("crop_suggestion", analysis_16_9)
        self.assertEqual(len(analysis_16_9["crop_suggestion"]["crop_box_normalized"]), 4)
        
        analysis_9_16 = analyzer.analyze_media(media_item, target_aspect_ratio="9:16")
        self.assertIn("camera_motion_suggestion", analysis_9_16)

    def test_script_generation_and_export(self):
        analyzer = VisionAnalyzer()
        media_item = {
            "file_path": str(self.img_path),
            "file_name": "IMG_1001.JPG",
            "caption": "今天在海邊看夕陽，風很舒服。",
            "media_type": "live_photo",
            "is_live_photo": True,
            "width": 3000,
            "height": 2000,
            "creation_date_formatted": "2024-08-26 18:30:00"
        }
        media_item["analysis"] = analyzer.analyze_media(media_item, target_aspect_ratio="16:9")

        script_engine = ScriptEngine()
        script = script_engine.generate_script(
            media_items_with_analysis=[media_item],
            user_prompt="製作一段海邊散步的放鬆短片",
            target_aspect_ratio="16:9"
        )

        self.assertIn("project_title", script)
        # Gemini（付費 key）可能把一張 Live Photo 拆成多顆鏡頭（微動態+定格/加開場尾），
        # 本機 fallback 則是 1 顆 —— 只要求 >=1，不寫死。
        self.assertGreaterEqual(len(script["storyboard"]), 1)
        shot = script["storyboard"][0]
        self.assertTrue(len(shot.get("voiceover", "").strip()) > 0)

        # 測試各 Exporter
        md_exporter = MarkdownExporter()
        md_content = md_exporter.export(script)
        self.assertIn("# 🎬", md_content)
        self.assertIn("IMG_1001.JPG", md_content)

        json_exporter = JSONExporter()
        json_content = json_exporter.export(script)
        self.assertIn("project_title", json_content)

        fcpxml_exporter = FCPXMLExporter()
        fcpxml_content = fcpxml_exporter.export(script)
        self.assertIn("<fcpxml", fcpxml_content)
        self.assertIn("<adjust-transform", fcpxml_content)
        self.assertNotIn("<title", fcpxml_content)   # 預設不內嵌字幕 (Resolve 走 SRT)
        self.assertNotIn("position=", fcpxml_content)  # 運鏡只 scale，不寫 position

        FCPXMLExporter.EMIT_TITLES = True
        try:
            with_titles = FCPXMLExporter().export(script)
        finally:
            FCPXMLExporter.EMIT_TITLES = False
        self.assertIn("<title", with_titles)
        self.assertIn("VO_Sub_01", with_titles)

    def test_smart_crop_analyzer(self):
        from backend.analyzers.smart_crop import SmartCropAnalyzer
        sc = SmartCropAnalyzer()
        res_16_9 = sc.analyze_crop(self.img_path, target_aspect_ratio="16:9", caption="夕陽特寫")
        
        self.assertIn("crop_suggestion", res_16_9)
        crop_box = res_16_9["crop_suggestion"]["crop_box_normalized"]
        self.assertEqual(len(crop_box), 4)
        ymin, xmin, ymax, xmax = crop_box
        self.assertTrue(0.0 <= ymin <= ymax <= 1.0)
        self.assertTrue(0.0 <= xmin <= xmax <= 1.0)

        # 檢查 Ken Burns 參數
        self.assertIn("camera_motion_suggestion", res_16_9)
        motion = res_16_9["camera_motion_suggestion"]
        self.assertIn("start_box", motion)
        self.assertIn("end_box", motion)
        self.assertIn("motion_type", motion)

    def test_apple_photos_extractor_interface(self):
        from backend.extractors.apple_photos_extractor import ApplePhotosExtractor
        extractor = ApplePhotosExtractor()
        # 測試 availability 函數
        is_avail = extractor.is_available()
        self.assertIsInstance(is_avail, bool)

    def _fake_storyboard(self):
        return [
            {"scene_title": "Day 1【啟程】", "media_file": "IMG_1001.JPG",
             "file_path": str(self.img_path), "media_type": "image", "is_image": True,
             "duration_seconds": 3.0, "voiceover": "出發了。", "visual_action": "兩人合照"},
            {"scene_title": "Day 1【啟程】", "media_file": "b.jpg", "file_path": "b.jpg",
             "media_type": "image", "is_image": True, "duration_seconds": 2.5,
             "voiceover": "", "visual_action": "公路全景延伸至山巔"},
            {"scene_title": "Day 2【海岸】", "media_file": "IMG_1001.MOV",
             "file_path": str(self.mov_path), "media_type": "video", "is_image": False,
             "duration_seconds": 3.0, "voiceover": "海風很舒服。", "visual_action": "騎車的身影"},
        ]

    def test_highlight_engine(self):
        from backend.engines.highlight_engine import select_highlights
        sb = self._fake_storyboard()
        select_highlights(sb, mode="highlight", target_count=2)
        for s in sb:
            self.assertIn("highlight_score", s)
            self.assertIn("keep", s)
        # 開場與結尾一定保留
        self.assertTrue(sb[0]["keep"])
        self.assertTrue(sb[-1]["keep"])

    def test_abroll_roles_and_export(self):
        from backend.engines.abroll_engine import classify_roles
        sb = self._fake_storyboard()
        counts = classify_roles(sb)
        self.assertEqual(counts["a-roll"] + counts["b-roll"], 3)
        self.assertEqual(sb[1]["role"], "b-roll")  # 「全景延伸至山巔」
        xml_ab = FCPXMLExporter().export({"project_title": "AB", "storyboard": sb}, abroll=True)
        self.assertIn("<spine>", xml_ab)
        self.assertIn("lane=\"1\"", xml_ab)  # B-roll 疊軌
        import xml.dom.minidom
        xml.dom.minidom.parseString(xml_ab)  # 格式合法


if __name__ == "__main__":
    unittest.main()

