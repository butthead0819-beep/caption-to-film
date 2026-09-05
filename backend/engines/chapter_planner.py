"""主題篇章架構師 (Chapter Planner)

職責：
運用純文字 LLM，將精選出的 40~50 顆鏡頭與旅程核心心得長文結合，
規劃出 6~8 個具有強烈電影感、情緒起伏與主題支撐的大篇章（例如【出發的勇氣】、【逆風太平洋】、【壽卡大魔王】...）。

取代過去純以曆日（Day 1, Day 2）或 GPS 距離硬切造成的碎裂感（避免切出數十個冷冰冰小段落）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from google import genai
from google.genai import types

from ..config import config


class ChapterPlanner:
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        self.api_key = api_key or config.gemini_api_key
        self.model = model
        self.client = None
        if self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[Warning] ChapterPlanner GenAI Client 初始化失敗: {e}")

    def plan_chapters(
        self,
        curated_shots: List[Dict[str, Any]],
        story_context: Optional[str] = None,
        target_chapters: int = 7,
    ) -> List[Dict[str, Any]]:
        """將挑選好的分鏡組織為 target_chapters 個主題篇章。
        
        回傳清單：
        [
          {
            "chapter_id": 1,
            "title": "【出發的勇氣】",
            "subtitle": "告別熟悉的日常",
            "shot_indices": [1, 2, 3, 4],
            "theme_summary": "整裝出發與初登板的緊張興奮",
            "emotional_tone": "期待"
          },
          ...
        ]
        """
        if not curated_shots:
            return []

        # 1. 建立簡明時間軸
        timeline = []
        for idx, s in enumerate(curated_shots, start=1):
            loc = (
                s.get("location", {}).get("short_location")
                or s.get("place", {}).get("name")
                or s.get("location_name")
                or "未知"
            )
            cap = s.get("caption") or s.get("user_caption_memory") or s.get("voiceover") or ""
            timeline.append({
                "idx": idx,
                "file": s.get("file_name") or s.get("stem") or f"shot_{idx}",
                "time": str(s.get("creation_date_formatted") or s.get("taken") or "")[:16],
                "place": loc,
                "caption": cap[:60] if cap else "（無文字）",
            })

        # 2. 調用 LLM 進行大綱規劃
        if self.client:
            try:
                chapters = self._call_planner_llm(timeline, story_context, target_chapters)
                if chapters:
                    return chapters
            except Exception as e:
                print(f"[Warning] LLM 篇章規劃失敗 ({e})，切換為本地均分章節")

        # 3. Fallback: 本機均分保底
        return self._fallback_partition(curated_shots, target_chapters)

    def _call_planner_llm(
        self,
        timeline: List[Dict[str, Any]],
        story_context: Optional[str],
        target_chapters: int,
    ) -> Optional[List[Dict[str, Any]]]:
        system_instruction = f"""你是金馬獎最佳紀錄片編劇。
請閱讀這份已挑選出的 {len(timeline)} 顆連續鏡頭時間軸與核心心得，
將整部片規劃為約 {target_chapters} 個主題鮮明、富有電影感的大篇章。

【篇章規劃法則】：
1. 🎬 每個篇章標題格式：必須帶有【】方括號，例如【出發的勇氣】、【山海之間的沉默】、【壽卡大魔王】、【歸途的微光】。
2. 🔗 嚴格時間連續性：鏡頭必須依時間順序分入各篇章，不可時空跳躍。每個篇章涵蓋連續的一段 shot_indices（例如第 1 章是 1~6，第 2 章是 7~13）。
3. 🎭 全片必須涵蓋全部 1 到 {len(timeline)} 顆鏡頭，不漏掉任何一顆。
4. 🚫 嚴格禁詞：標題與主題絕不得出現 AI 工具名。

請回傳嚴格 JSON 格式：
{{
  "chapters": [
    {{
      "chapter_id": 1,
      "title": "【章節主題】",
      "subtitle": "副標題一句話",
      "shot_indices": [1, 2, 3, 4, 5],
      "theme_summary": "這章的主要情節與情感重心",
      "emotional_tone": "開朗 / 沉重 / 振奮 / 溫馨"
    }}
  ]
}}
"""
        essay_sec = f"\n【旅程核心心得與人生體悟】：\n{story_context.strip()}\n" if story_context else ""
        user_content = f"""目標篇章數：約 {target_chapters} 章
{essay_sec}
【分鏡時序清單（共 {len(timeline)} 顆）】：
{json.dumps(timeline, ensure_ascii=False, indent=1)}
"""

        resp = self.client.models.generate_content(
            model=self.model,
            contents=[user_content],
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.3,
            ),
        )

        if resp.text:
            clean_text = resp.text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.splitlines()
                clean_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            data = json.loads(clean_text)
            chaps = data.get("chapters", [])
            if chaps:
                return chaps

        return None

    def _fallback_partition(
        self, curated_shots: List[Dict[str, Any]], target_chapters: int
    ) -> List[Dict[str, Any]]:
        """本地等份切分保底"""
        total = len(curated_shots)
        k = max(1, min(target_chapters, total))
        chunk_size = max(1, total // k)
        
        chapters = []
        curr = 1
        for i in range(1, k + 1):
            end = total if i == k else min(total, curr + chunk_size - 1)
            indices = list(range(curr, end + 1))
            chapters.append({
                "chapter_id": i,
                "title": f"【第{i}篇：旅途記憶】",
                "subtitle": f"第 {curr} 至 {end} 鏡",
                "shot_indices": indices,
                "theme_summary": "旅途風景與故事記錄",
                "emotional_tone": "溫馨",
            })
            curr = end + 1

        return chapters
