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
    （is_canvas=true），慢慢浮現、留白多。逐字或接近逐字取心得。

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
ITEMS_JSON = ROOT / "scratch" / "extracted_captions.json"   # [{"stem": ..., "caption": ...}]
ESSAY = MEDIA_DIR / "reflection.md"                          # 心得長文
MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]

KIND_WHITE = ("annotation", "bridge")

# 每顆「畫布」鏡頭承載心得原文的哪一段（**逐字**摘自 reflection.md）。
# Pass 3 只能把「指定的這段」順成通順句子 —— 不靠 LLM 亂挑，才不會跨畫布重複、才守得住原意。
# 參考片有 6 顆畫布；填成 {shot_index: "逐字心得段落"}。留空則 Pass 1 的 canvas_themes 接手。
CANVAS_THEME: dict[int, str] = {
    # 1: "……（逐字摘自 reflection.md 的一段）……",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s，、。！？：；「」『』（）()\-—…·.!?,;:\"']", "", s or "")


def load_facts() -> tuple[str, dict[str, str]]:
    essay = ESSAY.read_text("utf-8") if ESSAY.exists() else ""
    items = json.loads(ITEMS_JSON.read_text("utf-8"))
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


PASS1_SYS = """你是紀錄片總編劇。讀完一支個人紀錄片的「心得原文」與「全部場景」，排全片骨架。
只輸出 JSON：
{
 "arc": "一句話講全片情緒弧線",
 "scenes": [{"scene":1,
   "intent":"這場在講什麼（一句，只根據 caption/心得）",
   "mood":"開頭情緒 → 結尾情緒（例：興奮 → 疲憊但堅定）",
   "density": 0.35~0.7   // 場景註解要多密。日常→0.4；情緒重的關鍵場景→0.6~0.7；純風景蒙太奇→0.3
 }],
 "canvas_themes": [{"i": 鏡頭號, "theme":"這顆放長的鏡頭承載哪一段心得體悟（用心得原文的話講）"}]
}
規則：density 全片平均約 0.5。canvas 鏡頭的 theme 要彼此不同、湊成一條貫穿全片的暗線，結尾收束。
"""

PASS2_SYS = """你在幫「一個小章節」寫一段**連續的旁白故事**。

先看完這個章節所有鏡頭的 caption，在心裡拼出「這一章到底發生了什麼事」——
從進入這個章節到離開，有頭有尾、有轉折。然後把它寫成一小段**前後連貫、
讀起來就是在講一個完整故事**的話，再切成幾句、依鏡頭順序掛上去。

最重要的一條：把這個章節的字幕**從頭到尾連起來讀**，觀眾要能清楚知道
「這一章發生了什麼」。**不是每張照片配一句各自獨立的話** —— 那樣看完還是一頭霧水。
句與句之間要接得起來（時間推進、因果、對比），像一段文章，不是一串圖說。

每句要交代清楚 where / when / who / what，善用鏡頭的 place（GPS 地名）。
少用「我／我們」，用主述者講自己旅程的口氣。只能用 caption / metadata 寫到的事，不捏造。
好句：「車停在古戰場遺址，吃早餐補充能量。」「公路邊的餐廳，冷氣沙發座，補一頓。」
壞句（各自獨立、沒交代）：「我們都累了。」「真滿足！」「他看起來很得意。」

輸出 JSON：{
 "story": "把這一章的字幕連起來的完整故事（一段話，給你自己檢查連不連貫）",
 "cues": [{"i": 鏡頭號, "kind": "annotation"|"bridge", "text": "這一句"}]
}
- cues 依鏡頭順序；把所有 text 串起來，要 ≈ story（逐句對得上）。
- 第一句 kind=bridge：第三人稱，交代「第幾天、從哪到哪」，接住上一章。其餘 annotation。
- 給了字的鏡頭串起來必須是完整故事；沒故事推進作用的鏡頭就留白（別硬湊獨立句）。
- 每句字數 ≤ 該鏡頭 len 秒 × 6（念得完）。canvas=true 的鏡頭不要放字。
- 總字數 ≤ char_budget，但要把故事講完整（用到 7~9 成）。
"""

PASS3_SYS = """你在為「一顆放長的畫布鏡頭」寫第一人稱的人生感觸字幕。畫面是安靜的長鏡頭，
字幕一句一句慢慢浮現、之間留白。

theme 就是這顆鏡頭要講的那段心得原文（逐字摘自作者的心得長文）。你的工作是把 theme
**順成 want_lines 句通順、完整、一句接一句讀下去像一小段話**的字幕。
可以精簡、可以調整標點與語序讓它順口，但**不能改變原意、不能加 theme 沒有的意思**。
讀起來要像一個人靜靜把一段心裡話講完，不是把句子剪碎丟出來。

只輸出 JSON：{"cues":[{"kind":"reflection","text":"一句"}]}
硬規則：
1. 每句 6～24 字，是完整的一句話（語意完整），不是半截子句、不要用逗號結尾。
2. 給 want_lines 句（±1）。全部連起來讀是一小段通順、講得完的話，有起有結。
3. already_used_elsewhere 的意思別的畫布用過了 —— 換心得裡的其他段落。
4. 時間點不用你決定（會自動均勻鋪滿整顆鏡頭）。
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
    print("【弧線】", p1.get("arc", ""), "\n")
    plan = {p["scene"]: p for p in p1["scenes"]}
    themes = {t["i"]: t.get("theme", "") for t in p1.get("canvas_themes", [])}
    for s in scenes:
        p = plan.get(s["scene"], {})
        s["density"] = max(0.25, min(0.72, float(p.get("density", 0.5))))
        s["intent"] = p.get("intent", "")
        s["mood"] = p.get("mood", "")
        canvas_sec = sum(c["len"] for c in s["canvases"])      # 畫布鏡頭的時間留給「感觸」軌，不算註解預算
        s["budget"] = int(max(0.0, s["seconds"] - canvas_sec) * CPS * s["density"])
        print(f" sc{s['scene']:<2} {s['name'][:18]:<18} {s['seconds']:5.1f}s d{s['density']:.2f} "
              f"預算{s['budget']:>3}  {s['mood'][:26]}")
    if themes:
        print("\n畫布暗線：")
        for i, th in themes.items():
            print(f"  #{i}: {th}")
    if args.plan_only:
        return

    vo: dict[int, tuple[str, str]] = {}          # shot_index -> (kind, text)
    canvas_cues: dict[int, list[tuple[float, str]]] = {}   # shot_index -> [(after_sec, text)]

    # ── Pass 2：逐場景故事書註解 ──
    print("\n[Pass 2] 場景註解…")
    for s in scenes:
        n = len(s["shots"])
        shots_payload = [{
            **sh, "caption": caps.get(sh["stem"], "")[:240],
            "seam": ("章首" if j == 0 and s["scene"] > 1 else "章尾" if j == n - 1 else ""),
        } for j, sh in enumerate(s["shots"])]
        res = _ask(client, PASS2_SYS, json.dumps({
            "scene": s["scene"], "name": s["name"], "intent": s["intent"], "mood": s["mood"],
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
            text = _trim(text, int(valid[i] * 6.5))
            if not text or _norm(text) in seen:
                continue
            if used + len(text) > s["budget"] + 8:
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

    # ── Pass 3：畫布感觸（每顆畫布承載 CANVAS_THEME 指定的那段心得，Gemini 只負責順句）──
    print("\n[Pass 3] 感觸…")
    used_refl: set[str] = set()
    canvas_order = [(s, c) for s in scenes for c in s["canvases"]]
    def _near_dup(a: str, b: str) -> bool:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return False
        if na in nb or nb in na:
            return True
        sa = {na[i:i + 2] for i in range(len(na) - 1)}
        sb = {nb[i:i + 2] for i in range(len(nb) - 1)}
        return bool(sa) and len(sa & sb) / min(len(sa), len(sb)) >= 0.6

    for s, c in canvas_order:
        th = CANVAS_THEME.get(c["i"]) or themes.get(c["i"], "")
        want_n = max(2, min(8, round(c["len"] / 13)))    # ~13s 一句，短畫布最多 2 句
        gap = c["len"] / (want_n + 1)
        if gap < 4.0:                                    # 間隔太密就少放幾句
            want_n = max(2, int(c["len"] / 4.5))
        texts: list[str] = []
        for attempt in range(3):
            res = _ask(client, PASS3_SYS, json.dumps({
                "shot": c["i"], "length_sec": c["len"], "theme": th, "want_lines": want_n,
                "essay": essay,
                "already_used_elsewhere": sorted(used_refl)[:40],
            }, ensure_ascii=False), temp=0.4 + attempt * 0.2)
            for x in (res or {}).get("cues", []) if isinstance(res, dict) else []:
                t = (x.get("text") or "").strip()
                if not (6 <= len(t) <= 30) or not grounded_in(t, essay, 0.45):
                    continue
                if any(_near_dup(t, u) for u in used_refl) or any(_near_dup(t, x) for x in texts):
                    continue
                texts.append(t)
            if len(texts) >= want_n:
                break
        # 還不夠 → 把 theme（逐字心得）切句補上
        if len(texts) < want_n and th:
            for seg in re.split(r"[。！？]", th):
                seg = seg.strip().rstrip("，、；")
                if 6 <= len(seg) <= 26 and not any(_near_dup(seg, x) for x in texts) \
                        and not any(_near_dup(seg, u) for u in used_refl):
                    texts.append(seg + "。")
                if len(texts) >= want_n:
                    break
        texts = texts[:want_n]
        # 時間點自己均勻鋪滿整顆鏡頭（別信 LLM 的 after_sec）
        picks = []
        if texts:
            span0, span1 = 2.5, max(4.0, c["len"] - 2.5)
            step = (span1 - span0) / len(texts)
            for j, t in enumerate(texts):
                picks.append((round(span0 + step * j, 1), t))
                used_refl.add(_norm(t))
            canvas_cues[c["i"]] = picks
        print(f"  #{c['i']:>3} ({c['len']:.0f}s) {len(picks)}/{want_n} 句  «{th[:22]}»")

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
    if not args.write:
        print("（預覽，加 --write）")
        return
    for base in (ROOT, MEDIA_DIR):
        (base / f"{PREFIX}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print("✅ 寫回 → patch_vo.py --write（可選）→ rebuild（不要 --regen-vo）→ render_video.py")


if __name__ == "__main__":
    main()
