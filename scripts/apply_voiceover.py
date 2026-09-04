#!/usr/bin/env python3
"""把一份人工 / 別的 LLM 寫好的旁白套回 storyboard，並重新匯出。

`regenerate_voiceover` 的「不含 LLM 呼叫」版：Gemini 額度用完時，可以由 Claude Code
這個 session 直接寫旁白（見 --dump 印出的 payload + 規則），存成 vo.json 再套。

流程與 regenerate_voiceover 一致：逐鏡頭砍到念得完、逐 scene 不超總預算、只砍整句。

用法：
  # 1) 印出分章 payload（scene 秒數 / 字數上限 / 每鏡 seam·視覺·原旁白）
  .venv/bin/python scripts/apply_voiceover.py <prefix> --dump

  # 2) 寫 vo.json = {"vo":[{"i":鏡頭號,"text":"旁白"} ...]}，然後套用 + 重匯出
  .venv/bin/python scripts/apply_voiceover.py <prefix> --vo vo.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.engines.script_engine import _READING_CPS, _chapter_key, _trim_to_chars  # noqa: E402
from backend.engines.subtitle_engine import _char_count  # noqa: E402
from backend.exporters.fcpxml_exporter import FCPXMLExporter  # noqa: E402
from backend.exporters.markdown_exporter import MarkdownExporter  # noqa: E402
from backend.exporters.srt_exporter import SRTExporter  # noqa: E402
from backend.exporters.timeline_layout import timeline_layout  # noqa: E402

from scripts._config import MEDIA_DIR, SEARCH_DIRS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEARCH = SEARCH_DIRS
# 旁白覆蓋率：0.85 = 大部分有旁白的鏡頭把該場景的故事講清楚（用作者口氣），
# 留白留給純風景 / 蒙太奇 / 情緒定格。太低會變成只有串場、看不懂在講什麼。
COVERAGE = 0.85


def build_scenes(script: dict):
    layout = timeline_layout(script, SEARCH)
    marked = sum(1 for e in layout
                 if re.search(r"[【\[][^】\]]+[】\]]", e["shot"].get("scene_title", "") or ""))
    use_title = marked > len(layout) * 0.6 or not any(
        e["shot"].get("scene_id") is not None for e in layout)

    scenes: list[dict] = []
    for i, e in enumerate(layout, start=1):
        s = e["shot"]
        if use_title:
            key, title = _chapter_key(s.get("scene_title", "")), s.get("scene_title", "")
        else:
            key = s.get("scene_id", _chapter_key(s.get("scene_title", "")))
            title = s.get("scene_name") or s.get("scene_title", "")
        if not scenes or scenes[-1]["key"] != key:
            scenes.append({"key": key, "title": title, "entries": []})
        scenes[-1]["entries"].append((i, e))
    return layout, scenes


def dump(script: dict) -> None:
    _, scenes = build_scenes(script)
    payload = []
    for si, sc in enumerate(scenes, start=1):
        sec = sum(e["dur"] for _, e in sc["entries"])
        n = len(sc["entries"])
        payload.append({
            "scene": si,
            "chapter": sc["title"],
            "seconds": round(sec, 1),
            "char_budget": max(0, int(sec * _READING_CPS * COVERAGE)),
            "shots": [{
                "i": idx,
                "len": round(e["dur"], 1),
                "seam": ("章首·接住上一章" if k == 0 and si > 1 else
                         "章尾·留鉤子" if k == n - 1 and si < len(scenes) else ""),
                "visual": (e["shot"].get("visual_action")
                           or e["shot"].get("visual_description") or "")[:70],
                "orig_vo": (e["shot"].get("voiceover") or "")[:90],
            } for k, (idx, e) in enumerate(sc["entries"])],
        })
    print(json.dumps({
        "rules": [
            "畫面是主角，旁白填空隙。逐 scene，總字數 <= char_budget。",
            "純風景/情緒 scene 可整段不寫。寧可一半以上鏡頭留白。",
            "每句掛在某鏡頭 i，字數要能在該鏡頭 len 秒內以 6 字/秒念完。",
            "章尾·留鉤子：最後一句留懸念或留白；章首·接住上一章：第一句接住或翻轉。相鄰一放一收。",
            "保留原片精神：從 logline / theme / reflection 抓出片子真正在講的那件事，別漂走。",
            "主述不要吵：第一人稱，「我」能省就省。第一人稱複數（我們/兩人）當主語全片最多 1 次；"
            "「我」開頭的句子全片 <=5 句，只放情緒轉折。其餘零主語或把主語換成景物/動作/物件。",
            "地名/日期/第幾天不要用旁白念（交給畫面字卡）。連續兩句不要同一種開頭；"
            "可用第二人稱對畫面裡的人說、用側寫、用無主語名詞短句。",
        ],
        "scenes": payload,
    }, ensure_ascii=False, indent=1))


def apply(script: dict, vo_path: Path, prefix: str) -> None:
    layout, scenes = build_scenes(script)
    raw = {int(v["i"]): str(v.get("text", "")).strip()
           for v in json.loads(vo_path.read_text("utf-8")).get("vo", []) if v.get("text")}

    new_vo: dict[int, str] = {}
    trimmed = 0
    for sc in scenes:
        entries = sc["entries"]
        durs = [e["dur"] for _, e in entries]
        scene_sec = sum(durs)
        budget = int(scene_sec * _READING_CPS * COVERAGE)
        # 尊重作者/agent 在 vo.json 寫的「哪句掛哪顆鏡頭 i」（內容對得上畫面）。
        # 只做兩層裁切：① 這句 ~ 從該鏡頭起點到本章結尾的時間內念得完（不跨章、章尾自然壓短）
        #              ② 整章不超總預算。SRT 的 timeline 上限是最後防線。
        used = 0
        for k, (idx, _) in enumerate(entries):
            t = raw.get(idx, "")
            if not t:
                continue
            room = sum(durs[k:]) + 2.0
            cap = min(int(room * _READING_CPS * 1.1), budget - used)
            if _char_count(t) > cap:
                t = _trim_to_chars(t, max(0, cap))
                trimmed += 1
            if t:
                new_vo[idx] = t
                used += _char_count(t)

    n = 0
    for i, e in enumerate(layout, start=1):
        e["shot"]["voiceover"] = new_vo.get(i, "")
        e["shot"].pop("timed_subtitles", None)
        if new_vo.get(i):
            n += 1

    total = sum(_char_count(v) for v in new_vo.values())
    print(f"套用：{n}/{len(layout)} 顆鏡頭有旁白｜{len(scenes)} 章｜共 {total} 字｜{trimmed} 句被裁短")

    # 重新分時字幕 + 匯出
    for e in timeline_layout(script, SEARCH):
        vo = (e["shot"].get("voiceover") or "").strip()
        from backend.engines.subtitle_engine import SubtitleEngine
        if vo:
            e["shot"]["timed_subtitles"] = SubtitleEngine.generate_timed_subtitles(vo, e["dur"])

    fcpxml = FCPXMLExporter().export(script, search_dirs=SEARCH)
    srt = SRTExporter().export(script, search_dirs=SEARCH)
    md = MarkdownExporter().export(script)
    for base in (ROOT, MEDIA_DIR):
        (base / f"{prefix}.fcpxml").write_text(fcpxml, "utf-8")
        (base / f"{prefix}_字幕.srt").write_text(srt, "utf-8")
        (base / f"{prefix}_分鏡腳本.md").write_text(md, "utf-8")
        (base / f"{prefix}.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), "utf-8")
    print(f"✅ 已更新 {prefix}.fcpxml / _字幕.srt / _分鏡腳本.md / .json（專案 + Desktop）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--vo", help="vo.json 路徑")
    args = ap.parse_args()

    src = ROOT / f"{args.prefix}.json"
    if not src.exists():
        sys.exit(f"找不到 {src}")
    script = json.loads(src.read_text("utf-8"))

    if args.dump:
        dump(script)
    elif args.vo:
        apply(script, Path(args.vo), args.prefix)
    else:
        sys.exit("要 --dump 或 --vo <file>")


if __name__ == "__main__":
    main()
