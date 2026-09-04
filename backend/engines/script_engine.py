import json
import re
from typing import Dict, Any, List, Optional
from google import genai
from google.genai import types

from ..config import config

_READING_CPS = 6.0   # 中文舒適閱讀 / 念白速度（字/秒）


def _chapter_key(scene_title: str) -> str:
    """從 scene_title 取章節鍵（【縱谷與稻田】→ 縱谷與稻田；否則取「：」前）。"""
    m = re.search(r"[【\[]([^】\]]+)[】\]]", scene_title or "")
    if m:
        return m.group(1).strip()
    return (scene_title or "").split("：")[0].strip()


def _sentences(text: str) -> List[str]:
    """依句末標點切句，標點留在句尾。"""
    parts = re.split(r"([。！？!?…]+)", text or "")
    out, buf = [], ""
    for seg in parts:
        if not seg:
            continue
        buf += seg
        if re.fullmatch(r"[。！？!?…]+", seg):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out or ([text.strip()] if text.strip() else [])


def _trim_to_chars(text: str, limit: int) -> str:
    """把旁白裁到 limit 字內：先砍整句，最後一句還太長就在標點處硬切。"""
    from .subtitle_engine import _char_count

    text = (text or "").strip()
    if limit <= 0:
        return ""
    if _char_count(text) <= limit:
        return text
    out, used = [], 0
    for seg in _sentences(text):
        if out and used + _char_count(seg) > limit:
            break
        out.append(seg)
        used += _char_count(seg)
    kept = "".join(out).strip()
    if not out or _char_count(kept) > limit:
        # 單一長句塞不下 → 在最後一個標點 / 逗號處切，切完補句號
        cut = kept or _sentences(text)[0]
        while _char_count(cut) > limit:
            m = list(re.finditer(r"[，、；：。！？,;:]", cut[: max(1, limit)]))
            cut = cut[: m[-1].start()] if m else cut[: max(1, limit)]
        kept = cut.rstrip("，、；：,;: ") + "。"
    return kept


