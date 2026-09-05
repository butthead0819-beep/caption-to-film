"""智慧故事選片師 (Smart Story Curator)

職責：
運用純文字 LLM（花費極少 Token，約 3k~5k tokens），從大量（例如 150~300+）候選素材中，
依據故事線的「起、承、轉、合」、情緒轉折與題材多樣性，智慧精選出最精華的 35~50 顆鏡頭。

取代傳統純數學公式打分（容易選入同一地點連續 5 張自拍、卻把重要修車/跌倒回憶砍掉的問題）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from google import genai
from google.genai import types

from ..config import config


class CuratorEngine:
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        self.api_key = api_key or config.gemini_api_key
        self.model = model
        self.client = None
        if self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[Warning] CuratorEngine GenAI Client 初始化失敗: {e}")

    def curate_shots(
        self,
        candidate_items: List[Dict[str, Any]],
        target_count: int = 45,
        user_prompt: str = "請挑選最具故事性與情感起伏的鏡頭組合",
        story_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """從大量候選素材中，以故事線為導向精選 target_count 顆鏡頭。
        
        回傳結構：
        {
          "curated_indices": [1, 4, 12, ...],
          "narrative_arc": "...",
          "shot_roles": { "1": {"role": "opening", "reason": "..."} },
          "items": [...]  # 挑選出的 item 原始物件
        }
        """
        if not candidate_items:
            return {"curated_indices": [], "items": [], "narrative_arc": "無素材"}

        # 若候選數量已小於或等於目標數量，直接全數保留
        if len(candidate_items) <= target_count:
            return {
                "curated_indices": list(range(1, len(candidate_items) + 1)),
                "items": candidate_items,
                "narrative_arc": "素材數量在目標限額內，全數收錄",
                "shot_roles": {},
            }

        # 1. 建立緊湊純文字清單（每筆僅佔約 20~30 tokens）
        compact_manifest = []
        for idx, it in enumerate(candidate_items, start=1):
            loc = (
                it.get("location", {}).get("short_location")
                or it.get("place", {}).get("name")
                or it.get("location_name")
                or "未知地點"
            )
            time_str = str(it.get("creation_date_formatted") or it.get("taken") or "")[:16]
            caption = it.get("caption") or it.get("user_caption_memory") or ""
            
            compact_manifest.append({
                "idx": idx,
                "file": it.get("file_name") or it.get("stem") or f"shot_{idx}",
                "type": it.get("media_type") or ("video" if it.get("is_video") else "photo"),
                "time": time_str,
                "place": loc,
                "caption": caption if caption else "（無文字備註）",
                "people": bool(it.get("persons") or it.get("has_people")),
                "labels": (it.get("labels") or it.get("scene_labels") or [])[:3],
            })

        # 2. 若有 LLM Client，調用純文字高階策劃
        if self.client:
            try:
                res = self._call_curator_llm(
                    compact_manifest, target_count, user_prompt, story_context
                )
                if res and res.get("curated_indices"):
                    selected_idx_set = set(res["curated_indices"])
                    curated_items = [
                        it for idx, it in enumerate(candidate_items, start=1)
                        if idx in selected_idx_set
                    ]
                    res["items"] = curated_items
                    return res
            except Exception as e:
                print(f"[Warning] LLM 故事選片失敗 ({e})，切換至本機啟發式挑選")

        # 3. Fallback: 本機啟發式保底挑選（優先選有 Caption、人物、影片）
        return self._heuristic_curation(candidate_items, target_count)

    def _call_curator_llm(
        self,
        manifest: List[Dict[str, Any]],
        target_count: int,
        user_prompt: str,
        story_context: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        system_instruction = f"""你是世界頂級紀錄片首席剪輯總監。
你的任務是從大量候選素材中，精挑細選出剛好 {target_count} 顆（允許 ±3 顆）最能串聯起整部紀錄片起承轉合的黃金鏡頭。

【選片黃金法則】：
1. 📖 故事線優先（Narrative Over Aesthetics）：
   - 有「說明欄 Caption」記載特定回憶、挫折、對話、心理感受的素材是靈魂，優先挑選。
   - 包含具體事件轉折（出發、修車、登頂、迷路、吃美食、大雨、終點）的鏡頭必選。
2. 🚫 嚴禁同質堆疊（Anti-Redundancy）：
   - 同一地點、同一時間的連拍或相似自拍，嚴格只留 1 顆最強烈的。
   - 避免整部片都是風景明信片，人物互動、表情、動作才是牽動觀眾情緒的核心。
3. 🎭 起承轉合完整度：
   - 起（開場 3~5 顆）：期待、整裝出發、日常告別。
   - 承（中段 25~30 顆）：在路上的風景、遭遇的困難、父子/旅伴同行細節。
   - 轉（高潮 5~8 顆）：最艱難時刻（如大魔王爬坡）或最深刻的夕陽沉思。
   - 合（結尾 3~5 顆）：抵達終點、平安歸來、昇華感悟。

請輸出嚴格 JSON 格式：
{{
  "narrative_arc": "一句話描述這 45 顆鏡頭串起的故事主軸",
  "curated_indices": [整數清單，例如 1, 4, 7, 12, ... 目標數量約 {target_count}],
  "shot_roles": {{
    "1": {{"role": "opening", "reason": "出發前的整裝期待"}},
    "12": {{"role": "conflict", "reason": "逆風與疲憊的真實記錄"}}
  }}
}}
"""
        context_sec = f"\n【全片核心體悟與精神長文】：\n{story_context.strip()}\n" if story_context else ""
        user_content = f"""【剪輯需求】：{user_prompt}
目標鏡頭數：約 {target_count} 顆
{context_sec}
【所有候選素材清單（共 {len(manifest)} 筆）】：
{json.dumps(manifest, ensure_ascii=False, indent=1)}
"""

        resp = self.client.models.generate_content(
            model=self.model,
            contents=[user_content],
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.4,
            ),
        )

        if resp.text:
            clean_text = resp.text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.splitlines()
                clean_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            data = json.loads(clean_text)
            # 確保索引為 int
            indices = [int(x) for x in data.get("curated_indices", []) if isinstance(x, (int, str)) and str(x).isdigit()]
            data["curated_indices"] = sorted(set(indices))
            return data

        return None

    def _heuristic_curation(
        self, candidate_items: List[Dict[str, Any]], target_count: int
    ) -> Dict[str, Any]:
        """本地啟發式評分挑選保底方案"""
        scored = []
        for idx, it in enumerate(candidate_items, start=1):
            score = 1.0
            if it.get("caption") or it.get("user_caption_memory"):
                score += 3.0
            if it.get("is_video") or it.get("is_live_photo"):
                score += 1.5
            if it.get("persons") or it.get("has_people"):
                score += 1.0
            scored.append((idx, score, it))

        # 優先取前 target_count 高分
        scored.sort(key=lambda x: x[1], reverse=True)
        chosen = scored[:target_count]
        # 按原始順序排列
        chosen.sort(key=lambda x: x[0])

        return {
            "narrative_arc": "本地啟發式篩選（優先保留備註與動態素材）",
            "curated_indices": [x[0] for x in chosen],
            "items": [x[2] for x in chosen],
            "shot_roles": {},
        }
