#!/usr/bin/env python3
"""三段 Gemini 口白（故事書模式）—— 參考片的實際腳本，當作三軌字幕做法的範例讀，非開箱即用。

事實來源（一律不得捏造）：
  1. 心得原文 reflection.md（放在素材夾）    —— 非場景內的人生體悟
  2. 每顆素材的 iOS 說明欄 caption          —— 場景內發生什麼事、心情
  3. photos_meta 的 place / 日期 / 海拔      —— Day N、地名

三種字幕：
  - annotation 場景註解：**第一人稱**，像故事書 —— 發生什麼事、心情是什麼、接下來要幹嘛。
    場景內大量鋪，這是主體。取材自 caption，可用自己口氣講、不用逐字。
  - bridge 串接旁白：**第三人稱旁觀**，只放在每個場景的開頭，一句話交代時空、接住上一場。少。
  - reflection 感觸：**第一人稱**，心得原文裡「跟當下畫面無關」的體悟，只鋪在放長的「畫布」鏡頭上
    （is_canvas=true），慢慢浮現、留白多。Pass 3 通讀整篇心得 → 寫成一段連續內心話 →
    依旅程情緒弧線（開場期待 → 路上掙扎 → 夕陽領悟 → 收尾）切給各畫布，不一開場就丟最重的體悟。

輸入：素材夾內 reflection.md（心得長文）＋ scratch/extracted_captions.json（[{stem, caption}]）。
用法：
  .venv/bin/python examples/reference-film/gen_vo_gemini.py --plan-only
  .venv/bin/python examples/reference-film/gen_vo_gemini.py --write
  之後：patch_vo.py --write → rebuild（不要 --regen-vo）→ render_video.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts._config import MEDIA_DIR, PREFIX, SEARCH_DIRS  # noqa: E402
from backend.exporters.timeline_layout import timeline_layout  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
CPS = 6.0
ITEMS_JSON = ROOT / "scratch" / "extracted_captions.json"
ESSAY = MEDIA_DIR / "reflection.md"
MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]

KIND_WHITE = ("annotation", "bridge")

# 6 顆畫布鏡頭各自承載心得原文的哪一段（**逐字**摘自 2026年環島旅行心得.md）。
# Pass 3 只能把「指定的這段」順成通順句子 —— 不靠 LLM 亂挑，才不會跨畫布重複、才守得住原意。
CANVAS_THEME: dict[int, str] = {
    # 1: "……（逐字摘自 reflection.md 的一段）……",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s，、。！？：；「」『』（）()\-—…·.!?,;:\"']", "", s or "")


def load_facts() -> tuple[str, dict[str, str]]:
    essay = ""
    if ESSAY.exists():
        essay = ESSAY.read_text("utf-8")
    else:
        # 自動在專案目錄或 MEDIA_DIR 尋找心得、筆記或分鏡腳本大綱
        candidates = list(ROOT.glob("*reflection*.md")) + list(ROOT.glob("*notes*.md")) + list(ROOT.glob("*心得*.md"))
        if MEDIA_DIR.exists():
            candidates += list(MEDIA_DIR.glob("*心得*.md")) + list(MEDIA_DIR.glob("*.md"))
        for cand in candidates:
            if cand.is_file() and cand.stat().st_size > 50:
                txt = cand.read_text("utf-8")
                if "企劃與故事大綱" in txt or "故事一句話梗概" in txt:
                    essay = txt[:4000]
                    break
                elif len(txt) > len(essay):
                    essay = txt

    items = json.loads(ITEMS_JSON.read_text("utf-8")) if ITEMS_JSON.exists() else []
    caps = {it["stem"]: (it.get("caption") or "").strip() for it in items}
    return essay, caps


def build_scenes(data: dict, caps: dict[str, str]) -> list[dict]:
    from backend.util.photos_meta import load_photos_meta, meta_for
    pm = load_photos_meta()
    layout = timeline_layout(data, SEARCH_DIRS)
    dts = sorted({(meta_for(e["shot"], pm).get("taken") or "")[:10]
                  for e in layout if meta_for(e["shot"], pm).get("taken")})
    day_rank = {d: i + 1 for i, d in enumerate(dts)}

    scenes: list[dict] = []
    for e in layout:
        s = e["shot"]
        if s.get("intro_card"):          # 片頭地圖動畫：不寫旁白 / 註解
            continue
        sid = s.get("scene_id")
        disp = (s.get("scene_title") or s.get("scene_name") or f"場景{sid}").strip()
        if not scenes or scenes[-1]["sid"] != sid:
            scenes.append({"sid": sid, "name": disp, "entries": [], "t0": e["start"]})
        scenes[-1]["entries"].append(e)
        scenes[-1]["t1"] = e["start"] + e["dur"]

    out = []
    for k, sc in enumerate(scenes, start=1):
        ents = sc["entries"]
        recs = [meta_for(e["shot"], pm) for e in ents]
        dates = [r.get("taken", "")[:10] for r in recs if r.get("taken")]
        day = day_rank.get(dates[0]) if dates else None
        places = []
        for r in recs:
            p = (r.get("place") or {}) if isinstance(r.get("place"), dict) else {}
            nm = (p.get("name") or "").split("·")[0]
            if nm and nm not in places:
                places.append(nm)
        seen: set[str] = set()
        shots, facts, canvases = [], [], []
        for e, r in zip(ents, recs):
            s = e["shot"]
            stem = Path(str(s.get("media_file", ""))).stem
            cap = caps.get(stem, "")
            first = stem not in seen
            seen.add(stem)
            pl = (r.get("place") or {}) if isinstance(r.get("place"), dict) else {}
            place_name = (pl.get("name") or "").replace("臺", "台")
            gps = r.get("gps") or {}
            alt = gps.get("alt") if isinstance(gps, dict) else None
            if first and cap:
                facts.append(cap)
            row = {
                "i": s.get("shot_index"), "stem": stem, "len": round(e["dur"], 1),
                "kind": ("定格" if "定格" in (s.get("visual_action") or "")
                         else "微動態" if "微動態" in (s.get("visual_action") or "")
                         else "影片" if s.get("media_type") == "video" else "照片"),
                "canvas": bool(s.get("is_canvas")),
                "place": place_name,
                "alt_m": int(alt) if alt else None,
            }
            shots.append(row)
            if s.get("is_canvas") and first:
                canvases.append({"i": s.get("shot_index"), "len": round(e["dur"], 1),
                                 "caption": cap})
        out.append({
            "scene": k, "sid": sc["sid"], "name": sc["name"], "day": day,
            "places": places[:4], "seconds": round(sc["t1"] - sc["t0"], 1),
            "facts": facts, "shots": shots, "canvases": canvases,
        })
    return out


def _client():
    import google.genai as genai
    from backend.config import config
    if not config.gemini_api_key:
        sys.exit("沒有 GEMINI_API_KEY")
    return genai.Client(api_key=config.gemini_api_key)


def _ask(client, system: str, user: str, temp: float = 0.5, retries: int = 4):
    from google.genai import types
    for attempt in range(retries):
        for model in MODELS:
            try:
                r = client.models.generate_content(
                    model=model, contents=[user],
                    config=types.GenerateContentConfig(
                        system_instruction=system, response_mime_type="application/json",
                        temperature=temp),
                )
                if r.text:
                    return json.loads(r.text)
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(2 + attempt * 3)
        time.sleep(1 + attempt * 2)
    return None


# ── 口語規範與檢驗字庫 ──────────────────────────────────────────
BANNED_WORDS = [
    "美不勝收", "風光明媚", "揮灑汗水", "不言而喻", "滿滿活力", "如詩如畫", "畫下完美句點",
    "進行一個", "迎接挑戰", "享用美食", "品嚐美味", "準備熟練", "令人屏息", "別有一番風味",
    "踏上嶄新的旅程", "然而", "隨後", "不僅如此", "總而言之", "隨之而來"
]

DISCOURSE_MARKERS = [
    "結果", "沒想到", "更扯的是", "誰知道", "老實說", "說真的", "那時候", "還好", "本來以為",
    "差點", "心裡在想", "你看", "一轉眼", "其實", "笑死", "直接"
]


def check_subtitle_qa(cues: list[tuple[int, str, str]], brief: dict) -> dict:
    """自動檢驗字幕品質：違禁詞、口語話語標記比例與異常句長。"""
    results: dict = {
        "total_cues": len(cues),
        "banned_hits": [],
        "marker_hits": 0,
        "avg_len": 0.0,
        "long_cues": [],
        "short_cues": [],
        "marker_ratio": 0.0,
    }
    if not cues:
        return results

    lens = []
    for shot_i, kind, text in cues:
        lens.append(len(text))
        if len(text) > 30:
            results["long_cues"].append((shot_i, text))
        elif len(text) < 6:
            results["short_cues"].append((shot_i, text))
        for b in BANNED_WORDS:
            if b in text:
                results["banned_hits"].append((shot_i, b, text))
        if any(m in text for m in DISCOURSE_MARKERS):
            results["marker_hits"] += 1

    results["avg_len"] = round(sum(lens) / len(lens), 1)
    results["marker_ratio"] = round(results["marker_hits"] / len(cues), 2)
    return results


# ── Pass 1：總導演通讀與主題定案 (Creative Brief) ───────────────────────
PASS1_SYS = """你是紀錄片總導演兼敘事總監。請通讀本片的所有「企劃備註／心得原文」與「全部場景素材（包含 Caption、GPS 地名、拍攝時間與時長）」。

