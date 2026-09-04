#!/usr/bin/env python3
"""從 storyboard JSON 重新產生 FCPXML / SRT / Markdown（讀 edit_project.json 的 prefix）。

修正項目：
  1. FCPXML media-rep / format / 路徑重新連結 → 解決 Media Offline。
  2. 用新版 SubtitleEngine 依閱讀速度重切分時字幕 (字大、切得好、秒數合理)。
  3. 可選：--stabilize 自動偵測並剪除大幅晃動的影片片段。

用法:
  .venv/bin/python scripts/rebuild_all_projects.py [--stabilize] [--shake-threshold 3.2]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.engines.subtitle_engine import SubtitleEngine
from backend.exporters.fcpxml_exporter import FCPXMLExporter
from backend.exporters.srt_exporter import SRTExporter
from backend.exporters.json_exporter import JSONExporter
from backend.exporters.markdown_exporter import MarkdownExporter
from backend.util.media_probe import resolve_existing_path

from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent.parent

_DERIVED_KEYS = ("ken_burns", "source_in", "source_out", "shake_cut_note", "skip",
                 "role", "highlight_score", "keep", "keep_reason", "_hl")


def _strip_tags(s: str) -> str:
    for t in (" [Live 微動態]", " [定格]", "[Live 微動態]", "[定格]"):
        s = (s or "").replace(t, "")
    import re
    return re.sub(r"\s*\[穩定段 \d+/\d+\]", "", s).strip()


def normalize_storyboard(sb):
    """把先前 rebuild 加工過的 storyboard 還原成乾淨基準 (讓 rebuild 可重複執行)。

    - 合併 Live Photo 的「微動態 + 定格」成對鏡頭
    - 合併晃動剪除切出的「[穩定段 j/N]」連續片段
    - 移除衍生欄位 (ken_burns / source_in/out / skip ...)
    """
    out = []
    i = 0
    while i < len(sb):
        cur = sb[i]
        va = cur.get("visual_action", "") or ""

        # Live Photo 微動態 + 定格
        if "[Live 微動態]" in va and i + 1 < len(sb) and "[定格]" in (sb[i + 1].get("visual_action") or ""):
            freeze = sb[i + 1]
            merged = {k: v for k, v in freeze.items() if k not in _DERIVED_KEYS}
            merged["is_live_photo"] = True
            merged["media_type"] = "live_photo"
            merged["duration_seconds"] = round(
                float(cur.get("duration_seconds", 0)) + float(freeze.get("duration_seconds", 0)), 2)
            merged["visual_action"] = _strip_tags(freeze.get("visual_action", ""))
            merged["voiceover"] = freeze.get("voiceover") or cur.get("voiceover") or ""
            merged.pop("timed_subtitles", None)
            out.append(merged)
            i += 2
            continue

        # 晃動剪除切出的穩定片段：同一來源、連續
        if "[穩定段" in va:
            base = _strip_tags(va)
            run = []
            j = i
            while j < len(sb) and "[穩定段" in (sb[j].get("visual_action") or "") \
                    and _strip_tags(sb[j].get("visual_action") or "") == base \
                    and sb[j].get("media_file") == cur.get("media_file"):
                run.append(sb[j])
                j += 1
            merged = {k: v for k, v in run[0].items() if k not in _DERIVED_KEYS}
            merged["duration_seconds"] = round(sum(float(r.get("duration_seconds", 0)) for r in run), 2)
            merged["visual_action"] = base
            merged["voiceover"] = "".join(r.get("voiceover") or "" for r in run)
            merged.pop("timed_subtitles", None)
            out.append(merged)
            i = j
            continue

        out.append({k: v for k, v in cur.items() if k not in _DERIVED_KEYS})
        i += 1
    return out

PROJECTS = [PREFIX]   # 換影片改 edit_project.json / EDIT_PREFIX（見 scripts/_config.py）


def load_script(prefix: str):
    for cand in (CODE_DIR / f"{prefix}.json", MEDIA_DIR / f"{prefix}.json",
                 MEDIA_DIR / f"{prefix}_json.json"):
        if cand.exists():
            with open(cand, encoding="utf-8") as f:
                return json.load(f)
    return None


def rebuild(prefix: str, stabilize: bool, shake_threshold: float, effects: bool = True,
            select: str = "off", select_count=None, select_shake: bool = False,
            abroll: bool = False, keep_camera=None, chrono: bool = False,
            regen_vo: bool = False, segment: bool = False,
            stabilize_clips: bool = False, shaky_clip: float = 0.30):
    script = load_script(prefix)
    if not script:
        print(f"⚠️  找不到 {prefix}.json，略過")
        return
    script["storyboard"] = normalize_storyboard(script.get("storyboard", []))
    storyboard = script["storyboard"]
    search_dirs = [str(MEDIA_DIR)]
    select_summary = []
    role_counts = None
    cam_summary = None
    regen_summary = None

    # 0.9) 畫面穩定規則 (可選)：晃動比例 >= 門檻的影片 → vid.stab 穩定版 (冪等，已有就跳過)
    if stabilize_clips:
        from scripts.stabilize_clips import ensure_stabilized
        vids = []
        for shot in storyboard:
            f = resolve_existing_path(str(shot.get("file_path") or shot.get("media_file") or ""),
                                      [str(MEDIA_DIR)])
            if f and f.suffix.lower() in (".mp4", ".mov", ".m4v"):
                vids.append(f)
        made = ensure_stabilized(vids, threshold=shaky_clip)
        if made:
            print(f"   🎬 畫面穩定：{len(made)} 個影片有穩定版 → _stabilized/")

    # 1) 路徑重新連結
    from backend.util.media_prep import prepare_still
    prep_dir = str(MEDIA_DIR / "_prepared")
    stab_dir = MEDIA_DIR / "_stabilized"   # stabilize_clips.py 產的畫面穩定版
    _JUNK = ("字幕", "分鏡腳本", "測試", "subtitle", "storyboard")
    relinked = missing = dropped = prepped = stabilized = 0
    for shot in storyboard:
        raw = shot.get("file_path") or shot.get("media_file") or ""
        found = resolve_existing_path(str(raw), search_dirs)
        if found:
            if str(found) != str(raw):
                relinked += 1
            stab = stab_dir / found.name        # 有穩定版就優先用
            if stab.exists():
                found = stab
                stabilized += 1
            fixed = prepare_still(str(found), prep_dir)   # 奇數尺寸圖 → 偶數乾淨副本 (Resolve 才吃)
            if fixed != str(found):
                prepped += 1
                found = Path(fixed)
            shot["file_path"] = str(found)
            shot["media_file"] = found.name
        elif any(k in str(raw) for k in _JUNK):
            # 舊版建構腳本誤把專案輸出檔 (.srt/.md/.fcpxml) 當成素材
            shot["skip"] = True
            dropped += 1
        else:
            missing += 1

    # 1.2) 相機篩選 + 依拍攝時間重排 (可選)
    if keep_camera or chrono:
        from backend.util.photos_meta import load_photos_meta, meta_for
        pm = load_photos_meta()
        kept, dropped_cam, dropped_notime = [], 0, 0
        for s in storyboard:
            rec = meta_for(s, pm)
            cam = (rec.get("camera") or "").lower()
            if keep_camera and not any(kc.lower() in cam for kc in keep_camera):
                dropped_cam += 1
                continue
            if chrono and not rec.get("taken"):
                dropped_notime += 1
                continue
            s["_taken"] = rec.get("taken") or ""
            kept.append(s)
        if chrono:
            kept.sort(key=lambda s: s.get("_taken") or "9999")
        for s in kept:
            s.pop("_taken", None)
        cam_summary = (f"相機/時間篩選：保留 {len(kept)}/{len(storyboard)}"
                       + (f"｜非指定相機剔除 {dropped_cam}" if dropped_cam else "")
                       + (f"｜無拍攝時間剔除 {dropped_notime}" if dropped_notime else "")
                       + ("｜已依拍攝時間重排" if chrono else ""))
        storyboard = kept
        script["storyboard"] = storyboard

    # 1.3) 精華篩選 (可選) — 剔除的 shot 設 skip=True
    if select != "off":
        from backend.engines.highlight_engine import select_highlights
        select_summary = select_highlights(
            storyboard, mode=select, target_count=select_count,
            search_dirs=search_dirs, analyze_shake=select_shake,
            shake_threshold=shake_threshold,
        )
        storyboard = [s for s in storyboard if not s.get("skip")]
        script["storyboard"] = storyboard

    # 1.5) Live Photo：微動態 → 定格
    from backend.engines.livephoto_engine import expand_live_photos
    live_changes = expand_live_photos(storyboard, search_dirs=search_dirs)

    # Live Photo 在這一步才給 A/B 的 file_path → 再補一次 relink：
    #  A 段 MOV → _stabilized；B 段定格 HEIC → _prepared 的 JPG
    for shot in storyboard:
        fp = Path(str(shot.get("file_path") or ""))
        if fp.suffix.lower() in (".mp4", ".mov", ".m4v"):
            st = stab_dir / fp.name
            if st.exists() and str(fp) != str(st):
                shot["file_path"] = str(st)
                shot["media_file"] = st.name
                stabilized += 1
        elif fp.suffix.lower() in (".heic", ".heif", ".png", ".webp", ".tif", ".tiff"):
            fixed = prepare_still(str(fp), prep_dir)
            if fixed != str(fp):
                shot["file_path"] = fixed
                shot["media_file"] = Path(fixed).name
                prepped += 1

    # 2) 晃動剪除 (可選)
    shake_changes = []
    if stabilize:
        from backend.analyzers.motion_stability import apply_to_storyboard
        shake_changes = apply_to_storyboard(
            storyboard, search_dirs=search_dirs, shake_threshold=shake_threshold
        )

    # 2.5) 自動 Ken Burns + 填滿目標比例
    fx_changes = []
    if effects:
        from backend.engines.effects_engine import apply_effects
        fx_changes = apply_effects(
            storyboard, search_dirs=search_dirs,
            target_ratio=script.get("target_aspect_ratio", "16:9"),
        )

    from backend.exporters.timeline_layout import timeline_layout

    # 2.7) 小章節切分 (可選) — 寫 scene_id / scene_name，供旁白預算與審閱包分組
    if segment or regen_vo:
        try:
            from scripts.segment_scenes import segment as _seg
            from backend.util.photos_meta import load_photos_meta
            meta = load_photos_meta()
            montage = sum(1 for s in storyboard
                          if "蒙太奇" in (s.get("scene_title") or "")) > len(storyboard) * 0.5
            scs = _seg(storyboard, meta, day_gap_h=None, by_gps=False, move_km=3.0,
                       by_label=False, montage=montage, min_scene_sec=12.0, max_scene_sec=60.0)
            for sc in scs:
                for s in sc["shots"]:
                    s["scene_id"] = sc["scene_id"]
                    s["scene_name"] = sc["name"]
            print(f"   🗂️  小章節：{len(scs)} 章"
                  + ("（蒙太奇整支一章）" if montage else ""))
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️  章節切分略過：{e}")

    # 2.8) 旁白重生 (可選) — 配合重新剪過的時間軸整條重寫
    if regen_vo:
        from backend.engines.script_engine import ScriptEngine
        lay = timeline_layout(script, search_dirs)
        total = (lay[-1]["start"] + lay[-1]["dur"]) if lay else 0.0
        n = ScriptEngine().regenerate_voiceover(
            lay, script, total, story_context=script.get("theme_summary"))
        regen_summary = (f"旁白重生：{n} 顆鏡頭有台詞 / 共 {len(lay)}"
                         if n >= 0 else "旁白重生失敗（Gemini 不可用或額度用完），保留原旁白")

    # 3) 重切分時字幕（用成片時間軸的秒數，不是原始 duration_seconds）
    for e in timeline_layout(script, search_dirs):
        shot, dur = e["shot"], e["dur"]
        vo = (shot.get("voiceover") or "").strip()
        if vo:
            shot["timed_subtitles"] = SubtitleEngine.generate_timed_subtitles(vo, dur)
        else:
            shot.pop("timed_subtitles", None)

    # 3.5) A/B-roll 角色分類 (可選)
    if abroll:
        from backend.engines.abroll_engine import classify_roles
        role_counts = classify_roles(storyboard)

    # 4) 匯出
    fcpxml = FCPXMLExporter().export(script, search_dirs=search_dirs, abroll=abroll)
    srt = SRTExporter().export(script, search_dirs=search_dirs)
    md = MarkdownExporter().export(script)

    # --select / --abroll / --chrono 是「衍生剪法」：輸出到帶後綴的檔名，絕不覆寫正本 .json
    # （正本 storyboard 是唯一真相來源，被砍掉的鏡頭無法還原）。
    # --regen-vo 只重寫旁白、不動鏡頭 → 單獨用時當正式重建（寫回正本 .json）；
    # 跟衍生旗標一起用時才加 _vo 後綴。
    variant = ""
    if keep_camera or chrono:
        variant += "_chrono"
    if select != "off":
        variant += f"_{select}"
    if abroll:
        variant += "_abroll"
    if regen_vo and variant:
        variant += "_vo"

    from backend.engines.grading_engine import write_grade_hints
    from backend.util.photos_meta import load_photos_meta
    n_hints = 0

    for base in (CODE_DIR, MEDIA_DIR):
        (base / f"{prefix}{variant}.fcpxml").write_text(fcpxml, encoding="utf-8")
        (base / f"{prefix}{variant}_字幕.srt").write_text(srt, encoding="utf-8")
        (base / f"{prefix}{variant}_分鏡腳本.md").write_text(md, encoding="utf-8")
        if not variant:
            (base / f"{prefix}.json").write_text(JSONExporter().export(script), encoding="utf-8")
        if load_photos_meta():   # 有 photos_meta 才寫場景調色建議 sidecar
            n_hints = write_grade_hints(storyboard, str(base / f"{prefix}{variant}_grade_hints.json"))

    n_subs = srt.count(" --> ")   # 實際寫進 SRT 的則數（已過 timeline 上限與去重）
    print(f"✅ {prefix}")
    print(f"   鏡頭 {len(storyboard)}｜重新連結 {relinked}｜奇數尺寸修正 {prepped}｜畫面穩定版 {stabilized}｜剔除無效項 {dropped}｜仍找不到 {missing}｜字幕 {n_subs} 則")
    if live_changes:
        print(f"   📷 Live Photo 微動態→定格 {len(live_changes)} 個 (示例: {live_changes[0]})")
    for c in shake_changes:
        print(f"   ✂️  {c}")
    if fx_changes:
        print(f"   🎥 自動運鏡 {len(fx_changes)} 個鏡頭 (示例: {fx_changes[0]})")
    if cam_summary:
        print(f"   🎞️  {cam_summary}")
    if regen_summary:
        print(f"   🗣️  {regen_summary}")
    for line in select_summary:
        print(f"   🌟 {line}")
    if role_counts:
        print(f"   🎞️  A/B-roll：A-roll {role_counts['a-roll']}｜B-roll {role_counts['b-roll']}")
    if n_hints:
        print(f"   🎨 場景調色建議 {n_hints} 個素材 → {prefix}{variant}_grade_hints.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stabilize", action="store_true", help="自動剪除大幅晃動片段（裁掉晃動段）")
    ap.add_argument("--shake-threshold", type=float, default=3.2)
    ap.add_argument("--stabilize-clips", action="store_true",
                    help="畫面穩定規則：晃動比例 >= --shaky-clip 的影片跑 vid.stab 穩定版（不裁剪、冪等）")
    ap.add_argument("--shaky-clip", type=float, default=0.30,
                    help="--stabilize-clips 的晃動比例門檻（預設 0.30）")
    ap.add_argument("--no-effects", action="store_true", help="不要自動加 Ken Burns / 填滿比例")
    ap.add_argument("--only", default=None, help="只重建指定專案 prefix")
    ap.add_argument("--select", choices=["off", "narrative", "highlight", "montage"],
                    default="off", help="精華篩選模式 (預設 off = 全收)")
    ap.add_argument("--select-count", type=int, default=None,
                    help="highlight 模式的目標鏡頭數 (預設 ~總數/3)")
    ap.add_argument("--select-shake", action="store_true",
                    help="精華篩選時分析影片晃動當扣分 (較慢，跑 ffmpeg)")
    ap.add_argument("--abroll", action="store_true",
                    help="A/B-roll：A-roll 排 spine，旁白前的 B-roll 疊 lane-1 靜音")
    ap.add_argument("--fcp-keyframes", action="store_true",
                    help="運鏡寫成關鍵影格動畫（給 Final Cut；Resolve 不讀，預設寫靜態終點框）")
    ap.add_argument("--fcp-titles", action="store_true",
                    help="FCPXML 內嵌 <title> 字幕（給 Final Cut；Resolve 會置中壓畫面，預設不寫、改走 SRT）")
    ap.add_argument("--keep-camera", action="append", default=None, metavar="MODEL",
                    help="只保留這個相機型號的素材（可重複，如 --keep-camera 'iPhone Air'）；需 photos_meta")
    ap.add_argument("--chrono", action="store_true",
                    help="依拍攝時間重排 storyboard；沒有拍攝時間的素材會被剔除；需 photos_meta")
    ap.add_argument("--regen-vo", action="store_true",
                    help="配合重新剪過的時間軸，用 Gemini 整條重寫旁白（需 GEMINI_API_KEY）")
    ap.add_argument("--segment", action="store_true",
                    help="切小章節寫 scene_id/scene_name（--regen-vo 會自動做）；供旁白預算/審閱包分組")
    args = ap.parse_args()
    if args.fcp_keyframes:
        FCPXMLExporter.KEYFRAMES = True
    if args.fcp_titles:
        FCPXMLExporter.EMIT_TITLES = True

    targets = [args.only] if args.only else PROJECTS
    for prefix in targets:
        rebuild(prefix, args.stabilize, args.shake_threshold, effects=not args.no_effects,
                select=args.select, select_count=args.select_count,
                select_shake=args.select_shake, abroll=args.abroll,
                keep_camera=args.keep_camera, chrono=args.chrono,
                regen_vo=args.regen_vo, segment=args.segment,
                stabilize_clips=args.stabilize_clips, shaky_clip=args.shaky_clip)


if __name__ == "__main__":
    main()
