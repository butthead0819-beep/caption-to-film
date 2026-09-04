#!/usr/bin/env python3
"""階段 5：產生「給人類過的一版」— 腳本 + 關鍵影像審閱包（單一 HTML）。

在剪 timeline 前先讓人類看完整分鏡 + 每章關鍵影像 + 口白全文，直接在紙上／螢幕上批註，
批註完回饋迴圈 parse 成 storyboard patch（見 pipeline-and-tools.md）。

產出 <prefix>_審閱包.html：自帶 base64 縮圖，離線可開、可列印成 PDF。
內容：
  - 企劃摘要（logline / 主題 / 預估片長 / 鏡頭數）
  - 口白預算檢查（總字數 vs 片長；紀錄片旁白總字數 ≈ 片長秒數的一半）
  - 依章節（scene_title 的【】）分組
  - 每章「關鍵影像」：highlight_score 最高的 N 張放大
  - 全片分鏡表：縮圖 / 鏡頭號 / 檔名 / 景別 / 秒數 / 口白
  - 批註格式提示

用法：
  .venv/bin/python scripts/build_review_packet.py 2026_單車環島_父子冒險紀錄片
  .venv/bin/python scripts/build_review_packet.py --json path/to.json --key-images 4
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.engines.subtitle_engine import _char_count  # noqa: E402
from backend.exporters.timeline_layout import timeline_layout  # noqa: E402
from backend.util.media_probe import is_video_path, resolve_existing_path  # noqa: E402

from scripts._config import SEARCH_DIRS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_VO_DROP = ("", "null", "無", "(純畫面與環境音)")


def chapter_of(shot: dict) -> str:
    # Gemini 有分【章節】就用它（敘事片），否則用 segment_scenes 寫的 scene_name
    t = shot.get("scene_title", "") or ""
    m = re.search(r"[【\[]([^】\]]+)[】\]]", t)
    if m:
        return m.group(1).strip()
    if shot.get("scene_name"):
        return str(shot["scene_name"])
    return t.split("：")[0].strip() or "未分章"


def thumb_data_uri(shot: dict, width: int = 360) -> str | None:
    raw = shot.get("file_path") or shot.get("media_file") or ""
    p = resolve_existing_path(str(raw), SEARCH_DIRS)
    if not p:
        return None
    ss = float(shot.get("source_in", 0.0) or 0.0)
    if is_video_path(p):
        ss = ss + 0.3
    try:
        jpg = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{ss:.3f}",
             "-i", str(p), "-frames:v", "1", "-vf", f"scale={width}:-2",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=40).stdout
    except Exception:
        return None
    if not jpg:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode()


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", help="專案 prefix（讀 <prefix>.json）")
    ap.add_argument("--json", help="直接指定 storyboard json")
    ap.add_argument("--out", help="輸出 html 路徑")
    ap.add_argument("--key-images", type=int, default=3, help="每章關鍵影像張數")
    ap.add_argument("--photos-meta", default=str(ROOT / "scripts" / "photos_meta.json"))
    ap.add_argument("--no-score", action="store_true",
                    help="不重算 highlight_score（用 json 內現有的 / favorite）")
    args = ap.parse_args()

    src = Path(args.json) if args.json else ROOT / f"{args.prefix}.json"
    if not src.exists():
        sys.exit(f"找不到 {src}")
    data = json.loads(src.read_text("utf-8"))
    storyboard = [s for s in data.get("storyboard", []) if not s.get("skip")]

    if not args.no_score:
        try:
            from backend.engines.highlight_engine import score_storyboard
            score_storyboard(storyboard, search_dirs=SEARCH_DIRS,
                             photos_meta_path=args.photos_meta)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  highlight_score 重算失敗，改用現有值：{e}")

    layout = {id(l["shot"]): l for l in timeline_layout(data, SEARCH_DIRS)}
    timeline_sec = sum(l["dur"] for l in layout.values())

    # 口白預算
    vo_all = [s.get("voiceover", "") for s in storyboard
             if str(s.get("voiceover", "")).strip() not in _VO_DROP]
    vo_chars = sum(_char_count(v) for v in vo_all)
    # 舒適旁白 ≈ 覆蓋約一半片長、閱讀速度 6 字/秒（另一半留給環境音呼吸）
    vo_target = timeline_sec * 6.0 * 0.5
    vo_ratio = vo_chars / vo_target if vo_target else 0

    # 章節分組（保序）
    chapters: list[tuple[str, list[dict]]] = []
    for s in storyboard:
        c = chapter_of(s)
        if not chapters or chapters[-1][0] != c:
            chapters.append((c, []))
        chapters[-1][1].append(s)

    print(f"產生審閱包：{len(storyboard)} 鏡頭 / {len(chapters)} 章 / 片長 {timeline_sec:.0f}s")
    print("抽縮圖中…")
    thumbs: dict[int, str | None] = {}
    for i, s in enumerate(storyboard, 1):
        thumbs[id(s)] = thumb_data_uri(s)
        if i % 20 == 0:
            print(f"   {i}/{len(storyboard)}")

    # ---- HTML ----
    P: list[str] = []
    title = data.get("project_title", src.stem)
    P.append(f"""<!doctype html><meta charset=utf-8><title>{esc(title)}／審閱包</title>