你的核心任務是為這支影片提煉出一份【主題定案與口白風格手冊（Creative Brief）】，並規劃全片骨架。
這份手冊將直接決定後續口白編劇的「說話人設、聊天場合、語氣風格」，必須從素材真實發生的事情中提煉，不可生搬硬套特定模板。

只輸出 JSON 格式：
{
  "theme_title": "本片主題定案名稱（例：父子單車環島：突破舒適圈的公路試煉 / 京都散策：吃貨的療癒放空行）",
  "core_thesis": "這支影片最核心的一句話體悟／全片靈魂（例：有些事在保護傘下學不會，把手弄髒了才知冷熱）",
  "persona": {
    "speaker": "說話者的人設與心態（例：嘴硬心軟、注重磨練孩子但深愛兒子的父親 / 逃離高壓工作渴望放鬆的社畜）",
    "listener_and_setting": "最適合本片口白氛圍的真實聊天場合與傾聽對象（例：旅行回來後在熱炒店跟拜把兄弟喝啤酒聊天 / 在咖啡廳跟閨蜜吃下午茶）",
    "tone": "說話語氣基調（例：自嘲幽默、生活感強、帶點感嘆但真誠不說教）"
  },
  "voice_guidelines": {
    "key_phrases": ["適合該人設的口吻或常用字，例：'這小子'、'老實說'、'差點沒命'、'心裡一直在算'"],
    "narrative_focus": "敘事重點（例：著重突發狀況時兩人的真實心理OS與感官細節，少做空洞景點介紹）"
  },
  "arc": "一句話講全片情緒弧線（例：興奮期待啟程 -> 路上遭遇體力與意外考驗 -> 在夕陽與海風中沉澱釋然 -> 收穫傳承與溫暖）",
  "scenes": [
    {
      "scene": 1,
      "intent": "這場在講什麼（一句話交代事件進展，只根據事實）",
      "mood": "開頭情緒 → 結尾情緒（例：興奮急躁 → 狼狽但踏實）",
      "density": 0.45~0.8,
      "highlight": "這場最精彩或最狼狽的關鍵細節（引導口白發揮）"
    }
  ],
  "canvas_themes": [
    {
      "i": 鏡頭號,
      "theme": "這顆放長鏡頭承載哪一層體悟（從淺到深，不要一開始就丟最重的，最後夕陽/收尾放核心體悟）"
    }
  ]
}

