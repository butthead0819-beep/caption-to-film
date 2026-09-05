#!/usr/bin/env python3
"""CLI 工具：以低頻抽幀（預設 10 秒 1 幀）批次分析長影片素材並呼叫 Gemini 多模態 API。

優勢：
- 將每秒 1 幀（1 fps，~258 tokens/s）的官方高額度消耗降為 0.1 fps，直接省下 90% 以上 Token！
- 抓出長影片中的「最佳精華剪輯區間 (best_highlight)」與場景標籤、情緒氛圍。
- 支援 `--dry-run` 免費預覽所有影片抽樣點與 Token 節省試算。
- 可直接將分析結果寫入 `photos_meta.json`，讓後續的分鏡編排與自動調色無縫接軌。

用法：
  # 1. 預覽（不打 API，只看時長、抽幀數與 Token 節省試算）
  .venv/bin/python scripts/sample_video_gemini.py --dry-run

  # 2. 實際分析預設素材夾中的所有影片（10 秒 1 幀）
  .venv/bin/python scripts/sample_video_gemini.py

  # 3. 指定單一影片或自訂抽樣間隔（如 15 秒 1 幀）
  .venv/bin/python scripts/sample_video_gemini.py -i /path/to/my_trip.mp4 --interval 15.0

  # 4. 分析並自動將結果合併進 photos_meta.json
  .venv/bin/python scripts/sample_video_gemini.py --merge-meta
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.analyzers.video_sampler import (  # noqa: E402
    analyze_video_sampled,
    compute_sample_timestamps,
    estimate_token_savings,
    get_video_duration,
)
from backend.config import config  # noqa: E402
from backend.util.media_probe import VIDEO_EXTS  # noqa: E402
from scripts._config import MEDIA_DIR  # noqa: E402

DEFAULT_OUTPUT = ROOT / "scripts" / "video_sampled_analysis.json"
PHOTOS_META_PATH = ROOT / "scripts" / "photos_meta.json"


def find_video_files(inputs: List[str]) -> List[Path]:
    """搜尋所有輸入路徑下的影片檔案。"""
    videos: List[Path] = []
    targets = [Path(p) for p in inputs] if inputs else [MEDIA_DIR]

    for target in targets:
        if not target.exists():
            continue
        if target.is_file() and target.suffix.lower() in VIDEO_EXTS:
            videos.append(target)
        elif target.is_dir():
            for p in sorted(target.rglob("*")):
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    videos.append(p)

    return sorted(list(dict.fromkeys(videos)))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="影片低頻取樣（10秒1幀）與 Gemini 多模態分析工具"
    )
    ap.add_argument(
        "-i",
        "--input",
        nargs="*",
        default=[],
        help="影片檔案路徑或目錄（省略則使用專案預設素材夾）",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="抽樣間隔秒數（預設 10.0 秒 1 幀；短片自動退回中間 1 幀）",
    )
    ap.add_argument(
        "--max-dim",
        type=int,
        default=1024,
        help="抽樣影格最大邊長解析度（預設 1024）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="僅進行本地掃描與 Token 節省試算，不呼叫 Gemini API",
    )
    ap.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"分析結果輸出 JSON 路徑（預設：{DEFAULT_OUTPUT.name}）",
    )
    ap.add_argument(
        "--merge-meta",
        action="store_true",
        help="是否將場景標籤與精華資訊同步合併至 scripts/photos_meta.json",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="本次最多處理幾支影片",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="每次 API 呼叫後的冷卻秒數（避免撞 Rate Limit）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="強制重跑即使先前已存在於分析結果中",
    )
    ap.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini 模型名稱（預設 gemini-2.5-flash）",
    )

    args = ap.parse_args()

    videos = find_video_files(args.input)
    if not videos:
        print(f"⚠️ 未找到任何影片檔案（搜尋路徑：{args.input or [str(MEDIA_DIR)]}）")
        return

    print(f"🎬 找到 {len(videos)} 支影片素材，取樣設定：每 {args.interval:.1f} 秒抽 1 幀")
    print("=" * 65)

    # 載入現有輸出
    out_path = Path(args.output)
    existing_results: Dict[str, Any] = {}
    if out_path.exists():
        try:
            existing_results = json.loads(out_path.read_text("utf-8"))
        except Exception:
            existing_results = {}

    total_dur = 0.0
    total_raw_tokens = 0
    total_sampled_tokens = 0
    processed_count = 0

    if args.dry_run:
        print("🔍 [DRY RUN 模式] 計算抽樣點與預估節省量（不呼叫 API）：\n")
        for idx, v in enumerate(videos[: args.limit], start=1):
            dur = get_video_duration(v)
            pts = compute_sample_timestamps(dur, interval_s=args.interval)
            savings = estimate_token_savings(dur, len(pts))

            total_dur += dur
            total_raw_tokens += savings["estimated_raw_1fps_tokens"]
            total_sampled_tokens += savings["estimated_sampled_tokens"]

            pts_str = ", ".join(f"{t:.1f}s" for t in pts[:6])
            if len(pts) > 6:
                pts_str += f"... (+{len(pts)-6} 幀)"

            print(f"[{idx:02d}/{len(videos):02d}] 📹 {v.name}")
            print(f"     時長: {dur:.1f}s | 抽樣幀數: {len(pts)} 幀 ({pts_str})")
            print(
                f"     預估 Token: {savings['estimated_sampled_tokens']:,} "
                f"(原生直傳需 {savings['estimated_raw_1fps_tokens']:,}，節省 {savings['saved_percentage']}%)"
            )

        saved_total = total_raw_tokens - total_sampled_tokens
        saved_pct = (saved_total / total_raw_tokens * 100.0) if total_raw_tokens > 0 else 0
        print("\n" + "=" * 65)
        print(f"📊 總計評估結果 ({len(videos[: args.limit])} 支影片)：")
        print(f"   總時長: {total_dur/60:.1f} 分鐘 ({total_dur:.1f} 秒)")
        print(f"   原需 Token: {total_raw_tokens:,} tokens")
        print(f"   10秒1幀 Token: {total_sampled_tokens:,} tokens")
        print(f"   🎉 總計省下: {saved_total:,} tokens (節省約 {saved_pct:.1f}%)！")
        return

    # 實際 API 調用
    if not config.gemini_api_key:
        sys.exit("❌ 錯誤：未設定 GEMINI_API_KEY，無法調用 API。")

    from google import genai

    client = genai.Client(api_key=config.gemini_api_key)

    for idx, v in enumerate(videos, start=1):
        if processed_count >= args.limit:
            print(f"\n⏹️ 已達到處理上限 {args.limit} 支影片。")
            break

        stem_key = v.stem.lower()
        if not args.force and stem_key in existing_results and "error" not in existing_results[stem_key]:
            continue

        dur = get_video_duration(v)
        pts = compute_sample_timestamps(dur, interval_s=args.interval)
        print(f"\n[{idx:02d}/{len(videos):02d}] 🚀 分析 {v.name} ({dur:.1f}s, {len(pts)} 影格)...")

        try:
            res = analyze_video_sampled(
                client=client,
                video_path=v,
                interval_s=args.interval,
                max_dimension=args.max_dim,
                model=args.model,
            )
            existing_results[stem_key] = res
            processed_count += 1

            # 即時輸出摘要
            print(f"   📝 內容摘要: {res.get('summary', '無')}")
            hl = res.get("best_highlight", {})
            if hl:
                print(f"   ✨ 推薦精華: [{hl.get('start_sec', 0):.1f}s - {hl.get('end_sec', 0):.1f}s] ({hl.get('reason', '')})")
            print(f"   🏷️  標籤: {', '.join(res.get('scene_labels', []))} | 氛圍: {res.get('mood', '未知')}")
            
            savings = res.get("token_savings", {})
            if savings:
                print(f"   💰 Token: {savings.get('estimated_sampled_tokens', 0):,} (省下 {savings.get('saved_percentage', 0)}%)")

            # 每次處理完即寫入檔案，防止中斷丟失
            out_path.write_text(json.dumps(existing_results, ensure_ascii=False, indent=2), encoding="utf-8")

            if args.sleep > 0:
                time.sleep(args.sleep)

        except Exception as e:
            err_str = str(e)
            print(f"   ⚠️ 分析失敗: {err_str}")
            if "429" in err_str or "quota" in err_str.lower():
                print("   🛑 撞上 API 額度限制 (429)，已保存現有進度並停止。")
                break

    print("\n" + "=" * 65)
    print(f"✅ 分析完成！結果已保存至: {out_path}")

    # 若需要同步進 photos_meta.json
    if args.merge_meta and PHOTOS_META_PATH.exists():
        try:
            photos_meta = json.loads(PHOTOS_META_PATH.read_text("utf-8"))
            merged_count = 0
            for stem, data in existing_results.items():
                if "error" in data:
                    continue
                if stem not in photos_meta:
                    photos_meta[stem] = {}
                meta_item = photos_meta[stem]
                
                # 補充標籤與情緒
                if data.get("scene_labels"):
                    meta_item["labels"] = list(dict.fromkeys((meta_item.get("labels") or []) + data["scene_labels"]))
                if data.get("mood"):
                    meta_item["mood"] = data["mood"]
                if "has_people" in data:
                    meta_item["has_people"] = data["has_people"]
                if data.get("best_highlight"):
                    meta_item["best_highlight"] = data["best_highlight"]
                meta_item["video_summary"] = data.get("summary", "")
                merged_count += 1

            PHOTOS_META_PATH.write_text(json.dumps(photos_meta, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"🔄 已成功將 {merged_count} 筆影片分析資料合併至: {PHOTOS_META_PATH}")
        except Exception as e:
            print(f"⚠️ 合併至 photos_meta.json 時發生錯誤: {e}")


if __name__ == "__main__":
    main()