<style>
 body{{font:15px/1.7 -apple-system,"PingFang TC",sans-serif;max-width:1000px;margin:0 auto;padding:24px;color:#1a1a1a}}
 h1{{margin:.2em 0}} h2{{margin-top:2em;border-bottom:2px solid #333;padding-bottom:.2em}}
 .meta{{color:#555}} .box{{background:#f5f5f4;border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:14px 0}}
 .warn{{background:#fff4e5;border-color:#f0c078}}
 .keyimgs{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}}
 .keyimgs img{{width:300px;height:auto;border-radius:6px;border:1px solid #ccc}}
 table{{border-collapse:collapse;width:100%;margin:10px 0}}
 td,th{{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}}
 td.n{{white-space:nowrap;font-weight:bold;color:#666}} td.t img{{width:150px;height:auto;display:block}}
 .vo{{font-weight:600}} .novo{{color:#999}} .sec{{white-space:nowrap;color:#555}}
 @media print{{ .keyimgs img{{width:230px}} td.t img{{width:110px}} }}
</style>
<h1>{esc(title)}</h1>
<p class=meta>{esc(data.get('subtitle',''))}</p>
<div class=box>
 <b>Logline</b>：{esc(data.get('narrative_logline',''))}<br>
 <b>主題弧線</b>：{esc(data.get('theme_summary',''))}<br>
 <b>預估片長</b>：{timeline_sec:.0f}s（{int(timeline_sec//60)}分{int(timeline_sec%60)}秒）
 ｜<b>鏡頭</b>：{len(storyboard)} ｜<b>章節</b>：{len(chapters)} ｜<b>比例</b>：{esc(data.get('target_aspect_ratio','16:9'))}
</div>
<div class="box {'warn' if not 0.7 <= vo_ratio <= 1.3 else ''}">
 <b>口白預算</b>：目前 {vo_chars} 字，建議約 {vo_target:.0f} 字（旁白覆蓋約半部片長、6 字/秒）
 → 比例 <b>{vo_ratio:.0%}</b>{'　⚠️ 偏離太多，考慮增刪旁白或調整片長' if not 0.7 <= vo_ratio <= 1.3 else '　✓ 合理'}
</div>
<div class=box>
 <b>批註方式</b>：另存一個純文字檔 <code>notes.txt</code>，一行一條「鏡頭號 動作」，然後
 <code>python scripts/apply_notes.py notes.txt --write</code>：<br>
 <code>19 刪</code>　<code>19 換序→3</code>　<code>19 改口白：新的句子…</code>　<code>19 留白</code>　<code>19 換素材：IMG_2601.JPG</code>　<code>19 加長到 4s</code>
</div>
""")

    for ci, (cname, shots) in enumerate(chapters, 1):
        c_sec = sum(layout.get(id(s), {}).get("dur", s.get("duration_seconds", 0)) for s in shots)
        P.append(f"<h2>第 {ci} 章　{esc(cname)}　<span class=meta>({len(shots)} 鏡 / {c_sec:.0f}s)</span></h2>")

        ranked = sorted(shots, key=lambda s: s.get("highlight_score", 0) or s.get("favorite", 0),
                        reverse=True)[:args.key_images]
        P.append("<div class=keyimgs>")
        for s in ranked:
            u = thumbs.get(id(s))
            if u:
                sc = s.get("highlight_score")
                cap = f"#{s.get('shot_index','?')}" + (f"　score {sc}" if sc is not None else "")
                P.append(f"<figure style=margin:0><img src='{u}'><figcaption class=meta>{esc(cap)}</figcaption></figure>")
        P.append("</div>")

        P.append("<table><tr><th>#</th><th>縮圖</th><th>素材 / 景別 / 秒</th><th>口白</th></tr>")
        for s in shots:
            u = thumbs.get(id(s))
            sec = layout.get(id(s), {}).get("dur", s.get("duration_seconds", 0))
            vo = str(s.get("voiceover", "")).strip()
            vo_html = (f"<span class=vo>{esc(vo)}</span>" if vo not in _VO_DROP
                       else "<span class=novo>［留白／環境音］</span>")
            media = esc(s.get("media_file", ""))
            live = "　📸Live" if s.get("is_live_photo") else ("　🎥" if s.get("media_type") == "video" else "")
            P.append(
                f"<tr><td class=n>#{esc(s.get('shot_index','?'))}</td>"
                f"<td class=t>{'<img src=' + chr(39) + u + chr(39) + '>' if u else '—'}</td>"
                f"<td><code>{media}</code>{live}<br><span class=sec>{esc(s.get('shot_type',''))}　{sec:.1f}s</span>"
                f"<br><span class=meta>{esc(s.get('visual_description',''))}</span></td>"
                f"<td>{vo_html}</td></tr>"
            )
        P.append("</table>")

    out = Path(args.out) if args.out else src.with_name(src.stem + "_審閱包.html")
    out.write_text("\n".join(P), encoding="utf-8")
    print(f"\n✅ {out}\n   在瀏覽器開 → 檔案 ▸ 列印 ▸ 存成 PDF 給人看")


if __name__ == "__main__":
    main()
