"""單元測試：專案整體 LLM 呼叫與 Token 優化改進模組
- VisionAnalyzer 預設關閉視覺 AI
- CuratorEngine 故事選片師
- ChapterPlanner 主題篇章架構師
- VoiceoverFitter 口白長度精修
"""

import unittest
from backend.analyzers.vision_analyzer import VisionAnalyzer
from backend.engines.curator_engine import CuratorEngine
from backend.engines.chapter_planner import ChapterPlanner
from backend.engines.voiceover_fitter import VoiceoverFitter, char_count


class TestOptimizations(unittest.TestCase):
    def test_vision_analyzer_default_no_ai(self):
        """驗證 VisionAnalyzer 預設關閉視覺 AI (use_ai=False)，不調用 Gemini"""
        analyzer = VisionAnalyzer(api_key="fake_key_123", use_ai=False)
        self.assertFalse(analyzer.use_ai)
        self.assertIsNone(analyzer.client)

        # 啟發式幾何/SmartCrop 裁切分析
        media_item = {
            "file_path": "/non/existent/path.jpg",
            "width": 1920,
            "height": 1080,
            "media_type": "image",
            "is_image": True,
            "caption": "測試照片",
        }
        res = analyzer.analyze_media(media_item, target_aspect_ratio="16:9")
        self.assertIn("crop_suggestion", res)
        self.assertIn("camera_motion_suggestion", res)

    def test_curator_engine_heuristics_fallback(self):
        """驗證故事選片師在無 API 時的啟發式保底邏輯（優先保留有 caption 與動態的鏡頭）"""
        curator = CuratorEngine(api_key=None)
        
        # 建立 10 個假素材
        items = []
        for i in range(1, 11):
            items.append({
                "shot_index": i,
                "file_name": f"shot_{i:02d}.jpg",
                "caption": "重要回憶" if i in (2, 5, 8) else "",
                "is_video": i in (3, 7),
                "has_people": i in (5, 9),
            })

        # 目標挑選 4 顆
        res = curator.curate_shots(items, target_count=4)
        indices = res["curated_indices"]
        self.assertEqual(len(indices), 4)
        # 帶有 caption 的鏡頭（2, 5, 8）分數較高，應入選
        self.assertIn(2, indices)
        self.assertIn(5, indices)
        self.assertIn(8, indices)

    def test_chapter_planner_fallback_partition(self):
        """驗證篇章架構師在無 API 時的平均篇章分組保底"""
        planner = ChapterPlanner(api_key=None)
        shots = [{"shot_index": i, "file_name": f"img_{i}.jpg"} for i in range(1, 21)]
        
        chapters = planner.plan_chapters(shots, target_chapters=4)
        self.assertEqual(len(chapters), 4)
        self.assertEqual(chapters[0]["chapter_id"], 1)
        self.assertEqual(chapters[-1]["chapter_id"], 4)
        
        # 總鏡頭數必須涵蓋所有 20 顆
        all_indices = []
        for ch in chapters:
            all_indices.extend(ch["shot_indices"])
        self.assertEqual(sorted(all_indices), list(range(1, 21)))

    def test_voiceover_fitter(self):
        """驗證口白長度精修器的字數約束與標點截斷保底"""
        fitter = VoiceoverFitter(api_key=None)
        
        # 1. 未超標文字直接保留
        short_text = "今天天氣非常好，微風吹拂。"
        self.assertEqual(fitter.fit_sentence(short_text, max_chars=20), short_text)

        # 2. 超長文字被精準裁切至上限內，且結尾維持句號
        long_text = "中午太陽實在是太過毒辣，整條公路上完全沒有任何遮蔽，我們最後只好躲進枋寮的一間蓮霧店大口吹冷氣。"
        fitted = fitter.fit_sentence(long_text, max_chars=22)
        self.assertLessEqual(char_count(fitted), 22)
        self.assertTrue(fitted.endswith("。"))


if __name__ == "__main__":
    unittest.main()
