"""智慧口白秒數精修器 (Voiceover Pacing & Length Fitter)

職責：
解決口白字數超標時，傳統代碼使用正則逗號/句號「硬切截斷」造成語意殘缺（例如「中午太熱躲進枋寮一間。」）的問題。

當口白長度超過該鏡頭的時間限制（CPS ≈ 6.0 字/秒）時，調用極輕量模型 (gemini-2.5-flash-lite)
在幾十個 Token 內將句子改寫為語意完整、口語流暢且嚴格吻合字數上限的精煉短句。
若未聯網或額度不足，則使用增強型語意標點修剪作為保底。
"""

from __future__ import annotations

import re
from typing import Any, Optional
from google import genai
from google.genai import types

from ..config import config


def char_count(s: str) -> int:
    """計算中英混和字數（中文 1 字，英文單詞或符號計為相應長度）。"""
    return len(re.sub(r"\s+", " ", s or "").strip())


class VoiceoverFitter:
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash-lite"):
        self.api_key = api_key or config.gemini_api_key
        self.model = model
        self.client = None
        if self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[Warning] VoiceoverFitter GenAI Client 初始化失敗: {e}")

    def fit_sentence(self, text: str, max_chars: int, min_chars: int = 6) -> str:
        """將單句或短段落口白精修至 max_chars 字內。"""
        cleaned = (text or "").strip()
        if not cleaned or max_chars <= 0:
            return ""

        cur_len = char_count(cleaned)
        # 如果已經在長度限制內，原樣回傳
        if cur_len <= max_chars:
            return cleaned

        target_min = max(min_chars, int(max_chars * 0.7))

        # 1. 嘗試調用 Flash-Lite 進行精準改寫
        if self.client:
            try:
                refined = self._call_fit_llm(cleaned, target_min, max_chars)
                if refined and char_count(refined) <= max_chars:
                    return refined
            except Exception as e:
                print(f"[Warning] LLM 口白精修失敗 ({e})，使用本機智慧修剪")

        # 2. Fallback: 本機智慧切句保底
        return self._local_trim(cleaned, max_chars)

    def _call_fit_llm(self, text: str, min_chars: int, max_chars: int) -> Optional[str]:
        prompt = f"""你是紀錄片字幕精修師。請將以下這句口白改寫為語意完整、語調自然口語的短句。
【嚴格要求】：
1. 繁體中文，保持原句的核心事件與情感。
2. 總字數（含標點符號）嚴格必須在 {min_chars} 到 {max_chars} 字之間。
3. 絕不可在句尾留下未完的逗號或殘缺語句，必須是一句完整的話。
4. 只回傳改寫後的單一句子，不要包含引號、解釋或任何額外文字。

原句：{text}"""

        resp = self.client.models.generate_content(
            model=self.model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=60,
            ),
        )
        if resp.text:
            ans = resp.text.strip().strip('"').strip("'").strip("「」『』")
            return ans

        return None

    def _local_trim(self, text: str, limit: int) -> str:
        """本機語意標點智慧截斷保底"""
        if char_count(text) <= limit:
            return text

        # 優先依標點切段
        parts = re.split(r"([，、；：。！？,;!?:])", text)
        buf = ""
        last_complete = ""
        for i in range(0, len(parts), 2):
            clause = parts[i]
            punct = parts[i + 1] if i + 1 < len(parts) else ""
            candidate = buf + clause + punct
            if char_count(candidate) <= limit:
                buf = candidate
                if punct in ("。", "！", "？", "!", "?"):
                    last_complete = buf
            else:
                break

        if last_complete:
            return last_complete
        if buf:
            return buf.rstrip("，、；：,;:") + "。"

        # 單一句子本身太長：在前半段最後一個字切並補省略號或句號
        return text[: max(1, limit - 1)].rstrip("，、；：,;:") + "。"