class ScriptEngine:
    """
    劇本與口白編譯引擎：
    彙整照片/影片的 iOS 說明欄記憶、時間、地點、視覺分析與 Live Photo 資訊，
    依據使用者的 Prompt 需求，編譯成具備專業分鏡、口白、轉場與音樂音效指示的影片剪輯腳本。
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or config.gemini_api_key
        self.client = None
        if self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[Warning] ScriptEngine GenAI Client 初始化失敗: {e}")

    def generate_script(
        self,
        media_items_with_analysis: List[Dict[str, Any]],
        user_prompt: str = "請幫我編寫一段溫馨感人的旅行生活紀錄片腳本",
        target_duration_seconds: Optional[int] = None,
        style: str = "溫馨感人",
        target_aspect_ratio: str = "16:9",
        story_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        生成完整影片腳本資料結構
        """
        # 1. 若有 Gemini Client，調用 AI 進行深度劇本創作
        if self.client:
            try:
                script = self._generate_with_gemini(
                    media_items_with_analysis,
                    user_prompt,
                    target_duration_seconds,
                    style,
                    target_aspect_ratio,
                    story_context=story_context
                )
                if script:
                    return script
            except Exception as e:
                print(f"[Warning] Gemini 劇本生成失敗: {e}，切換至本機構圖引擎")

        # 2. Fallback: 本地智慧腳本編譯
        return self._local_script_compiler(
            media_items_with_analysis,
            user_prompt,
            target_duration_seconds,
            style,
            target_aspect_ratio
        )

    def _generate_with_gemini(
        self,
        media_items: List[Dict[str, Any]],
        user_prompt: str,
        target_duration: Optional[int],
        style: str,
        target_aspect_ratio: str,
        story_context: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """利用 Gemini 進行高水準劇本編寫"""
        # 整理關鍵素材資料給 AI（優先保留有說明欄備註、Live Photo 與影片的精彩鏡頭）
        items_summary = []
        captioned_items = [it for it in media_items if it.get("caption")]
        # 如果有備註的素材很多，優先全數納入；其他無備註素材抽樣補足
        selected_items = list(captioned_items)
        if len(selected_items) < 30:
            for it in media_items:
                if it not in selected_items and (it.get("is_live_photo") or it.get("is_video")):
                    selected_items.append(it)
                    if len(selected_items) >= 40:
                        break

        # 依時間重新排序
        selected_items.sort(key=lambda x: str(x.get("creation_date") or ""))

        for idx, item in enumerate(selected_items, start=1):
            analysis = item.get("analysis", {})
            crop = analysis.get("crop_suggestion", {})
            motion = analysis.get("camera_motion_suggestion", {})
            loc = item.get("location", {}).get("short_location", "未標記地點") if item.get("location") else "未標記地點"
            
            items_summary.append({
                "shot_index": idx,
                "file_name": item.get("file_name"),
                "file_path": item.get("file_path"),
                "media_type": item.get("media_type"),
                "is_live_photo": item.get("is_live_photo", False),
                "user_caption_memory": item.get("caption", "（無輸入特定備註）"),
                "taken_time": item.get("creation_date_formatted"),
                "location": loc,
                "visual_subject": analysis.get("visual_subject", ""),
                "shot_type": analysis.get("shot_type", ""),
                "crop_reason": crop.get("crop_reason", ""),
                "camera_motion": motion.get("motion_type", "Slow Zoom-in"),
                "live_photo_advice": analysis.get("live_photo_advice"),
                "apple_scene_labels": (item.get("labels") or [])[:8],   # Apple ML 場景標籤
                "people_in_frame": item.get("persons") or [],           # 入鏡人物
            })

        # 全片 Apple ML 標籤直方圖 → 給 AI 判斷主題比重與章節骨架
        from collections import Counter
        label_hist = Counter()
        for it in selected_items:
            for lb in (it.get("labels") or []):
                label_hist[lb] += 1
        top_themes = "、".join(f"{k}×{v}" for k, v in label_hist.most_common(15)) or "（無標籤資料）"

        system_instruction = """
你是一位奧斯卡級紀錄片編劇兼頂級影片剪輯導演。
你的任務是將使用者在 iOS 相簿中每張照片/影片寫下的【說明欄回憶（caption）】、拍攝時間脈絡、GPS 地名與 Live Photo 動態，
結合使用者的【客製需求 Prompt】與【全片旅程心得長文】，編譯成一份具備強烈情感共鳴、分鏡畫面感極強的專業影片剪輯腳本（Movie Script & Storyboard）。

【核心創作原則】：
1. 💡 說明欄靈魂轉化與心得融合（Caption & Philosophy to Voiceover）：
   - 使用者在說明欄寫下的想法是影片的靈魂，而全片心得長文是精神支柱。
   - 請將心得中的核心思想（如人生的體悟、為什麼出發、面對困難的心態、與孩子的陪伴成長、失敗也是一種結果）提煉為感人金句，在關鍵段落（開場引言、轉折、結尾昇華）中作為旁白口白，並與照片中具體的場景備註交織融合。
   - 口白要有起承轉合：開頭引人入勝、中段層層鋪陳、結尾餘韻悠長。
   - 適度留白：不是每個鏡頭都要說話，部分畫面標註「[留白，專注於現場環境音與配樂]」，讓節奏有呼吸感。
2. 💫 Live Photo 深度動態運用：
   - Live Photo 含有 1.5s~3s 的微動態。請針對該畫面給出精確剪輯手法（例如：「前 1.5 秒播放微動態並收錄現場風聲/水流聲，隨後畫面定格並緩慢 Zoom in，同時進入口白」）。
3. 📐 取景與鏡頭動態（Framing & Ken Burns）：
   - 依據目標比例給出最佳景別（特寫/中景/全景）與鏡頭運動（如緩慢推近 Slow Zoom-in、水平搖鏡 Pan、俯仰 Tilt）。
4. 🎵 聲音與環境音效設計（Sound Design）：
   - 為每個鏡頭設計環境音 (Ambience)、特定音效 (SFX cue) 與配樂 (BGM) 起伏。
"""

        reflection_section = f"\n【旅程全局核心心得與感悟（全片精神主軸）】\n{story_context.strip()}\n" if story_context else ""

        user_content = f"""
【使用者需求與風格設定】
- 需求 Prompt 指令："{user_prompt}"
- 期望風格語調：{style}
- 目標影片比例：{target_aspect_ratio}
- 期望總片長：{f'{target_duration} 秒' if target_duration else '由素材自然決定（約 1~3 分鐘）'}
{reflection_section}
【Apple 影像辨識：全片場景主題比重（用來抓章節骨架與各段情緒）】
{top_themes}

【素材時間軸與說明欄回憶清單】（每則附 apple_scene_labels 場景標籤、people_in_frame 入鏡人物）
{json.dumps(items_summary, ensure_ascii=False, indent=2)}

請輸出嚴格符合以下 JSON 格式的完整專業腳本（請勿包含任何額外雜訊）：
{{
  "project_title": "影片主標題（富有詩意或電影感）",
  "subtitle": "影片副標題（一句話點題）",
  "narrative_logline": "故事一句話梗概",
  "theme_summary": "故事主題大綱、情緒起伏曲線與情感核心闡述",
  "target_aspect_ratio": "{target_aspect_ratio}",
  "estimated_total_duration": "預估總秒數（整數）",
  "soundtrack_design": {{
    "overall_mood": "整體音樂氛圍與曲風走向",
    "recommended_tracks": "推薦曲風、樂器編制或配樂關鍵字（例如：Acoustic Guitar, Cinematic Cello, Ambient Lo-Fi）",
    "audio_dynamics": "音樂起承轉合設計（鋪陳、高潮、結尾漸弱）"
  }},
  "director_notes": "導演剪輯手記：給剪輯師的節奏、調色 (Color Grading) 與氛圍把控建議",
  "storyboard": [
    {{
      "shot_index": 1,
      "scene_title": "段落名稱（例如：序幕：清晨微風中的啟程）",
      "media_file": "檔案名稱",
      "media_type": "image / live_photo / video",
      "is_live_photo": true/false,
      "live_photo_usage": "Live Photo 動態運用建議（若非 Live Photo 則為 null）",
      "duration_seconds": 4.0,
      "shot_type": "特寫 (Close-up) / 中景 (Medium Shot) / 全景 (Wide Shot)",
      "visual_description": "畫面視覺焦點與剪輯畫面描述",
      "crop_focus": "裁切建議說明",
      "camera_motion": "Slow Zoom-in / Slow Zoom-out / Pan Left-to-Right 等",
      "voiceover": "該分鏡對應的配音旁白台詞（若該鏡頭留白聽音樂則寫 [留白，專注於現場環境音與配樂]）",
      "bgm_cue": "此處音樂情緒（例如：吉他單音輕輕切入，營造平靜氛圍）",
      "sfx_cue": "環境音效提示（例如：行李箱輪子滑過地面的聲音、微風聲）",
      "transition": "淡入 (Fade In) / 交叉溶解 (Cross Dissolve) / 硬切 (Cut) / 快速推鏡轉場"
    }}
  ]
}}
"""

        models_to_try = ["gemini-2.5-flash-lite", config.script_model, "gemini-2.5-flash"]
        for model_name in models_to_try:
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=[user_content],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json"
                    )
                )
                if response.text:
                    try:
                        return json.loads(response.text)
                    except json.JSONDecodeError:
                        pass
            except Exception as e:
                print(f"[Warning] 模型 {model_name} 調用失敗: {e}，嘗試下一個可用模型...")
                continue
        return None

    def regenerate_voiceover(
        self,
        layout: List[Dict[str, Any]],
        script_meta: Dict[str, Any],
        total_seconds: float,
        story_context: Optional[str] = None,
        coverage: float = 0.6,
        polish: bool = True,
    ) -> int:
        """就地重寫每個鏡頭的 voiceover，配合「重新剪過的」時間軸。

        **畫面是主角**：旁白按「章節（scene）」分組編寫，每個 scene 有它的
        總秒數與字數上限（scene 秒數 × 6字/秒 × coverage），其餘時間留白給
        環境音與畫面呼吸；純風景/蒙太奇 scene 可完全不放旁白。

        兩段式 LLM：① 照 scene 預算 + seam 寫初稿；② `polish=True` 時把整份初稿
        依鏡頭順序丟回去做**主述校訂**（修「我／我們」超標、句式單調、
        報地名報日期），只准同長或更短。之後才做**確定性裁切**（逐 scene 不超
        總預算、逐鏡頭念得完），不硬信 LLM。

        layout: [{shot, start, dur}] 來自 timeline_layout（已濾 skip、依序）。
        回傳有台詞的鏡頭數；失敗回傳 -1（呼叫端保留原旁白）。
        """
        if not self.client:
            return -1

        from .subtitle_engine import _char_count

        # 1) 把 layout 依章節切成連續的 scene。
        #    Gemini 有好好分【章節】的（敘事片）→ 用 scene_title；
        #    否則用 segment_scenes.py 寫的 scene_id（機械訊號，chrono/蒙太奇/生素材較穩）。
        marked = sum(1 for e in layout
                     if re.search(r"[【\[][^】\]]+[】\]]", e["shot"].get("scene_title", "") or ""))
        use_title = marked > len(layout) * 0.6 or not any(
            e["shot"].get("scene_id") is not None for e in layout)

        scenes: List[Dict[str, Any]] = []
        for i, e in enumerate(layout, start=1):
            s = e["shot"]
            if use_title:
                key = _chapter_key(s.get("scene_title", ""))
                title = s.get("scene_title", "")
            else:
                key = s.get("scene_id", _chapter_key(s.get("scene_title", "")))
                title = s.get("scene_name") or s.get("scene_title", "")
            if not scenes or scenes[-1]["key"] != key:
                scenes.append({"key": key, "title": title, "entries": []})
            scenes[-1]["entries"].append((i, e))

        # 2) 每個 scene 一份「總秒數 + 字數上限 + 鏡頭清單」
        scenes_payload = []
        for si, sc in enumerate(scenes, start=1):
            sec = sum(e["dur"] for _, e in sc["entries"])
            sc["seconds"] = sec
            sc["char_budget"] = max(0, int(sec * _READING_CPS * coverage))
            n_sh = len(sc["entries"])
            scenes_payload.append({
                "scene": si,
                "chapter": sc["title"],
                "seconds": round(sec, 1),
                "char_budget": sc["char_budget"],
                "shots": [{
                    "i": idx,
                    "len": round(e["dur"], 1),
                    "seam": ("章首·接住上一章" if k == 0 and si > 1 else
                             "章尾·留鉤子" if k == n_sh - 1 and si < len(scenes) else ""),
                    "visual": (e["shot"].get("visual_action")
                               or e["shot"].get("visual_description") or "")[:60],
                    "orig_vo": (e["shot"].get("voiceover") or "")[:80],
                } for k, (idx, e) in enumerate(sc["entries"])],
            })

        system_instruction = f"""你是紀錄片旁白編劇。**畫面是主角，旁白只是填進畫面之間的空隙，不是主線。**
這是一支個人紀錄片「重新剪過」的分鏡表，已按章節（scene）分組，每個 scene 有它的
總秒數與字數上限。規則：
1. 逐 scene 處理。每個 scene 的旁白**總字數不可超過該 scene 的 char_budget**
   （= scene 秒數 × 6字/秒 × {coverage:.0%}，剩下時間留給環境音與畫面呼吸）。
2. 有些 scene 適合**完全不放旁白**（純風景、蒙太奇快剪、情緒定格）→ 就不要為它寫任何句子。
   寧可整支片子一半以上鏡頭留白。
3. 每一句旁白掛在該 scene 內某一顆鏡頭 i 上，且那句的字數要能在該鏡頭的 len 秒內
   以每秒 6 字念完（念不完就寫更短，或拆給同 scene 相鄰鏡頭）。
4. 全片連貫、有起承轉合；開場勾人、結尾昇華。可參考 orig_vo 的精神，不要照抄、不要每顆都寫。
5. 保留原片精神：從 logline / theme / reflection 抓出這支片子真正在講的那件事，別漂走。
6. **章與章之間的縫（seam）**：
   - 標「章尾·留鉤子」的鏡頭：那一章最後一句留懸念或情緒未完（提問、預告、轉折的前半），不要把話講死。
     也可以直接留白（不寫），讓畫面收尾。
   - 標「章首·接住上一章」的鏡頭：第一句接住上一章的鉤子、或明確翻轉（時間/地點/心境的跳接），
     像「翻過這個埡口，就是縱谷」。不要每章都用同一種句式。
   - 章首常是空景 / establishing / 地圖片段 → 那句就當「到了哪裡、心情如何」的過場，短。
7. 相鄰兩章的鉤子+接句要像一組對話，一放一收；不要兩句都在總結、也不要兩句都在提問。
8. **主述不要吵**：這是第一人稱旁白，「我」是預設發話者，能省就省，不要每句「我…」。
   第一人稱複數（我們／兩人）當主語**全片最多 1 次**；「我」開頭的句子**全片 ≤5 句**，
   只放情緒轉折處。其餘一律零主語，或把主語換成景物 / 動作 / 物件。
9. 交代地名、日期、第幾天 → **不要用旁白念**（交給畫面字卡），那句留白或只寫心情。
10. 連續兩句不要同一種開頭；可用第二人稱直接對畫面裡的人說（「你睡著的時候…」）、
   側寫（前面那個背影）、無主語名詞短句（「濕的柏油。逆風。」）。
只輸出 JSON：{{"vo": [{{"i": 鏡頭編號, "text": "旁白"}} ...]}}（只列有旁白的鏡頭）。"""

        user_content = json.dumps({
            "logline": script_meta.get("narrative_logline", ""),
            "theme": script_meta.get("theme_summary", ""),
            "reflection": (story_context or "")[:1500],
            "total_runtime": f"{total_seconds:.0f}s",
            "scenes": scenes_payload,
        }, ensure_ascii=False)

        result = None
        for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
            try:
                resp = self.client.models.generate_content(
                    model=model_name,
                    contents=[user_content],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                    ),
                )
                if resp.text:
                    result = json.loads(resp.text)
                    break
            except Exception as e:
                print(f"[regen-vo] {model_name} 失敗: {str(e)[:100]}")
                continue
        if not result or "vo" not in result:
            return -1

        raw_vo = {int(v["i"]): str(v.get("text", "")).strip()
                  for v in result["vo"] if v.get("text")}

        # 2.5) 第二段 LLM 校訂：修主述重複 / 句式單調 / 我·我們超標（只改口氣，不加資訊）。
        #      只接受「同長或更短」的改寫，廢話句可被清空；校訂失敗 → 沿用初稿。
        if polish and len(raw_vo) >= 4:
            polished = self._polish_voiceover_voice(
                [{"i": i, "text": raw_vo[i]} for i in sorted(raw_vo)])
            if polished:
                kept = 0
                for i, t in polished.items():
                    if i in raw_vo and _char_count(t) <= _char_count(raw_vo[i]):
                        raw_vo[i] = t
                        kept += 1
                raw_vo = {i: t for i, t in raw_vo.items() if t}
                print(f"[regen-vo] 主述校訂：{kept} 則採用改寫、{len(raw_vo)} 則保留")

        # 3) 確定性處理（不硬信 LLM）：一章的句子照鏡頭順序接成一條敘述 →
        #    裁到 char_budget → 依每顆鏡頭長度比例分回各鏡頭（整句、依序、不跨章）。
        from ..analyzers.motion_stability import _distribute_text

        new_vo: Dict[int, str] = {}
        over_scenes = 0
        for sc in scenes:
            lines = [raw_vo[idx] for idx, _ in sc["entries"] if raw_vo.get(idx)]
            if not lines:
                continue
            blob = "".join(lines)
            if _char_count(blob) > sc["char_budget"]:
                blob = _trim_to_chars(blob, sc["char_budget"])
                if not blob:
                    over_scenes += 1
            durs = [e["dur"] for _, e in sc["entries"]]
            for (idx, _), part in zip(sc["entries"], _distribute_text(blob, durs)):
                if part.strip():
                    new_vo[idx] = part.strip()

        n = 0
        for i, e in enumerate(layout, start=1):
            e["shot"]["voiceover"] = new_vo.get(i, "")
            e["shot"].pop("timed_subtitles", None)
            if new_vo.get(i):
                n += 1
        total_chars = sum(_char_count(v) for v in new_vo.values())
        print(f"[regen-vo] {n}/{len(layout)} 顆有旁白｜{len(scenes)} 章｜共 {total_chars} 字"
              f"（全片預算上限 {sum(s['char_budget'] for s in scenes)}）")
        return n

    def _polish_voiceover_voice(
        self,
        ordered: List[Dict[str, Any]],
    ) -> Optional[Dict[int, str]]:
        """第二段 LLM 校訂：只修「主述重複 / 句式單調 / 我·我們超標」，不加新資訊。

        ordered: [{"i": 鏡頭號, "text": 初稿旁白}]，全片、依鏡頭順序。
        回傳 {i: 校訂後旁白}（可含空字串＝該句刪掉）；失敗或格式不對回傳 None。
        """
        if not self.client or not ordered:
            return None

        system_instruction = """你是紀錄片旁白的校訂編輯。收到一份「內容已定、只差口氣」的第一人稱旁白初稿（依鏡頭順序）。
你的**唯一任務**是讓「主述」不吵，不要重寫內容：

1. 這是第一人稱旁白 →「我」是預設發話者，能省就省，不要每句「我…」。
2. 第一人稱複數（我們／兩人）當主語 → 整份最多留 1 次；其餘改寫（零主語，或改講景物 / 動作 / 物件）。
3. 只在報地名、日期、第幾天的句子 → 刪掉那部分（交給畫面字卡），留白或只留心情。
4. 連續句子不要同一種開頭；多用時間 / 地點 / 景物 / 動作起句，主語埋句中或拿掉。
5. 可用第二人稱直接對畫面裡的人說（「你睡著的時候…」）、側寫（前面那個背影）。
6. **只改口氣與主語**，不加任何原文沒有的事實或情緒；每則字數**只能相同或更短**。
7. 某則整句都是廢話（如「他們繼續前進」「熟練地綁好行李」）→ 那則 text 設為空字串 ""。

只輸出 JSON：{"vo":[{"i":鏡頭號,"text":"校訂後旁白"} ...]}，i 沿用輸入、順序不變、每則都要列（含清空的）。"""

        draft = json.dumps({"vo": ordered}, ensure_ascii=False)
        for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
            try:
                resp = self.client.models.generate_content(
                    model=model_name,
                    contents=[draft],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                    ),
                )
                if resp.text:
                    data = json.loads(resp.text)
                    out = {int(v["i"]): str(v.get("text", "")).strip()
                           for v in data.get("vo", []) if "i" in v}
                    if out:
                        return out
            except Exception as e:
                print(f"[regen-vo] 主述校訂 {model_name} 失敗: {str(e)[:100]}")
                continue
        return None

    def _local_script_compiler(
        self,
        media_items: List[Dict[str, Any]],
        user_prompt: str,
        target_duration: Optional[int],
        style: str,
        target_aspect_ratio: str
    ) -> Dict[str, Any]:
        """本地啟發式腳本編譯引擎"""
        total_items = len(media_items)
        avg_duration = 4.0
        if target_duration and total_items > 0:
            avg_duration = max(2.5, min(7.0, target_duration / total_items))

        storyboard = []
        total_sec = 0.0

        for idx, item in enumerate(media_items, start=1):
            caption = item.get("caption", "").strip()
            loc_name = item.get("location", {}).get("short_location") if item.get("location") else ""
            analysis = item.get("analysis", {})
            crop = analysis.get("crop_suggestion", {})
            motion = analysis.get("camera_motion_suggestion", {})
            is_live = item.get("is_live_photo", False)
            
            dur = item.get("duration") or avg_duration
            total_sec += dur

            # 產生段落標題
            if idx == 1:
                scene_title = "序幕：故事的開端"
                transition = "淡入 (Fade In)"
            elif idx == total_items:
                scene_title = "終章：難忘的定格"
                transition = "淡出 (Fade Out)"
            else:
                scene_title = f"篇章 {idx}：{loc_name if loc_name else '記憶碎片'}"
                transition = "交叉溶解 (Cross Dissolve)"

            # 生成旁白
            if caption:
                voiceover = f"「{caption}」"
            else:
                voiceover = f"在{loc_name}的這一刻，時間彷彿慢了下來。" if loc_name else "那些沒有說出口的畫面，都悄悄留在了記憶裡。"

            storyboard.append({
                "shot_index": idx,
                "scene_title": scene_title,
                "media_file": item.get("file_name"),
                "file_path": item.get("file_path"),
                "media_type": item.get("media_type", "image"),
                "is_live_photo": is_live,
                "live_photo_usage": "前 1.5 秒播放微動態，隨後平滑過渡至定格照片" if is_live else None,
                "duration_seconds": round(dur, 1),
                "shot_type": analysis.get("shot_type", "中景 (Medium Shot)"),
                "visual_description": f"畫面展示 {analysis.get('visual_subject', item.get('file_name'))}，強調主體氛圍",
                "crop_focus": crop.get("crop_reason", f"針對 {target_aspect_ratio} 畫面進行構圖安全裁切"),
                "camera_motion": motion.get("motion_type", "Slow Zoom-in"),
                "voiceover": voiceover,
                "bgm_cue": "溫暖柔和的背景音樂，襯托畫面情感",
                "sfx_cue": "快門聲 / 環境環境音",
                "transition": transition
            })

        return {
            "project_title": "記憶時光剪影 (Memories in Motion)",
            "subtitle": f"依據「{user_prompt}」編排之剪片腳本",
            "theme_summary": f"融合 {total_items} 個珍貴片段與說明欄回憶，打造一段{style}的故事短片。",
            "target_aspect_ratio": target_aspect_ratio,
            "estimated_total_duration": round(total_sec, 1),
            "soundtrack_design": {
                "overall_mood": f"{style}配樂，推薦輕柔鋼琴與溫暖木吉他交織",
                "recommended_tracks": "Acoustic Folk, Cinematic Piano, Chill Lo-Fi",
                "audio_dynamics": "開頭靜謐引入，中段節奏豐富，結尾餘音悠長"
            },
            "storyboard": storyboard
        }
