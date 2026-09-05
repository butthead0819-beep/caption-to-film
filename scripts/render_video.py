#!/usr/bin/env python3
"""階段 8「自己出片」：不經過 DaVinci Resolve，直接用 ffmpeg 把 storyboard 渲成 mp4。

用 pipeline 已經算好的東西：`timeline_layout` 每鏡起訖、`ken_burns` 縮放/平移、
`_stabilized/` 穩定版、`grade_hints.json` 的 ASC-CDL、`<prefix>_字幕.srt`。

流程：逐鏡渲成規格一致的片段（1920x1080 / 30fps / yuv420p / 48k stereo）→ concat
→ 燒字幕 → x264 輸出。

v1 涵蓋：切點、來源 in/out、Ken Burns（照片 zoompan、影片靜態填滿）、片段原音、燒中文字幕、
        可選 CDL 調色（--grade，用 grade_hints 生 .cube LUT）。
v1 不含：音樂 bed、TTS 旁白語音（旁白目前只有燒進畫面的字幕）、轉場特效。

用法：
  .venv/bin/python scripts/render_video.py                       # 全片 → <prefix>.mp4
  .venv/bin/python scripts/render_video.py --grade --fast        # 720p 快版（給 self-eval）
  .venv/bin/python scripts/render_video.py --start 120 --duration 40 -o slice.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import PREFIX, SEARCH_DIRS  # noqa: E402
from backend.exporters.timeline_layout import timeline_layout  # noqa: E402
from backend.util.media_probe import is_video_path, resolve_existing_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FPS = 30


def _sh(cmd: list, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cdl_to_cube(cdl: dict, path: Path, size: int = 17) -> None:
    """ASC-CDL (slope/offset/power + 飽和) → .cube 3D LUT。"""
    import numpy as np

    sl = np.array(cdl.get("slope_mul", [1, 1, 1]), float)
    of = np.array(cdl.get("offset_add", [0, 0, 0]), float)
    pw = np.array(cdl.get("power", [1, 1, 1]), float)
    sat = float(cdl.get("sat_mul", 1.0))
    g = np.linspace(0, 1, size)
    b, gr, r = np.meshgrid(g, g, g, indexing="ij")
    rgb = np.stack([r, gr, b], -1)                       # (s,s,s,3)
    out = np.clip(rgb * sl + of, 0, 1) ** pw
    luma = out @ np.array([0.2126, 0.7152, 0.0722])
    out = np.clip(luma[..., None] + (out - luma[..., None]) * sat, 0, 1)
    lines = ["LUT_3D_SIZE %d" % size]
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                c = out[bi, gi, ri]
                lines.append(f"{c[0]:.5f} {c[1]:.5f} {c[2]:.5f}")
    path.write_text("\n".join(lines), "utf-8")


def _ease(n: int) -> str:
    """smoothstep 緩入緩出：把 on/n 線性進度換成 3p²-2p³（頭尾不會突然啟停）。"""
    p = f"(on/{n})"
    return f"(3*{p}*{p}-2*{p}*{p}*{p})"


_ASPECT_CACHE: dict[str, float] = {}


def _ffprobe_display_size(src: str) -> tuple[int, int] | None:
    """影片解碼後真正的顯示尺寸 —— ffmpeg 會自動套 frame cropping（clean aperture）
    與 display matrix（rotation）；iPhone Live Photo 直式 MOV 的 coded 尺寸是橫的
    （1920x1440），實際顯示是直的（~1308x1744）。stream w/h 兩者都沒套，要自己算。"""
    r = _sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:stream_side_data_list",
             "-of", "json", str(src)])
    try:
        st = json.loads(r.stdout)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
        rot = 0
        for sd in st.get("side_data_list", []):
            if sd.get("side_data_type") == "Frame Cropping":
                w -= int(sd.get("crop_left", 0)) + int(sd.get("crop_right", 0))
                h -= int(sd.get("crop_top", 0)) + int(sd.get("crop_bottom", 0))
            if "rotation" in sd:
                try:
                    rot = abs(int(sd["rotation"])) % 180
                except Exception:
                    pass
        if rot == 90:
            w, h = h, w
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _aspect(src: str) -> float | None:
    key = str(src)
    if key in _ASPECT_CACHE:
        return _ASPECT_CACHE[key]
    val = None
    try:
        if is_video_path(src):
            wh = _ffprobe_display_size(src)
            if wh:
                val = wh[0] / wh[1]
        if val is None:
            from backend.util.media_probe import probe_image, probe_video
            info = (probe_video if is_video_path(src) else probe_image)(str(src))
            w, h = info.get("width"), info.get("height")
            if w and h:
                val = w / h
    except Exception:
        pass
    if val:
        _ASPECT_CACHE[key] = val
    return val


def framing_vf(kb: dict | None, dur: float, W: int, H: int, src: str, is_img: bool,
               focus: tuple[float, float] | None = None) -> str:
    """把來源放進 WxH：比例接近就 cover 填滿 + 乾淨 Ken Burns；
    直式/比例落差大的（裁掉 >33%）改『模糊填滿』—— 照片完整置中，兩側用自己放大模糊當底，
    不再把直式照裁成一條中間。

    Ken Burns 不用 effects_engine 的 FCP 座標，改標準做法：cover 後在小範圍慢推，
    緩入緩出（_ease），照片用 3x supersample 讓 zoompan 不頓。
    """
    n = max(2, round(dur * FPS))
    ar = W / H
    a = _aspect(src) or ar
    crop_frac = (1.0 - ar / a) if a >= ar else (1.0 - a / ar)   # cover 會裁掉的比例
    kind = (kb or {}).get("type", "static")
    # 對焦點（cx, cy 正規化）：有顯著性/人臉焦點就用
    fcx, fcy = (float(focus[0]), float(focus[1])) if focus else (0.5, 0.5)
    # 照片/影片裁掉上下時：人像 vlog 幾乎都把頭放上半 → 裁切框往上壓，垂直焦點夾在 0.30~0.44，
    # 寧可切腳不要切頭
    if a < ar:
        fcy = min(max(fcy, 0.30), 0.44)
    fcx = min(max(fcx, 0.30), 0.70)
    fx = min(0.72, max(0.28, fcx)); fy = min(0.72, max(0.28, fcy))
    # cover 裁切位置（讓裁切框中心貼近對焦點）：expr 給 ffmpeg crop 的 x/y
    _cxp = f"clip(in_w*{fcx:.3f}-out_w/2,0,in_w-out_w)"
    _cyp = f"clip(in_h*{fcy:.3f}-out_h/2,0,in_h-out_h)"

    # ── 模糊填滿：直式照片 / 9:16 / 比例落差大 ──────────────────────────
    if crop_frac > 0.33:
        SS = 2
        Wl, Hl = W * SS, H * SS
        blur = round(26 * SS)
        do_push = is_img and dur >= 1.2 and kind not in (None, "none", "static")
        g = (
            f"split=2[bg][fg];"
            f"[bg]scale={Wl}:{Hl}:force_original_aspect_ratio=increase,crop={Wl}:{Hl},"
            f"gblur=sigma={blur},eq=brightness=-0.10:saturation=0.82,setsar=1[bgb];"
            f"[fg]scale={Wl}:{Hl}:force_original_aspect_ratio=decrease,setsar=1[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={FPS}[cmp];"
        )
        if not do_push:
            return g + f"[cmp]scale={W}:{H},setsar=1"
        z0, z1 = 1.0, 1.045                      # 底+前景一起推，幅度要很小
        return (g + f"[cmp]zoompan=z='{z0}+{z1 - z0:.3f}*{_ease(n)}':"
                f"x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={W}x{H}:fps={FPS},"
                f"trim=end_frame={n},setpts=PTS-STARTPTS")

    # ── 一般 cover 填滿 ────────────────────────────────────────────────
    if not is_img:                              # 影片：已有運動，對焦裁切（人物 vlog 常把臉放上半）
        return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:x='{_cxp}':y='{_cyp}',setsar=1,fps={FPS}")

    SS = 2                                       # 照片：2x supersample → zoompan 平滑
    Wl, Hl = W * SS, H * SS
    cover = (f"scale={Wl}:{Hl}:force_original_aspect_ratio=increase,"
             f"crop={Wl}:{Hl}:x='{_cxp}':y='{_cyp}',setsar=1,fps={FPS}")
    if kind in (None, "none", "static") or dur < 1.2:
        return f"{cover},scale={W}:{H}"

    if kind == "pan":                            # 橫幅 → 左右平移，輕微、緩入緩出
        z = 1.06
        x0, x1 = f"(iw-iw/{z})*0.12", f"(iw-iw/{z})*0.88"
        expr = (f"z='{z}':x='{x0}+(({x1})-({x0}))*{_ease(n)}':y='(ih-ih/{z})/2'")
    else:                                        # zoom in 慢推向焦點，緩入緩出
        z0, z1 = 1.0, 1.09
        expr = (f"z='{z0}+{z1 - z0:.3f}*{_ease(n)}':"
                f"x='(iw-iw/zoom)*{fx:.3f}':y='(ih-ih/zoom)*{fy:.3f}'")
    return (f"{cover},zoompan={expr}:d=1:s={W}x{H}:fps={FPS},"
            f"trim=end_frame={n},setpts=PTS-STARTPTS")


def _has_audio(src: str) -> bool:
    r = _sh(["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(src)])
    return bool(r.stdout.strip())


def render_shot(shot: dict, dur: float, out: Path, ff: str, W: int, H: int,
                grade: dict | None, luts_dir: Path, ambient_db: float = 0.0) -> bool:
    raw = shot.get("file_path") or shot.get("media_file") or ""
    si = float(shot.get("source_in", 0.0) or 0.0)

    # Live Photo 定格：抓「微動態」尾巴那一幀當靜照 → 動→定格零跳動，再從靜止 Ken Burns
    frz = shot.get("_freeze_from")
    src = None
    if frz:
        mov, t = frz
        frame = out.with_name(out.stem + "_frz.png")
        _sh([ff, "-nostdin", "-y", "-ss", f"{float(t):.3f}", "-i", str(mov),
             "-frames:v", "1", "-q:v", "2", str(frame)])
        if frame.exists() and frame.stat().st_size > 0:
            src = str(frame)
    if src is None:
        src = resolve_existing_path(str(raw), SEARCH_DIRS)
    if not src:
        return False
    is_vid = is_video_path(src)

    focus = None
    kbf = shot.get("kb_focus")          # effects_engine 存的 (cx, cy) 顯著性/人臉焦點
    if isinstance(kbf, (list, tuple)) and len(kbf) == 2:
        focus = (float(kbf[0]), float(kbf[1]))
    else:
        try:
            from backend.util.photos_meta import face_center, load_photos_meta, meta_for
            fc = face_center(meta_for(shot, load_photos_meta()))   # (cy, cx)
            if fc:
                focus = (fc[1], fc[0])
        except Exception:
            pass
    vf = framing_vf(shot.get("ken_burns"), dur, W, H, src, is_img=not is_vid, focus=focus)
    if grade:
        stem = Path(src).stem.lower()
        cdl = grade.get(stem) or grade.get(Path(str(raw)).stem.lower())
        if cdl:
            lut = luts_dir / f"{stem}.cube"
            if not lut.exists():
                cdl_to_cube(cdl, lut)
            vf += f",lut3d=file='{lut}'"
    vf += ",format=yuv420p"

    gain = f",volume={ambient_db:.1f}dB" if abs(ambient_db) > 0.05 else ""
    cmd = [ff, "-nostdin", "-y"]
    if is_vid and _has_audio(str(src)):
        cmd += ["-ss", f"{si:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
        amap = ["-map", "0:a?", "-af",
                f"aresample=48000,aformat=cl=stereo,apad,atrim=0:{dur:.3f}{gain}"]
    elif is_vid:
        # 影片沒音軌（例如片頭路線動畫）→ 補靜音，才能跟其他片段 concat
        cmd += ["-ss", f"{si:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=cl=stereo:r=48000"]
        amap = ["-map", "1:a"]
    else:
        cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-r", str(FPS), "-i", str(src),
                "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=cl=stereo:r=48000"]
        amap = ["-map", "1:a"]
    cmd += ["-map", "0:v", *amap, "-vf", vf, "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-shortest", str(out)]
    r = _sh(cmd)
    if r.returncode != 0 or not out.exists():
        # 影片沒音軌 → 補靜音重試
        if is_vid and "0:a?" in " ".join(cmd):
            cmd2 = [ff, "-nostdin", "-y", "-ss", f"{si:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                    "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=cl=stereo:r=48000",
                    "-map", "0:v", "-map", "1:a", "-vf", vf, "-t", f"{dur:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                    "-shortest", str(out)]
            r = _sh(cmd2)
            if r.returncode == 0 and out.exists():
                return True
        print(f"   ✗ shot {shot.get('shot_index')}: {r.stderr.strip()[-160:]}")
        return False
    return True


def _ass_ts(x: float) -> str:
    x = max(0.0, x)
    h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_parse_ts(s: str) -> float:
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _remap_dialogue_times(txt: str, tmap) -> str:
    if tmap is None:
        return txt
    out = []
    for ln in txt.splitlines():
        if ln.startswith("Dialogue:"):
            p = ln.split(",", 3)
            if len(p) >= 4:
                p[1] = _ass_ts(tmap(_ass_parse_ts(p[1].strip())))
                p[2] = _ass_ts(tmap(_ass_parse_ts(p[2].strip())))
                ln = ",".join(p)
        out.append(ln)
    return "\n".join(out)


def _xfade_shift(layout: list, xf: float):
    """硬切時間軸 t → 章間 xfade 後的時間軸（每經過一個章界提前 xf 秒）。"""
    if xf <= 0:
        return None
    starts, prev = [], object()
    for e in layout:
        sid = e["shot"].get("scene_id")
        if sid != prev:
            starts.append(e["start"]); prev = sid
    bounds = starts[1:]
    return lambda t: max(0.0, t - xf * sum(1 for b in bounds if b <= t + 1e-6))


def make_ass(srt: Path, ass: Path, ff: str, W: int, H: int, tmap=None) -> None:
    """SRT → ASS，把 [Script Info] 的 PlayRes 與 [V4+ Styles] 換成 preset 的樣式。
    比 subtitles filter 的 force_style 可靠（force_style 遇到含空白的字體名常靜默失效）。
    tmap：把字幕時間碼從硬切時間軸重新映射到章間 xfade 後的時間軸。"""
    from backend.util.subtitle_preset import STYLE as S

    subprocess.run([ff, "-nostdin", "-y", "-i", str(srt), str(ass)], capture_output=True)
    txt = ass.read_text("utf-8")

    def bgr(hexrgb: str) -> str:
        return f"&H00{hexrgb[4:6]}{hexrgb[2:4]}{hexrgb[0:2]}".upper()

    k = H / 1080.0
    style = (
        "Style: Default,{font},{size},{fill},{fill},{outl},&H64000000&,{bold},0,0,0,"
        "100,100,0,0,{border},{sz},{shadow},2,40,40,{mv},1"
    ).format(
        font=S["font"], size=round(S["size_1080"] * k),
        fill=bgr(S["fill"]), outl=bgr(S["outline"]),
        bold=-1 if S.get("bold") else 0,
        border=3 if S.get("box") else 1,
        sz=max(1, round(S["outline_px_1080"] * k)),
        shadow=2 if S.get("shadow") else 0,
        mv=round(H * S["margin_v_frac"]),
    )
    txt = re.sub(r"PlayResX:\s*\d+", f"PlayResX: {W}", txt)
    txt = re.sub(r"PlayResY:\s*\d+", f"PlayResY: {H}", txt)
    txt = re.sub(r"^Style:\s*Default,.*$", style, txt, count=1, flags=re.M)
    if "PlayResX" not in txt:
        txt = txt.replace("[Script Info]", f"[Script Info]\nPlayResX: {W}\nPlayResY: {H}", 1)

    # 感觸軌（琥珀色）→ 偏下的置中 + 放大。ffmpeg 把 SRT 的 <font color="#E8C88C"> 轉成
    # ASS 的 {\c&H8CC8E8&}（BGR 反序）。認到那個色碼就在該 Dialogue 文字前加 override。
    if S.get("reflection_center", True):
        rc = S.get("reflection_fill", "E8C88C")
        rc_bgr = (rc[4:6] + rc[2:4] + rc[0:2]).lower()          # E8C88C → 8cc8e8
        big = round(S["size_1080"] * S.get("reflection_size_mul", 1.18) * k)
        py = round(H * S.get("reflection_y_frac", 0.80))        # 螢幕高的 ~80% = 下方，不擋主體
        out_lines = []
        for ln in txt.splitlines():
            if ln.startswith("Dialogue:") and rc_bgr in ln.lower():
                p = ln.split(",", 9)               # 前 9 欄固定，第 10 欄才是文字
                if len(p) == 10:
                    p[9] = f"{{\\an5\\pos({W // 2},{py})\\fs{big}}}{p[9]}"
                    ln = ",".join(p)
            out_lines.append(ln)
        txt = "\n".join(out_lines)

    txt = _remap_dialogue_times(txt, tmap)   # 硬切時間軸 → 章間 xfade 後的時間軸
    ass.write_text(txt, "utf-8")


# Apple 在都會區給的是「里 / POI」小地名，太細又難懂 → 收斂成大家認得的地名或直接不顯示
_PLACE_FIX = {
    "滬尾": "淡水", "淡海": "淡水", "潭美": "台北", "奇岩": "台北", "下埤頭": "台北",
    "城內": "台中", "下崁": "台中", "大湖": "台中",
}


def _same_next_day(d0: str, d1: str) -> bool:
    """d1 是 d0 的隔天？（YYYY-MM-DD 字串）"""
    from datetime import date
    try:
        y0, m0, dd0 = map(int, d0.split("-"))
        y1, m1, dd1 = map(int, d1.split("-"))
        return (date(y1, m1, dd1) - date(y0, m0, dd0)).days == 1
    except Exception:
        return False


def _short_place(name: str) -> str:
    """行政區地名 → 去掉『縣/市』前綴與『鄉/鎮/市/區』後綴，留鄉鎮名。認不出的小地名不顯示。"""
    if not name:
        return ""
    name = name.split("·")[0].strip()                       # 去掉『臺東市·台東體育館』的 POI 尾巴
    m = re.search(r"[一-鿿]{2,3}[縣市]([一-鿿]{1,4})[鄉鎮市區]", name)
    if m:
        return m.group(1)
    m = re.search(r"([一-鿿]{1,4})[鄉鎮市區]", name)          # 「大肚區」「后里區」等無縣市前綴
    if m:
        return m.group(1)
    if name in _PLACE_FIX:
        return _PLACE_FIX[name]
    return ""                                                # 純里名 / POI → 寧可不顯示


def chyron_ass(data: dict, layout: list, ass: Path, W: int, H: int, tmap=None, xf: float = 0.0) -> bool:
    """右下角資訊軌：每個小章節一條 Dialogue，持續整章。Day / 地名 / 里程 / 海拔。
    xf = 章間交叉溶接秒數：每條在章界前提早 xf+0.3s 收，免得溶接時前後兩章的 Day 標同時出現。"""
    from backend.util.subtitle_preset import STYLE as S
    from backend.util.photos_meta import load_photos_meta, meta_for

    pm = load_photos_meta()
    date_earliest: dict[str, str] = {}
    date_count: dict[str, int] = {}
    for e in layout:
        t = meta_for(e["shot"], pm).get("taken") or ""
        if len(t) >= 16:
            d, hm = t[:10], t[11:16]
            date_count[d] = date_count.get(d, 0) + 1
            if d not in date_earliest or hm < date_earliest[d]:
                date_earliest[d] = hm
    all_dates = sorted(date_earliest)
    # 「第 N 天」：隔天清晨的一兩顆收尾鏡併進前一天，不另起一天
    day_rank: dict[str, int] = {}
    n = 0
    for i, d in enumerate(all_dates):
        coda = (i > 0 and date_count.get(d, 0) <= 2 and date_earliest[d] < "08:00"
                and _same_next_day(all_dates[i - 1], d))
        if coda:
            day_rank[d] = n
        else:
            n += 1
            day_rank[d] = n

    # 分章（scene_id 優先，否則 scene_title【】）
    groups: list[list] = []
    prev = object()
    for e in layout:
        s = e["shot"]
        key = s.get("scene_id")
        if key is None:
            m = re.search(r"[【\[]([^】\]]+)", s.get("scene_title", "") or "")
            key = m.group(1) if m else s.get("scene_title", "")
        if key != prev:
            groups.append([])
            prev = key
        groups[-1].append(e)

    lines = []
    for g in groups:
        recs = [meta_for(e["shot"], pm) for e in g]
        dates = [r.get("taken", "")[:10] for r in recs if r.get("taken")]
        day = day_rank.get(dates[0]) if dates else None
        places = [_short_place((r.get("place") or {}).get("name", "")).replace("臺", "台")
                  for r in recs if isinstance(r.get("place"), dict)]
        # 連續去重，留下行進順序：['台北','台北','淡水'] → ['台北','淡水']
        seq: list[str] = []
        for p in places:
            if p and (not seq or seq[-1] != p):
                seq.append(p)
        alts = [r["gps"].get("alt") for r in recs if r.get("gps") and r["gps"].get("alt")]

        if not seq:                       # 沒地名就不顯示（單一個 Day N 沒意義、還搶畫面）
            continue
        parts = []
        if day:
            parts.append(f"Day {day}")
        parts.append(seq[0] if seq[0] == seq[-1] else f"{seq[0]} → {seq[-1]}")
        # 里程不寫：GPS 點距和會把「搭火車」也算進去，會誤導。海拔是點測、可信。
        if alts and max(alts) > 130:
            parts.append(f"海拔 {int(max(alts))} m")
        t0 = g[0]["start"]
        t1 = g[-1]["start"] + g[-1]["dur"] - 0.05
        if tmap is not None:
            t0, t1 = tmap(t0), tmap(t1)
        t1 = max(t0 + 1.0, t1 - (xf + 0.3))   # 章界前收掉，溶接時不會前後兩章 Day 標疊著
        lines.append((t0, t1, "  ｜  ".join(parts)))

    if not lines:
        return False

    def bgr_a(hexrgb: str, alpha: float) -> str:
        aa = f"{int(max(0.0, min(1.0, alpha)) * 255):02X}"
        return f"&H{aa}{hexrgb[4:6]}{hexrgb[2:4]}{hexrgb[0:2]}".upper()

    k = H / 1080.0
    mv = round(H * S.get("meta_margin_frac", 0.035))
    fill = bgr_a(S.get("meta_fill", "E8E8E8"), S.get("meta_alpha", 0.08))
    use_box = S.get("meta_box", True)
    # BorderStyle=3 → OutlineColour 當半透明黑底框，Outline 值 = 內距；否則走細描邊
    border_style = 3 if use_box else 1
    box_col = bgr_a("000000", S.get("meta_box_alpha", 0.45))
    outline_col = box_col if use_box else bgr_a("000000", 0.0)
    outline_px = round(S.get("meta_box_pad_1080", 12) * k) if use_box else max(1, round(2 * k))
    head = (
        "[Script Info]\n"
        f"PlayResX: {W}\nPlayResY: {H}\nScriptType: v4.00+\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Meta,{S['font']},{round(S.get('meta_size_1080', 40) * k)},"
        f"{fill},{fill},{outline_col},&H00000000&,0,0,0,0,100,100,0,0,"
        f"{border_style},{outline_px},0,3,{mv},{mv},{mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def ts(x: float) -> str:
        x = max(0, x)
        h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    body = "".join(f"Dialogue: 0,{ts(a)},{ts(b)},Meta,,0,0,0,,{t}\n" for a, b, t in lines)
    ass.write_text(head + body, "utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", nargs="?", default=PREFIX)
    ap.add_argument("-o", "--out")
    ap.add_argument("--grade", action="store_true", help="套 grade_hints 的 CDL 調色")
    ap.add_argument("--music", help="音樂 bed 檔（會 loop、對白段自動壓低、整體 loudnorm 到 -15）")
    ap.add_argument("--no-loudnorm", action="store_true", help="不做最終響度正規化")
    ap.add_argument("--for-imovie", action="store_true",
                    help="給 iMovie 收尾（自己錄口白+配樂）：燒字幕當口白稿、現場音壓到 -10dB 當環境底、"
                         "不加音樂不 loudnorm、H.264 high+faststart")
    ap.add_argument("--ambient-db", type=float, default=0.0,
                    help="現場原音增益 dB（--for-imovie 預設 -10；純環境底不要蓋掉之後錄的口白）")
    ap.add_argument("--no-subs", action="store_true", help="不燒字幕")
    ap.add_argument("--fast", action="store_true", help="720p / veryfast（給 self-eval proxy / 手機預覽）")
    ap.add_argument("--crf", type=int, default=None, help="最終畫質 CRF（越大越小，預設 fast=28 / 正式=20）")
    ap.add_argument("--start", type=float, default=0.0, help="只渲從第 N 秒開始")
    ap.add_argument("--duration", type=float, default=0.0, help="只渲這麼多秒（0=到片尾）")
    ap.add_argument("--keep", action="store_true", help="保留逐鏡片段（除錯用）")
    ap.add_argument("--xfade", type=float, default=0.8, help="章與章之間交叉溶接秒數（章內硬切）")
    ap.add_argument("--no-xfade", action="store_true", help="全片硬切，不做章間溶接")
    args = ap.parse_args()
    if args.for_imovie:
        args.no_loudnorm = True
        args.music = None
        if args.ambient_db == 0.0:
            args.ambient_db = -10.0

    # 需要 subtitles(libass) filter → 主 ffmpeg 沒編，用 ffmpeg-full
    ff = next((p for p in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
                           shutil.which("ffmpeg") or "") if p and Path(p).exists()), "ffmpeg")
    if "full" not in ff:
        print("   ⚠️ 沒有 ffmpeg-full，字幕可能燒不進去。brew install ffmpeg-full")
    src_json = ROOT / f"{args.prefix}.json"
    if not src_json.exists():
        sys.exit(f"找不到 {src_json}")
    data = json.loads(src_json.read_text("utf-8"))
    W, H = (1280, 720) if args.fast else (1920, 1080)

    grade = None
    if args.grade:
        gh = ROOT / f"{args.prefix}_grade_hints.json"
        grade = json.loads(gh.read_text("utf-8")) if gh.exists() else {}
        if not grade:
            print("   ⚠️ 沒有 grade_hints.json，跳過調色")

    full = timeline_layout(data, SEARCH_DIRS)
    t_end = args.start + args.duration if args.duration else 1e9
    layout = []
    for e in full:
        a, b = e["start"], e["start"] + e["dur"]
        if b <= args.start or a >= t_end:
            continue
        head = max(0.0, args.start - a)          # 視窗切掉這顆前面多少
        tail = max(0.0, b - t_end)
        d = e["dur"] - head - tail
        if d < 0.1:
            continue
        sh = dict(e["shot"])
        if head and is_video_path(resolve_existing_path(
                str(sh.get("file_path") or sh.get("media_file") or ""), SEARCH_DIRS) or ""):
            sh["source_in"] = float(sh.get("source_in", 0.0) or 0.0) + head
        layout.append({"shot": sh, "dur": round(d, 3)})
    if not layout:
        sys.exit("時間範圍內沒有鏡頭")

    # Live Photo 定格段：改抓前一顆「微動態」MOV 的尾幀當靜照（動→定格無縫），再從靜止推鏡
    for i in range(1, len(layout)):
        cur, prev = layout[i]["shot"], layout[i - 1]["shot"]
        tag = f"{cur.get('visual_action', '')}{cur.get('live_photo_usage', '')}{cur.get('visual_description', '')}"
        if "定格" not in tag or (cur.get("media_type") or "") == "video":
            continue
        if Path(str(prev.get("media_file") or "")).stem != Path(str(cur.get("media_file") or "")).stem:
            continue
        pv = resolve_existing_path(str(prev.get("file_path") or prev.get("media_file") or ""), SEARCH_DIRS)
        if not pv or not is_video_path(pv):
            continue
        t = float(prev.get("source_out", 0.0) or 0.0) - 1.0 / FPS
        cur["_freeze_from"] = (str(pv), max(0.0, t))

    work = Path(tempfile.mkdtemp(prefix="render_"))
    luts = work / "luts"
    luts.mkdir()
    segs: list[Path] = []
    seg_sids: list = []                       # 每個「成功」片段對應的 scene_id（給 xfade 分組）
    print(f"渲染 {len(layout)} 個鏡頭 @ {W}x{H}{' +調色' if grade else ''} …")
    for i, e in enumerate(layout):
        seg = work / f"{i:04d}.mp4"
        if render_shot(e["shot"], e["dur"], seg, ff, W, H, grade, luts, args.ambient_db):
            segs.append(seg)
            seg_sids.append(e["shot"].get("scene_id"))
        if (i + 1) % 25 == 0:
            print(f"   {i + 1}/{len(layout)}")

    if not segs:
        sys.exit("沒有成功的鏡頭片段")

    pre = work / "pre.mp4"
    want = sum(e["dur"] for e in layout)

    def _probe_dur(p: Path) -> float:
        return float(_sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(p)]).stdout.strip() or 0)

    # concat *filter*（不是 demuxer）：逐段 setpts/fps/scale/format 正規化再接 —— demuxer 對
    # 圖片 loop / zoompan / overlay 產生的片段會在 pix_fmt(full/limited) 或時間戳不一致時整條截斷。
    def _concat_filter(parts: list[Path], dst: Path) -> bool:
        ins: list[str] = []
        for p in parts:
            ins += ["-i", str(p)]
        fc = ("".join(f"[{i}:v:0]setpts=PTS-STARTPTS,fps={FPS},scale={W}:{H},"
                      f"setsar=1,format=yuv420p[v{i}];"
                      f"[{i}:a:0]aresample=48000:async=1,asetpts=PTS-STARTPTS[a{i}];"
                      for i in range(len(parts)))
              + "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
              + f"concat=n={len(parts)}:v=1:a=1[v][a]")
        r = _sh([ff, "-nostdin", "-y", *ins, "-filter_complex", fc,
                 "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(dst)])
        if r.returncode != 0 or not dst.exists():
            print(f"   concat 失敗：{r.stderr.strip()[-300:]}")
            return False
        return True

    def _concat_batched(parts: list[Path], dst: Path, batch: int = 60) -> bool:
        if len(parts) <= batch:
            return _concat_filter(parts, dst)
        chunks = []
        for j in range(0, len(parts), batch):
            c = dst.with_name(f"{dst.stem}_c{j:04d}.mp4")
            if not _concat_filter(parts[j:j + batch], c):
                return False
            chunks.append(c)
        return _concat_filter(chunks, dst)

    XF = 0.0 if (args.no_xfade or args.start or args.duration) else args.xfade
    if XF <= 0:
        # 硬切：全部一次接完
        if not _concat_batched(segs, pre):
            sys.exit("concat 失敗")
    else:
        # 章內硬切、章間交叉溶接：先逐 scene concat，再 xfade 串起來
        groups: list[list[Path]] = []
        gkeys: list = []
        for sid, seg in zip(seg_sids, segs):
            if not groups or gkeys[-1] != sid:
                groups.append([]); gkeys.append(sid)
            groups[-1].append(seg)
        scene_files: list[Path] = []
        for gi, g in enumerate(groups):
            sf = work / f"scene_{gi:02d}.mp4"
            if not _concat_batched(g, sf):
                sys.exit("scene concat 失敗")
            scene_files.append(sf)
        if len(scene_files) == 1:
            shutil.copy(scene_files[0], pre)
        else:
            durs = [_probe_dur(f) for f in scene_files]
            ins: list[str] = []
            for f in scene_files:
                ins += ["-i", str(f)]
            vparts, aparts = [], []
            vcur, acur, off = "[0:v]", "[0:a]", 0.0
            for idx in range(1, len(scene_files)):
                off += durs[idx - 1] - XF
                vout = f"[vx{idx}]" if idx < len(scene_files) - 1 else "[v]"
                aout = f"[ax{idx}]" if idx < len(scene_files) - 1 else "[a]"
                vparts.append(f"{vcur}[{idx}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}{vout}")
                aparts.append(f"{acur}[{idx}:a]acrossfade=d={XF}{aout}")
                vcur, acur = vout, aout
            r = _sh([ff, "-nostdin", "-y", *ins, "-filter_complex",
                     ";".join(vparts + aparts), "-map", "[v]", "-map", "[a]",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                     "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", str(pre)])
            if r.returncode != 0 or not pre.exists():
                sys.exit(f"xfade 串接失敗：{r.stderr.strip()[-300:]}")
            print(f"   {len(scene_files)} 個場景 · 章間 {XF}s 交叉溶接")

    pdur = _probe_dur(pre)
    exp = want - (XF * max(0, len({e['shot'].get('scene_id') for e in layout}) - 1) if XF > 0 else 0)
    print(f"   concat → {pdur:.1f}s（預期 ~{exp:.1f}s）")
    if pdur < exp - 2.0:
        print("   ⚠️ 短少 >2s，有片段被截斷")

    out = Path(args.out) if args.out else ROOT / f"{args.prefix}.mp4"
    srt = ROOT / f"{args.prefix}_字幕.srt"
    crf = str(args.crf if args.crf is not None else (28 if args.fast else 20))
    preset = "veryfast" if args.fast else "medium"

    # 一個 filter_complex 同時做：燒字幕 + 右下角資訊軌（[v]）+ 音樂 bed / loudnorm（[a]）
    vchain = "[0:v]"
    if srt.exists() and not args.no_subs:
        tmap = _xfade_shift(full, XF)                 # 字幕/資訊軌時間碼 → 章間 xfade 後的時間軸
        make_ass(srt, work / "subs.ass", ff, W, H, tmap)   # SRT→ASS + preset 樣式（路徑不含空白）
        vchain += "ass=subs.ass,"
        if not args.start and not args.duration:      # 資訊軌時間碼是全片的，切片模式不燒
            if chyron_ass(data, full, work / "meta.ass", W, H, tmap, xf=XF):
                vchain += "ass=meta.ass,"
    vchain += "null[v]"

    cmd = [ff, "-nostdin", "-y", "-i", "pre.mp4"]
    parts = [vchain]
    if args.music and Path(args.music).exists():
        cmd += ["-stream_loop", "-1", "-i", str(Path(args.music).resolve())]
        parts.append("[0:a]asplit=2[a0][sc]")
        parts.append("[1:a][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bed]")
        parts.append("[a0][bed]amix=inputs=2:weights=1 0.5:duration=first[am]")
        alast = "[am]"
    else:
        alast = "[0:a]"
    parts.append(f"{alast}loudnorm=I=-15:TP=-1.5:LRA=11[a]"
                 if not args.no_loudnorm else f"{alast}anull[a]")
    vcodec = ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
    if args.for_imovie:
        vcodec += ["-profile:v", "high", "-level", "4.0", "-movflags", "+faststart"]
    r = _sh([*cmd, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
             *vcodec, "-c:a", "aac", "-b:a", "192k", "-shortest", str(out.resolve())], cwd=work)
    if r.returncode != 0:
        sys.exit(f"最終合成失敗：{r.stderr.strip()[-300:]}")

    dur = float(_sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(out)]).stdout.strip() or 0)
    mb = out.stat().st_size / 1e6
    print(f"\n✅ {out}  （{dur:.0f}s / {mb:.0f} MB / {W}x{H}）")
    if args.keep:
        print(f"   逐鏡片段留在 {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
