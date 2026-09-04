#!/usr/bin/env python3
"""直接用 DaVinci Resolve 腳本 API 建時間軸——**繞過 FCPXML**。

為什麼：Resolve 匯入 FCPXML 會掉東西——
  - 下載來的 JPG 常變 Media Offline
  - `<adjust-transform>` 的關鍵影格運鏡（Ken Burns）不會讀進來
  - 影片入出點 / 晃動剪除的多段裁切不一定生效

這支腳本讓 Resolve 用「自己的匯入器」拉素材（哪個檔真的壞會明講），
再逐格 append clip（含 source_in/out 裁切），最後對每個 clip 套上
smart-crop 焦點的靜態推鏡框（Pan/Tilt/Zoom）。字幕仍走 File ▸ Import ▸ Subtitle。

用法（Resolve 要開著、開一個空專案）：
  export PYTHONPATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules:$PYTHONPATH"
  python3 scripts/resolve_build_timeline.py my_trip
  python3 scripts/resolve_build_timeline.py <prefix> --no-transform   # 只建時間軸不套運鏡
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402
from backend.util.media_probe import resolve_existing_path, is_video_path, probe_video

CODE_DIR = Path(__file__).resolve().parent.parent
FPS = 30


def get_resolve():
    # 1) 在 Resolve 內從 Workspace ▸ Scripts 執行時，resolve / bmd 會自動注入
    g = globals()
    if g.get("resolve") is not None:
        return g["resolve"]
    try:
        return bmd.scriptapp("Resolve")  # type: ignore  # noqa: F821
    except Exception:
        pass
    # 2) 外部執行：需要 External scripting = Local（Studio 版）
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except ImportError:
        for p in ("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
                  str(Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules")):
            if Path(p).is_dir():
                sys.path.append(p)
        try:
            import DaVinciResolveScript as dvr  # type: ignore
        except ImportError:
            sys.exit("找不到 DaVinciResolveScript 模組。")
    r = dvr.scriptapp("Resolve")
    if r is None:
        sys.exit("連不上 Resolve。從 Resolve 內 Workspace ▸ Scripts ▸ Edit 執行本腳本，"
                 "或（Studio 版）開啟 Preferences ▸ System ▸ General ▸ External scripting = Local。")
    return r


def load_storyboard(prefix: str):
    for c in (CODE_DIR / f"{prefix}.json", MEDIA_DIR / f"{prefix}.json"):
        if c.exists():
            return json.loads(c.read_text("utf-8"))
    sys.exit(f"找不到 {prefix}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", default=PREFIX)
    ap.add_argument("--no-transform", action="store_true")
    ap.add_argument("--timeline-name", default=None)
    args, _ = ap.parse_known_args()
    print(f"專案：{args.prefix}")

    script = load_storyboard(args.prefix)
    shots = [s for s in script.get("storyboard", []) if not s.get("skip")]
    ratio = script.get("target_aspect_ratio", "16:9")
    tw, th = {"9:16": (1080, 1920), "4:3": (1440, 1080), "1:1": (1080, 1080)}.get(ratio, (1920, 1080))

    resolve = get_resolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        sys.exit("Resolve 沒有開啟的專案。")
    for k, v in {"timelineResolutionWidth": str(tw), "timelineResolutionHeight": str(th),
                 "timelineFrameRate": str(FPS)}.items():
        project.SetSetting(k, v)

    media_pool = project.GetMediaPool()
    ms = resolve.GetMediaStorage()

    # 1) 收集要用到的實體檔，用 Resolve 自己的匯入器拉進 media pool
    paths, order = [], []
    for shot in shots:
        raw = shot.get("file_path") or shot.get("media_file") or ""
        p = resolve_existing_path(str(raw), [str(MEDIA_DIR)])
        order.append(str(p) if p else None)
        if p and str(p) not in paths:
            paths.append(str(p))

    print(f"匯入 {len(paths)} 個素材...")
    root = media_pool.GetRootFolder()
    media_pool.SetCurrentFolder(root)
    ms.AddItemListToMediaPool(paths)

    # path -> MediaPoolItem
    pool = {}
    def walk(folder):
        for it in folder.GetClipList() or []:
            fp = it.GetClipProperty("File Path")
            if fp:
                pool[fp] = it
        for sub in folder.GetSubFolderList() or []:
            walk(sub)
    walk(root)

    missing = [p for p in paths if p not in pool]
    if missing:
        print(f"⚠️ Resolve 匯不進來的檔（{len(missing)}）：")
        for m in missing[:20]:
            print("   ", m)

    # 2) 組 timeline clip list（含 source_in/out 裁切）
    clip_list, meta_for_item = [], []
    for shot, path in zip(shots, order):
        if not path or path not in pool:
            continue
        item = pool[path]
        is_vid = is_video_path(path)
        src_in = float(shot.get("source_in", 0.0) or 0.0)
        src_out = shot.get("source_out")
        if is_vid and src_out is not None:
            clip_sec = max(1.0 / FPS, float(src_out) - src_in)
        else:
            clip_sec = float(shot.get("duration_seconds", 4.0))
        if is_vid:
            info = probe_video(path)
            if info.get("duration_s"):
                clip_sec = min(clip_sec, max(1.0 / FPS, info["duration_s"] - src_in))
        start_f = int(round(src_in * FPS)) if is_vid else 0
        end_f = start_f + max(1, int(round(clip_sec * FPS)))
        clip_list.append({"mediaPoolItem": item, "startFrame": start_f,
                          "endFrame": end_f, "mediaType": 1})
        meta_for_item.append(shot)

    name = args.timeline_name or f"{args.prefix}_API"
    tl = media_pool.CreateEmptyTimeline(name)
    project.SetCurrentTimeline(tl)
    added = media_pool.AppendToTimeline(clip_list)
    print(f"✅ 時間軸「{name}」：{len(added or [])} 個 clip")

    # 3) 逐 clip 套 smart-crop 焦點的靜態推鏡
    if not args.no_transform and added:
        n = 0
        for ti, shot in zip(added, meta_for_item):
            kb = shot.get("ken_burns")
            if not isinstance(kb, dict) or not kb.get("end"):
                continue
            e = kb["end"]
            try:
                ti.SetProperty("ZoomX", float(e["scale"]))
                ti.SetProperty("ZoomY", float(e["scale"]))
                ti.SetProperty("Pan", float(e.get("x", 0.0)) * tw)
                ti.SetProperty("Tilt", float(e.get("y", 0.0)) * th)
                if kb.get("type") in ("zoom", "pan"):
                    ti.SetProperty("DynamicZoomEase", 0)   # 開 Dynamic Zoom（在檢視器可再拉起訖框）
                n += 1
            except Exception:
                pass
        print(f"   運鏡框套用 {n} 個 clip（靜態；動態 Ken Burns 請在檢視器用 Dynamic Zoom 調起訖）")

    print("\n字幕：File ▸ Import ▸ Subtitle 選 " + f"{args.prefix}_字幕.srt")
    print("調色：python3 scripts/resolve_auto_grade.py --hints " + f"{args.prefix}_grade_hints.json")


if __name__ == "__main__":
    main()