規則：
- 必須完全依據傳入的 captions 與備註內容自動推理人設與情境。美食片就定案為吃貨與朋友聊天；登山片就定案為山友圍爐；親子片就定案為生活分享。
- density 全片平均約 0.55~0.65（日常0.55；情緒重/意外/對話多0.7~0.8；純風景0.4）。
- canvas_themes 必須彼此不同，湊成一條完整的內心暗線。
"""


def build_pass2_sys(brief: dict) -> str:
    """根據 Pass 1 產生的主題定案手冊，動態組裝 Pass 2 場景口白生成提示詞。"""
    persona = brief.get("persona", {}) if isinstance(brief, dict) else {}
    speaker = persona.get("speaker", "親歷這趟旅程的第一人稱主述者")
    listener_setting = persona.get("listener_and_setting", "旅行結束後跟最好的老朋友在輕鬆場合聊天")
    tone = persona.get("tone", "輕鬆、真誠、有生活感與幽默感，不說教")
    guidelines = brief.get("voice_guidelines", {}) if isinstance(brief, dict) else {}
    phrases = ", ".join(f"「{p}」" for p in guidelines.get("key_phrases", ["老實說", "結果", "那時候"]))
    core_thesis = brief.get("core_thesis", "") if isinstance(brief, dict) else ""
    focus = guidelines.get("narrative_focus", "著重真實反應與心理OS")

    return f"""你現在是口白編劇。請完全依循導演剛完成的【主題定案手冊】，為這一個小章節寫一段「連續、生動、像跟朋友聊天」的旁白字幕。

