"""單元測試：影片低頻取樣模組 (backend/analyzers/video_sampler.py) 與 CLI 工具
"""

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from backend.analyzers.video_sampler import (
    compute_sample_timestamps,
    estimate_token_savings,
    extract_frame_bytes,
    extract_sampled_frames,
    get_video_duration,
)


class TestVideoSampler(unittest.TestCase):
    def test_compute_sample_timestamps_edge_cases(self):
        # 邊界 1: 負時長或 0 秒
        self.assertEqual(compute_sample_timestamps(0.0, interval_s=10.0), [])
        self.assertEqual(compute_sample_timestamps(-5.0, interval_s=10.0), [])

        # 邊界 2: 短片 (<= 10 秒，例如 2.4 秒 Live Photo、8 秒短片、10 秒片)
        self.assertEqual(compute_sample_timestamps(2.4, interval_s=10.0), [1.2])
        self.assertEqual(compute_sample_timestamps(8.0, interval_s=10.0), [4.0])
        self.assertEqual(compute_sample_timestamps(10.0, interval_s=10.0), [5.0])

        # 邊界 3: 15 秒影片（0.0s, 10.0s，共 2 幀）
        self.assertEqual(compute_sample_timestamps(15.0, interval_s=10.0), [0.0, 10.0])

        # 邊界 4: 28 秒影片（0.0s, 10.0s, 20.0s，共 3 幀）
        self.assertEqual(compute_sample_timestamps(28.0, interval_s=10.0), [0.0, 10.0, 20.0])

        # 邊界 5: 60 秒長片（0.0s, 10.0s, 20.0s, 30.0s, 40.0s, 50.0s，共 6 幀）
        self.assertEqual(
            compute_sample_timestamps(60.0, interval_s=10.0),
            [0.0, 10.0, 20.0, 30.0, 40.0, 50.0],
        )

        # 邊界 6: 自訂間隔 5 秒 (14 秒長度 -> 0.0s, 5.0s, 10.0s)
        self.assertEqual(compute_sample_timestamps(14.0, interval_s=5.0), [0.0, 5.0, 10.0])

    def test_estimate_token_savings(self):
        # 60 秒影片，抽 6 幀
        res = estimate_token_savings(60.0, 6)
        self.assertEqual(res["duration_s"], 60.0)
        self.assertEqual(res["sampled_frames_count"], 6)
        self.assertEqual(res["estimated_raw_1fps_tokens"], 60 * 258)  # 15,480
        self.assertEqual(res["estimated_sampled_tokens"], 6 * 258)    # 1,548
        self.assertEqual(res["saved_tokens"], 15480 - 1548)          # 13,932
        self.assertAlmostEqual(res["saved_percentage"], 90.0, places=1)

        # 0 秒影片
        res_zero = estimate_token_savings(0.0, 0)
        self.assertEqual(res_zero["saved_percentage"], 0.0)

    def test_ffmpeg_extraction_with_synthetic_video(self):
        """生成一個 3 秒的色彩漸變測試影片，測試時長偵測與影格抽取"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_video = Path(tmpdir) / "test_synth.mp4"
            # 使用 ffmpeg testsrc 生成 3 秒 1280x720 24fps 測試影片
            cmd_gen = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=3:size=1280x720:rate=24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(tmp_video),
            ]
            subprocess.run(cmd_gen, capture_output=True, check=True)

            # 1. 驗證時長偵測
            dur = get_video_duration(tmp_video)
            self.assertGreaterEqual(dur, 2.9)
            self.assertLessEqual(dur, 3.2)

            # 2. 驗證抽單幀
            b = extract_frame_bytes(tmp_video, timestamp_s=1.0, max_dimension=640)
            self.assertIsNotNone(b)
            im = Image.open(io.BytesIO(b))
            self.assertEqual(im.format, "JPEG")
            # 寬高比應為 16:9，且長邊為 640
            self.assertEqual(im.width, 640)
            self.assertEqual(im.height, 360)

            # 3. 驗證短片抽取序列（3 秒 < 10 秒，應只回傳 1 幀中點幀）
            sampled = extract_sampled_frames(tmp_video, interval_s=10.0, max_dimension=640)
            self.assertEqual(len(sampled), 1)
            ts, frame_bytes = sampled[0]
            self.assertAlmostEqual(ts, dur / 2.0, places=1)
            self.assertTrue(len(frame_bytes) > 0)


if __name__ == "__main__":
    unittest.main()