【導演主題定案】
- 你的說話身分：{speaker}
- 說話情境與場合：你正在【{listener_setting}】，一邊看著這些照片/影片，一邊跟對方【分享這段經歷】。
- 口吻風格：{tone}
- 核心靈魂背景：{core_thesis}
- 建議常運用的口語習慣：{phrases}
- 敘事關注點：{focus}

【聊天體的 4 大語言黃金法則】
1. 善用「話語標記」推進思維：
   每兩三句中，自然融入日常生活對話連接詞（例如：「結果」、「更扯的是」、「老實說」、「誰知道」、「那時候心裡想」、「還好有」），讓句子之間有因果與轉折，緊密咬合。
2. 用「感官細節與心理 OS」取代客觀陳述：
   - ❌ 客觀陳述：「氣溫很高，騎乘非常耗費體力。」
   -  感官與OS：「太陽毒到柏油路都在反光，踩到大腿發抖，心裡一直在算到底還有幾公里。」
3. 短句直陳，拒絕長前綴修飾：
   不要用一長串形容詞修飾名詞。把長句拆成兩句短句（每句 10~24 字），一口氣講完一個動作或情緒。
4. 自然人稱與微型故事：
   把這一章串成一段「有起承轉合」的小故事（原本想怎樣 -> 結果發生意外 -> 當下怎麼解決 -> 事後吐槽回味），不是每張照片配一句獨立圖說！

【嚴格禁用詞黑名單（違規直接視為失敗）】
- 嚴禁成語與四字文青詞：美不勝收、風光明媚、揮灑汗水、不言而喻、滿滿活力、如詩如畫、迎接挑戰、別有一番風味。
- 嚴禁公文與展覽說明牌腔：然而、隨後、不僅如此、總而言之、此處、進行一個...的動作、準備熟練、享用美食。

【輸出前必做：出聲朗讀測試】
每一句話在輸出前，請在心中模擬「唸給對面坐著的對方聽」：
- 這句話在該場合說出口會不會太裝腔作勢？會不會拗口？若是，立刻重寫成大白話！

輸出 JSON：
{{
  "story": "在該場合講給對方聽的完整口述段落（前後連貫，有頭有尾）",
  "cues": [
    {{"i": 鏡頭號, "kind": "bridge"|"annotation", "text": "說出口的一句大白話"}}
  ]
}}
規則：
- 第一句通常是 kind=bridge（第三人稱交代時空轉移，接住上一場）；後續為 kind=annotation（第一人稱）。
- 寧可少幾句、每句講完整，不要把句子剁碎。每句 10~26 字。
- canvas=true 的鏡頭不要放字（留給感觸軌）。
"""


def build_pass3_sys(brief: dict) -> str:
    """根據 Pass 1 產生的主題定案手冊，動態組裝 Pass 3 長鏡頭感觸獨白提示詞。"""
    persona = brief.get("persona", {}) if isinstance(brief, dict) else {}
    speaker = persona.get("speaker", "親歷這趟旅程的第一人稱主述者")
    core_thesis = brief.get("core_thesis", "") if isinstance(brief, dict) else ""

    return f"""你在為紀錄片的「畫布長鏡頭」（放很長的安靜風景或人物凝視鏡頭）寫感觸軌字幕。
這是一段深層的「第一人稱內心獨白」—— 【{speaker}】在夜深人靜時，看著窗外慢慢整理自己內心的體悟。

【核心靈魂】
- 本片的核心體悟：{core_thesis}
- 口氣：溫和、真誠、平實、有一點自省與感嘆。
- 這是「說出來的真心話」，不是「散文朗讀」或「大道理說教」。
- 每一句留有足夠的呼吸感，字字句句像在心裡沉澱後的低語。

【情緒階梯（不可顛倒）】
1. 開場（第 1 顆畫布）：帶著點期待與不安，輕鬆起頭 —— 為什麼想出發、想帶對方出來闖。
2. 路上（中間畫布）：遇到難關時的掙扎與自我懷疑 —— 看到對方快撐不住，懷疑自己是不是做錯了。
3. 頂峰（最長畫布/夕陽）：釋然與領悟 —— 輸贏不重要，看著對方學會自己站起來，這就是答案。
4. 尾聲（最後畫布）：回甘與篤定 —— 雖然精疲力盡，但絕對值得，還想再來一次。

硬規則：
1. 每句 8～24 字，完整的一句話，不用逗號結尾。
2. 同一顆畫布的句子之間不能重複同一層意思（每句要往前推進）。
3. 全部畫布連起來讀，是一條連貫不重複的心靈旅程。
4. 必須緊扣提供的 essay/心得筆記原意，不捏造不存在的感悟。

只輸出 JSON：{{"canvases":[{{"shot": 鏡頭號, "cues": ["完整平實的獨白句","推進一層的獨白句"]}}]}}
"""


def _trim(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    m = list(re.finditer(r"[，、；：。！？]", cut))
    return cut[:m[-1].end()].rstrip("，、；：") if m and m[-1].end() >= limit * 0.55 else ""


def verify_in(text: str, *sources: str) -> bool:
    """text 的正規化字串是否為某來源的連續子串（容許掐頭去尾各 6 字）。"""
    n = _norm(text)
    if len(n) < 5:
        return False
    hay = "".join(_norm(s) for s in sources)
    if n in hay:
        return True
    for a in range(min(7, len(n) - 4)):
        for b in range(min(7, len(n) - 4 - a)):
            if len(n) - a - b >= 6 and n[a:len(n) - b] in hay:
                return True
    return False


def grounded_in(text: str, essay: str, thresh: float = 0.7) -> bool:
    """感觸容許把心得原文順成通順句子 → 不查逐字，查「2-gram 重疊率」夠高
    （順過的句子大多數字組還在原文裡，憑空編的不會）。"""
    n = _norm(text)
    if len(n) < 6:
        return False
    hay = _norm(essay)
    if n in hay:
        return True
    grams = [n[i:i + 2] for i in range(len(n) - 1)]
    hit = sum(1 for g in grams if g in hay)
    return grams and hit / len(grams) >= thresh


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    data = json.loads((ROOT / f"{PREFIX}.json").read_text("utf-8"))
    essay, caps = load_facts()
    scenes = build_scenes(data, caps)
    client = _client()
    n_canvas = sum(len(s["canvases"]) for s in scenes)
    print(f"心得 {len(essay)} 字｜caption {len(caps)}｜場景 {len(scenes)}｜畫布鏡頭 {n_canvas}｜"
          f"片長 {sum(s['seconds'] for s in scenes)/60:.1f} 分\n")

    # ── Pass 1 ──
    p1 = _ask(client, PASS1_SYS, json.dumps({
        "essay": essay,
        "scenes": [{"scene": s["scene"], "name": s["name"], "day": s["day"],
                    "places": s["places"], "seconds": s["seconds"], "captions": s["facts"],
                    "canvas_shots": [c["i"] for c in s["canvases"]]} for s in scenes],
    }, ensure_ascii=False), temp=0.6)
    if not p1 or "scenes" not in p1:
        sys.exit("Pass 1 失敗")
    # 顯示主題定案手冊 (Creative Brief)
    print("=" * 64)
    print(f"🎬【主題定案】 {p1.get('theme_title', '未命名主題')}")
    print(f"💡【核心靈魂】 {p1.get('core_thesis', '')}")
    persona = p1.get("persona", {})
    print(f"👤【說話人設】 {persona.get('speaker', '')}")
    print(f"☕【聊天場合】 {persona.get('listener_and_setting', '')}")
    print(f"🗣️【說話語調】 {persona.get('tone', '')}")
    guidelines = p1.get("voice_guidelines", {})
    if guidelines.get("key_phrases"):
        print(f"💬【常用慣用詞】 {', '.join(guidelines.get('key_phrases', []))}")
    if guidelines.get("narrative_focus"):
        print(f"🎯【敘事重點】 {guidelines.get('narrative_focus', '')}")
    print(f"📈【全片弧線】 {p1.get('arc', '')}")
    print("=" * 64 + "\n")

    plan = {p["scene"]: p for p in p1["scenes"]}
    themes = {t["i"]: t.get("theme", "") for t in p1.get("canvas_themes", [])}
    for s in scenes:
        p = plan.get(s["scene"], {})
        s["density"] = max(0.4, min(0.82, float(p.get("density", 0.6))))
        s["intent"] = p.get("intent", "")
        s["mood"] = p.get("mood", "")
        s["highlight"] = p.get("highlight", "")
        canvas_sec = sum(c["len"] for c in s["canvases"])      # 畫布鏡頭的時間留給「感觸」軌，不算註解預算
        s["budget"] = int(max(0.0, s["seconds"] - canvas_sec) * CPS * s["density"])
        hl_str = f" 🔥{s['highlight'][:20]}" if s.get("highlight") else ""
        print(f" sc{s['scene']:<2} {s['name'][:18]:<18} {s['seconds']:5.1f}s d{s['density']:.2f} "
              f"預算{s['budget']:>3}  {s['mood'][:20]}{hl_str}")
    if themes:
        print("\n畫布暗線：")
        for i, th in themes.items():
            print(f"  #{i}: {th}")
    if args.plan_only:
        return

    vo: dict[int, tuple[str, str]] = {}          # shot_index -> (kind, text)
    canvas_cues: dict[int, list[tuple[float, str]]] = {}   # shot_index -> [(after_sec, text)]

    # ── Pass 2：逐場景動態聊天口白 ──
    print("\n[Pass 2] 場景口白生成（動態注入主題定案與聊天法則）…")
    pass2_sys = build_pass2_sys(p1)
    for s in scenes:
        n = len(s["shots"])
        shots_payload = [{
            **sh, "caption": caps.get(sh["stem"], "")[:240],
            "seam": ("章首" if j == 0 and s["scene"] > 1 else "章尾" if j == n - 1 else ""),
        } for j, sh in enumerate(s["shots"])]
        res = _ask(client, pass2_sys, json.dumps({
            "scene": s["scene"], "name": s["name"], "intent": s["intent"], "mood": s["mood"],
            "highlight": s.get("highlight", ""),
            "day": s["day"], "places": s["places"], "char_budget": s["budget"],
            "shots": shots_payload,
        }, ensure_ascii=False), temp=0.55)
        cues = (res or {}).get("cues", []) if isinstance(res, dict) else []
        s["_story"] = (res or {}).get("story", "") if isinstance(res, dict) else ""
        valid = {sh["i"]: sh["len"] for sh in s["shots"]}
        canvas_i = {sh["i"] for sh in s["shots"] if sh["canvas"]}
        used, kept, seen = 0, [], set()
        for c in cues:
            i, kind, text = c.get("i"), c.get("kind"), (c.get("text") or "").strip()
            if i not in valid or kind not in ("annotation", "bridge") or len(text) < 4:
                continue
            if i in canvas_i:                    # 畫布鏡頭不放註解
                continue
            # 每句字數上限：不綁單顆鏡頭秒數（字幕會跨快鏡頭延伸）。給一個寬裕的下限，
            # 讓完整句子活下來，別被剁成電報式短語。
            text = _trim(text, max(30, int(valid[i] * 8.0)))
            if not text or _norm(text) in seen:
                continue
            if used + len(text) > s["budget"] + 20:
                continue
            kept.append((i, kind, text)); seen.add(_norm(text)); used += len(text)
        # 串接放在該場景第一顆有效鏡頭；若那顆也被指派了註解，註解讓給下一顆（別互蓋）
        bridges = [(i, k, t) for i, k, t in kept if k == "bridge"]
        anns = [(i, k, t) for i, k, t in kept if k == "annotation"]
        if bridges:
            bi = bridges[0][0]
            moved = []
            for j, (i, k, t) in enumerate(anns):
                if i == bi:
                    nxt = next((sh["i"] for sh in s["shots"]
                                if sh["i"] not in {bi} | {x[0] for x in moved}
                                and sh["i"] not in canvas_i and sh["i"] > bi), None)
                    if nxt:
                        anns[j] = (nxt, k, t)
                moved.append(anns[j])
            anns = moved or anns
        for i, k, t in (bridges[:1] + anns):
            vo[i] = (k, t)
        na = len(anns)
        story = " ".join(t for _, _, t in (bridges[:1] + anns))
        print(f" sc{s['scene']:<2} {s['name'][:16]:<16} 註解 {na}｜串接 {len(bridges[:1])}｜{used}字")
        print(f"      ↳ {story[:90]}")

    # ── Pass 3：感觸軌 —— 通讀心得寫成一段連續內心話，跟著旅程情緒弧線切給各畫布 ──
    print("\n[Pass 3] 感觸…")
    used_refl: set[str] = set()
    canvas_order = [c for s in scenes for c in s["canvases"]]
    def _near_dup(a: str, b: str, thr: float = 0.6) -> bool:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return False
        if na in nb or nb in na:
            return True
        sa = {na[i:i + 2] for i in range(len(na) - 1)}
        sb = {nb[i:i + 2] for i in range(len(nb) - 1)}
        return bool(sa) and len(sa & sb) / min(len(sa), len(sb)) >= thr

    # 依「總窗口秒數」算每顆要幾句（~13s 一句），再按片中位置貼情緒 slot
    n_cv = len(canvas_order)
    peak_i = max(range(n_cv), key=lambda j: canvas_order[j]["len"]) if n_cv else -1
    cv_plan = []
    for j, c in enumerate(canvas_order):
        want_n = max(2, min(8, round(c["len"] / 13)))
        if c["len"] / (want_n + 1) < 4.0:
            want_n = max(2, int(c["len"] / 4.5))
        if j == 0:
            slot = "開場·期待、輕鬆、帶點興奮：想帶孩子出去闖一闖"
        elif j == n_cv - 1:
            slot = "收尾·值不值得、還想不想再帶他來一次（想）"
        elif j == peak_i:
            slot = "情緒最高點·領悟、釋然、滿足：看著他學會面對、把手弄髒才知冷熱"
        elif j < peak_i:
            slot = "路上·遇到難關、懷疑自己是不是逼太緊、孩子快撐不住的掙扎"
        else:
            slot = "接近尾聲·釋然、溫暖、旅程沉澱下來"
        cv_plan.append({"shot": c["i"], "seconds": round(c["len"], 1),
                        "want_lines": want_n, "slot": slot})

    total_cv = sum(c["len"] for c in canvas_order)
    print(f"  畫布總窗口 {total_cv:.0f}s / {n_cv} 顆 → 共 ~{sum(p['want_lines'] for p in cv_plan)} 句")

    got: dict[int, list[str]] = {p["shot"]: [] for p in cv_plan}
    pass3_sys = build_pass3_sys(p1)
    grounded_source = "\n".join(filter(None, [essay, *CANVAS_THEME.values(), *caps.values(), p1.get("core_thesis", "")]))
    for attempt in range(3):
        res = _ask(client, pass3_sys, json.dumps({
            "essay": essay or p1.get("core_thesis", ""), "canvases": cv_plan,
        }, ensure_ascii=False), temp=0.45 + attempt * 0.15)
        for cv in (res or {}).get("canvases", []) if isinstance(res, dict) else []:
            sh = cv.get("shot")
            if sh not in got:
                continue
            for t in cv.get("cues", []) or []:
                t = (t or "").strip().rstrip("，、")
                if not (6 <= len(t) <= 30) or not grounded_in(t, grounded_source, 0.25):
                    continue
                # 跨畫布 0.6；同一顆畫布內更嚴（0.45）—— 不准換句話說重講一次
                if any(_near_dup(t, u) for u in used_refl) or any(_near_dup(t, x, 0.45) for x in got[sh]):
                    continue
                got[sh].append(t)
                used_refl.add(_norm(t))
        slack = 0 if attempt < 2 else 1        # 前兩趟要湊滿，最後一趟容 1 句差
        if all(len(got[p["shot"]]) >= p["want_lines"] - slack for p in cv_plan):
            break

    # 補洞：某顆不夠 → 從 CANVAS_THEME / Pass1 theme 切句補上（保底，不會空）
    for p in cv_plan:
        sh, want_n = p["shot"], p["want_lines"]
        th = CANVAS_THEME.get(sh) or themes.get(sh, "")
        if len(got[sh]) < want_n and th:
            for seg in re.split(r"[。！？，、；]", th):
                seg = seg.strip().rstrip("，、；")
                if 6 <= len(seg) <= 30 and not any(_near_dup(seg, x) for x in got[sh]) \
                        and not any(_near_dup(seg, u) for u in used_refl):
                    got[sh].append(seg + "。"); used_refl.add(_norm(seg))
                if len(got[sh]) >= want_n:
                    break

    for c in canvas_order:
        texts = got[c["i"]][:max(2, min(8, round(c["len"] / 13)))]
        picks = []
        if texts:
            span0, span1 = 2.5, max(4.0, c["len"] - 2.5)
            step = (span1 - span0) / len(texts)
            for j, t in enumerate(texts):
                picks.append((round(span0 + step * j, 1), t))
            canvas_cues[c["i"]] = picks
        print(f"  #{c['i']:>3} ({c['len']:.0f}s) {len(picks)} 句  ↳ {' / '.join(texts)[:70]}")

    # ── 寫回 storyboard ──
    def _punct(t: str) -> str:
        t = (t or "").strip()
        if t and t[-1] not in "。！？…」』，、":
            t += "。"
        return t

    last_of: dict[int, int] = {}
    for idx, sh in enumerate(data["storyboard"]):
        last_of[sh.get("shot_index")] = idx

    n_ann = n_br = n_ref = 0
    for idx, sh in enumerate(data["storyboard"]):
        i = sh.get("shot_index")
        sh.pop("timed_subtitles", None)
        if last_of.get(i) != idx:
            sh["voiceover"] = ""; sh.pop("voiceover_kind", None); sh.pop("reflection_cues", None)
            continue
        if i in canvas_cues:
            # 畫布鏡頭：多句感觸，帶時間點 → 存 reflection_cues，voiceover 放第一句當摘要
            sh["reflection_cues"] = [{"t": t, "text": _punct(tx)} for t, tx in canvas_cues[i]]
            sh["voiceover"] = _punct(canvas_cues[i][0][1])
            sh["voiceover_kind"] = "reflection"
            n_ref += len(canvas_cues[i])
        elif i in vo:
            kind, text = vo[i]
            sh["voiceover"] = _punct(text)
            sh["voiceover_kind"] = kind
            sh.pop("reflection_cues", None)
            n_ann += kind == "annotation"
            n_br += kind == "bridge"
        else:
            sh["voiceover"] = ""; sh.pop("voiceover_kind", None); sh.pop("reflection_cues", None)

    print(f"\n註解 {n_ann}｜串接 {n_br}｜感觸 {n_ref} 句（{len(canvas_cues)} 顆畫布）")

    # ── 品質檢驗報告 (QA Report) ──
    all_cues_for_qa = []
    for i, (k, t) in vo.items():
        all_cues_for_qa.append((i, k, t))
    for i, cues in canvas_cues.items():
        for _, t in cues:
            all_cues_for_qa.append((i, "reflection", t))

    qa = check_subtitle_qa(all_cues_for_qa, p1)
    print("\n" + "=" * 64)
    print("📋【字幕口語品質檢驗報告 (Subtitle QA Report)】")
    print(f"  • 總字幕句數: {qa['total_cues']} 句 (平均長度: {qa['avg_len']} 字/句)")
    print(f"  • 口語話語標記使用率: {qa['marker_ratio']*100:.1f}% ({qa['marker_hits']}/{qa['total_cues']} 句融入自然口語轉折詞)")
    if qa["banned_hits"]:
        print(f"  ⚠️ 違禁詞命中 ({len(qa['banned_hits'])} 處):")
        for sh_i, b_word, txt in qa["banned_hits"][:5]:
            print(f"     - 鏡頭 #{sh_i} 含「{b_word}」: {txt}")
    else:
        print("  ✅ 違禁詞檢測: 0 命中 (成功過濾文青成語、公文腔與AI八股)")
    if qa["long_cues"]:
        print(f"  ℹ️ 超長句 (>30字，{len(qa['long_cues'])} 處): 建議確認或由 SubtitleEngine 自動斷行")
        for sh_i, txt in qa["long_cues"][:3]:
            print(f"     - 鏡頭 #{sh_i} ({len(txt)}字): {txt}")
    print("=" * 64)

    if not args.write:
        print("（預覽，加 --write）")
        return
    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print("✅ 寫回 → patch_vo.py --write（可選）→ rebuild（不要 --regen-vo）→ render_video.py")


if __name__ == "__main__":
    main()
