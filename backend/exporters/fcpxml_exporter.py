"""進階 FCPXML 1.9 匯出引擎。

相較舊版的重點修正：
1. 每個素材都輸出 <media-rep kind="original-media">，並附正確的 <format>
   (真實寬高 / 影格率 / 時長)。缺這些正是「Media Offline」的主因。
2. 路徑一律對應到「真的存在」的檔案；找不到就保留原路徑並在 marker 標記
   需重新連結，絕不再自己竄改副檔名指向不存在的檔。
3. 圖片用 <video> 參照、影片用 <asset-clip>；時間全部對齊影格。
4. 字幕 <title> 放大字級、加粗、黑色描邊 + 陰影、置於下三分之一。
5. 支援 storyboard 內的 source_in / source_out (晃動自動剪除後的穩定區間) 與 skip。
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util.media_probe import probe_image, probe_video, resolve_existing_path, is_video_path

FPS = 30
FRAME_UNIT = 100  # 100/3000s = 1/30s


def _dur_str(seconds: float) -> str:
    frames = int(round(float(seconds) * FPS))
    if frames <= 0:
        return "0s"
    return f"{frames * FRAME_UNIT}/3000s"


def _frames(seconds: float) -> int:
    return max(0, int(round(float(seconds) * FPS)))


def _frame_dur_str(frames: int) -> str:
    return f"{frames * FRAME_UNIT}/3000s"


def _motion_label(camera_motion) -> str:
    if isinstance(camera_motion, dict):
        return str(camera_motion.get("motion_type", "Static"))
    return str(camera_motion or "Static")


class FCPXMLExporter:

    SUBTITLE_FONT = "PingFang TC"
    SUBTITLE_SIZE = 72          # 1080p、每行 16 全形字下不會撐出安全區
    SUBTITLE_Y = -430           # 置中靠下 (畫面下緣約 9%，字幕安全區內)

    # Resolve 匯入 FCPXML 的 <title> 會忽略 Position → 字幕全跑到正中央壓畫面。
    # 預設不寫 <title>，字幕改走 SRT 字幕軌 (File ▸ Import ▸ Subtitle)。
    # --fcp-titles 才寫 (給 Final Cut)。
    EMIT_TITLES = False

    def export(self, script_data: Dict[str, Any], output_path: Optional[str] = None,
               search_dirs: Optional[List[str]] = None, abroll: bool = False) -> str:
        self._ts_counter = 0
        title = str(script_data.get("project_title", "iMovie_Script_Project")).replace(" ", "_")
        target_ratio = script_data.get("target_aspect_ratio", "16:9")
        storyboard: List[Dict[str, Any]] = [
            s for s in script_data.get("storyboard", []) if not s.get("skip")
        ]

        if target_ratio == "9:16":
            seq_w, seq_h = "1080", "1920"
        elif target_ratio == "4:3":
            seq_w, seq_h = "1440", "1080"
        elif target_ratio == "1:1":
            seq_w, seq_h = "1080", "1080"
        else:
            seq_w, seq_h = "1920", "1080"
        seq_w_i, seq_h_i = int(seq_w), int(seq_h)

        fcpxml = ET.Element("fcpxml", version="1.9")
        resources = ET.SubElement(fcpxml, "resources")

        ET.SubElement(
            resources, "format", id="r0", name=f"FFVideoFormat_{seq_w}x{seq_h}_30p",
            frameDuration="100/3000s", width=seq_w, height=seq_h,
            colorSpace="1-1-1 (Rec. 709)",
        )
        ET.SubElement(
            resources, "effect", id="r_title", name="Basic Title",
            uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti",
        )

        # ── 建立 asset / format 資源 ──────────────────────────────────────
        missing_notes: List[str] = []
        asset_info: Dict[int, Dict[str, Any]] = {}
        seen: Dict[str, Dict[str, Any]] = {}  # 真實路徑 -> asset info (去重)

        for idx, shot in enumerate(storyboard, start=1):
            raw = shot.get("file_path") or shot.get("media_file") or f"shot_{idx}.jpg"
            resolved = resolve_existing_path(str(raw), search_dirs)
            exists = resolved is not None
            path = resolved if resolved else Path(str(raw))
            key = str(path)

            if key in seen:
                asset_info[idx] = dict(seen[key])
                continue

            asset_id = f"r_asset_{len(seen) + 1}"
            fmt_id = f"r_fmt_{len(seen) + 1}"

            is_vid = is_video_path(path)
            file_uri = path.as_uri() if path.is_absolute() else f"file://localhost/{path}"

            if is_vid:
                info = probe_video(key) if exists else {
                    "width": 1920, "height": 1080, "duration_s": float(shot.get("duration_seconds", 4.0)),
                    "fps": 30.0, "has_audio": True,
                }
                ET.SubElement(
                    resources, "format", id=fmt_id, name=f"fmt_{asset_id}",
                    frameDuration="100/3000s",
                    width=str(info["width"]), height=str(info["height"]),
                )
                asset_dur = _dur_str(max(info["duration_s"], 0.1))
                asset_el = ET.SubElement(
                    resources, "asset", id=asset_id, name=path.name,
                    start="0s", duration=asset_dur, format=fmt_id,
                    hasVideo="1", videoSources="1",
                    hasAudio="1" if info["has_audio"] else "0",
                    audioSources="1" if info["has_audio"] else "0",
                    audioChannels="2" if info["has_audio"] else "0",
                )
                asset_info[idx] = {
                    "asset_id": asset_id, "kind": "video",
                    "duration_s": info["duration_s"], "exists": exists,
                }
            else:
                info = probe_image(key) if exists else {"width": 1920, "height": 1080}
                # Resolve 的靜態圖片地雷（血淚）：
                #  - format 缺 frameDuration → offline（補上）
                #  - MOV 與 JPG 同名（都叫 IMG_2440）→ Resolve 按 name 去重，
                #    其中一個 clip 變 offline → asset name 帶副檔名 (IMG_2440.JPG)
                #  - 靜態圖 asset duration 給有限大值 → Resolve 當成超長影片找不到影格
                #    → 用 FCP 慣例 duration="0s"（timeless）
                ET.SubElement(
                    resources, "format", id=fmt_id, name=f"fmt_{asset_id}",
                    frameDuration="100/3000s",
                    width=str(info["width"]), height=str(info["height"]),
                    colorSpace="1-13-1 (sRGB IEC61966-2.1)",
                )
                still_uid = hashlib.md5(key.encode("utf-8")).hexdigest().upper()
                asset_el = ET.SubElement(
                    resources, "asset", id=asset_id, name=path.name, uid=still_uid,
                    start="0s", duration="0s", format=fmt_id,
                    hasVideo="1", videoSources="1",
                    hasAudio="0", audioSources="0",
                )
                asset_info[idx] = {
                    "asset_id": asset_id, "kind": "image",
                    "duration_s": None, "exists": exists,
                }

            ET.SubElement(asset_el, "media-rep", kind="original-media", src=file_uri)
            seen[key] = asset_info[idx]
            if not exists:
                missing_notes.append(f"{idx:02d}: {raw}")

        # ── Library / Event / Project / Sequence ────────────────────────
        library = ET.SubElement(fcpxml, "library")
        event = ET.SubElement(library, "event", name=f"{title}_Event")
        project = ET.SubElement(event, "project", name=title)
        sequence = ET.SubElement(
            project, "sequence", format="r0", tcStart="0s", tcFormat="NDF", duration="0s"
        )
        spine = ET.SubElement(sequence, "spine")

        ctx = {
            "asset_info": asset_info, "W": seq_w_i, "H": seq_h_i,
        }
        if abroll:
            total_frames = self._build_spine_abroll(spine, storyboard, ctx)
        else:
            offset_frames = 0
            for idx, shot in enumerate(storyboard, start=1):
                offset_frames += self._emit_clip(
                    spine, idx, shot, ctx, offset_frames=offset_frames
                )
            total_frames = offset_frames

        sequence.set("duration", _frame_dur_str(total_frames))

        ET.indent(fcpxml, space="  ")
        xml_str = ET.tostring(fcpxml, encoding="utf-8", xml_declaration=True).decode("utf-8")

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(xml_str)
        if missing_notes:
            print(f"[FCPXML] ⚠️ {len(missing_notes)} 個鏡頭素材找不到，已於專案內標記需重新連結。")
        return xml_str

    # ── 單一鏡頭 clip 產生 ────────────────────────────────────────────
    def _clip_seconds(self, shot: Dict[str, Any], info: Dict[str, Any]) -> float:
        src_in = float(shot.get("source_in", 0.0) or 0.0)
        src_out = shot.get("source_out")
        if src_out is not None and info["kind"] == "video":
            clip_sec = max(1.0 / FPS, float(src_out) - src_in)
        else:
            clip_sec = float(shot.get("duration_seconds", 4.0))
        if info["kind"] == "video" and info.get("duration_s"):
            clip_sec = min(clip_sec, max(1.0 / FPS, info["duration_s"] - src_in))
        return clip_sec

    def _vo_span(self, shot: Dict[str, Any], fallback: float) -> float:
        """旁白唸完所需的時間 (timed_subtitles 最後一則的結束點 + 尾巴)。"""
        subs = shot.get("timed_subtitles") or []
        if not subs:
            return fallback
        end = max(float(s.get("start_offset", 0.0)) + float(s.get("duration", 0.0)) for s in subs)
        return max(fallback, end + 0.3)

    def _emit_clip(self, parent: ET.Element, idx: int, shot: Dict[str, Any], ctx: Dict[str, Any],
                   *, offset_frames: int, override_sec: Optional[float] = None,
                   lane: Optional[int] = None, mute: bool = False,
                   add_titles: bool = True, add_markers: bool = True) -> int:
        """產生一個 clip 元素 (spine 或 connected)。回傳它佔用的影格數。"""
        info = ctx["asset_info"].get(idx, {"asset_id": "r_asset_1", "kind": "image",
                                           "duration_s": None, "exists": False})
        W, H = ctx["W"], ctx["H"]
        src_in = float(shot.get("source_in", 0.0) or 0.0)
        clip_sec = override_sec if override_sec is not None else self._clip_seconds(shot, info)
        clip_frames = max(1, _frames(clip_sec))

        shot_name = f"Shot {idx:02d}: {str(shot.get('visual_action', f'Scene_{idx}'))[:30]}"
        vo_text = (shot.get("voiceover") or "").strip()

        tag = "asset-clip" if info["kind"] == "video" else "video"
        clip_attrs = {
            "ref": info["asset_id"], "name": shot_name,
            "offset": _frame_dur_str(offset_frames),
            "duration": _frame_dur_str(clip_frames), "start": _dur_str(src_in),
        }
        if info["kind"] == "video":
            clip_attrs["tcFormat"] = "NDF"
        if lane is not None:
            clip_attrs["lane"] = str(lane)
        clip = ET.SubElement(parent, tag, **clip_attrs)

        kb = shot.get("ken_burns")
        if isinstance(kb, dict) and kb.get("start"):
            self._write_transform(clip, kb, clip_sec, W, H)
        else:
            self._inject_ken_burns(clip, shot.get("camera_motion"), clip_sec)

        if mute and info["kind"] == "video":
            ET.SubElement(clip, "adjust-volume", amount="-96dB")

        if add_titles and self.EMIT_TITLES:
            timed_subs = shot.get("timed_subtitles") or []
            if timed_subs:
                for s_idx, sub_item in enumerate(timed_subs, start=1):
                    text = (sub_item.get("text") or sub_item.get("raw_text") or "").strip()
                    if not text or text in ("null", "無", "(純畫面與環境音)"):
                        continue
                    sub_off = float(sub_item.get("start_offset", 0.0))
                    sub_dur = float(sub_item.get("duration", clip_sec))
                    sub_off = max(0.0, min(sub_off, clip_sec - 1.0 / FPS))
                    sub_dur = max(1.0 / FPS, min(sub_dur, clip_sec - sub_off))
                    self._ts_counter += 1
                    self._add_title(clip, f"VO_Sub_{idx:02d}_{s_idx:02d}", f"ts_{self._ts_counter}",
                                    text, sub_off, sub_dur)
            elif vo_text and vo_text not in ("null", "無", "(純畫面與環境音)"):
                self._ts_counter += 1
                self._add_title(clip, f"VO_Sub_{idx:02d}", f"ts_{self._ts_counter}",
                                vo_text, 0.0, clip_sec)

        if add_markers:
            self._add_markers(clip, idx, shot, info)
        return clip_frames

    def _add_markers(self, clip: ET.Element, idx: int, shot: Dict[str, Any],
                     info: Dict[str, Any]) -> None:
        """導演筆記 marker — DTD 要求排在所有 anchored item (title / connected clip) 之後。"""
        vo_text = (shot.get("voiceover") or "").strip()
        transition = shot.get("transition") or "直切 (Cut)"
        ET.SubElement(clip, "marker", start="0s", duration="100/3000s", value=(
            f"【Shot {idx}】{shot.get('shot_type', '')}"
            + (f" · {shot.get('role')}" if shot.get("role") else "") + "\n"
            f"景別主體: {shot.get('visual_action', '')}\n"
            f"運鏡: {_motion_label(shot.get('camera_motion'))}\n"
            f"配樂音效: {shot.get('sound_fx', '')}\n"
            f"口白: {vo_text}"
        ))
        if shot.get("shake_cut_note"):
            ET.SubElement(clip, "marker", start="0s", duration="100/3000s",
                          value=f"⚠️ 晃動自動剪除: {shot['shake_cut_note']}")
        if not info.get("exists", True):
            ET.SubElement(clip, "marker", start="0s", duration="100/3000s",
                          value="⚠️ 素材需重新連結 (Media Missing) — 原路徑找不到檔案")
        if idx > 1 and transition and "直切" not in transition and "Cut" not in transition:
            ET.SubElement(clip, "marker", start="0s", duration="100/3000s",
                          value=f"轉場特效建議: {transition}")

    MAX_BROLL_PER_HOST = 8

    def _build_spine_abroll(self, spine: ET.Element, storyboard: List[Dict[str, Any]],
                            ctx: Dict[str, Any]) -> int:
        """A/B-roll：A-roll 鏡頭排在 spine；緊接其後的連續 B-roll 鏡頭被「吸收」成該
        A-roll 的 lane-1 靜音 connected clip，平鋪在延長的尾段上。所有字幕（含 B-roll
        原本的）都搬到 host A-roll 上、依時間位移，畫面時間與字幕一則都不掉。"""
        def is_aroll(s: Dict[str, Any]) -> bool:
            return s.get("role", "a-roll") == "a-roll"

        offset_frames = 0
        i, n = 0, len(storyboard)
        while i < n:
            shot = storyboard[i]
            idx = i + 1
            info = ctx["asset_info"].get(idx, {"kind": "image", "duration_s": None, "exists": True})

            if not is_aroll(shot):
                offset_frames += self._emit_clip(spine, idx, shot, ctx, offset_frames=offset_frames)
                i += 1
                continue

            # host 自身可見段 = 素材真實長度 (影片不硬撐避免可見定格)
            base_frames = max(1, _frames(self._clip_seconds(shot, info)))
            vo_frames = max(1, _frames(self._vo_span(shot, base_frames / FPS)))

            # 吸收後面連續的 B-roll
            brolls: List[int] = []
            j = i + 1
            while j < n and not is_aroll(storyboard[j]) and len(brolls) < self.MAX_BROLL_PER_HOST:
                brolls.append(j)
                j += 1

            b_frames = [
                max(1, _frames(self._clip_seconds(storyboard[bj], ctx["asset_info"].get(bj + 1, {"kind": "image"}))))
                for bj in brolls
            ]
            # 無 B-roll 可吸收時，host 至少撐到旁白唸完
            host_frames = base_frames + sum(b_frames)
            if not brolls:
                host_frames = max(host_frames, vo_frames)

            # 合併字幕：host 原字幕 (offset 不動) + 每個 B-roll 的字幕 (位移到它在 host 內的起點)
            merged_subs = list(shot.get("timed_subtitles") or [])
            cursor = base_frames
            for bj, bf in zip(brolls, b_frames):
                for sub in (storyboard[bj].get("timed_subtitles") or []):
                    ns = dict(sub)
                    ns["start_offset"] = float(sub.get("start_offset", 0.0)) + cursor / FPS
                    merged_subs.append(ns)
                cursor += bf
            host_shot = dict(shot)
            host_shot["timed_subtitles"] = merged_subs

            self._emit_clip(spine, idx, host_shot, ctx, offset_frames=offset_frames,
                            override_sec=host_frames / FPS, add_markers=False)
            host_clip = list(spine)[-1]

            local = base_frames
            for bj, bf in zip(brolls, b_frames):
                self._emit_clip(host_clip, bj + 1, storyboard[bj], ctx,
                                offset_frames=local, override_sec=bf / FPS,
                                lane=1, mute=True, add_titles=False, add_markers=False)
                local += bf

            self._add_markers(host_clip, idx, shot, info)  # marker 一律最後
            offset_frames += host_frames
            i = j
        return offset_frames

    # ── 字幕 title 元素 ────────────────────────────────────────────────
    def _add_title(self, parent: ET.Element, name: str, ts_id: str,
                   text: str, offset_s: float, dur_s: float) -> None:
        title = ET.SubElement(
            parent, "title", ref="r_title", name=name, lane="1",
            offset=_dur_str(offset_s), duration=_dur_str(dur_s), start="0s",
        )
        # 置中靠下定位。FCP 讀 Position param；Resolve 匯入會忽略此 param 而
        # 置中，故 Resolve 使用者請改走 SRT 字幕軌 (見 README / 匯入說明)。
        ET.SubElement(
            title, "param", name="Position",
            key="9999/999166631/999166633/1/100/101", value=f"0 {self.SUBTITLE_Y}",
        )
        text_el = ET.SubElement(title, "text")
        ts_ref = ET.SubElement(text_el, "text-style", ref=ts_id)
        ts_ref.text = text
        tsd = ET.SubElement(title, "text-style-def", id=ts_id)
        # 白字 + 黑描邊 + 柔陰影：中文筆劃密，描邊 6 會糊成一團，取 4；
        # 陰影壓低不透明度、往正下方偏一點，亮天空/海面背景下維持對比。
        ET.SubElement(
            tsd, "text-style",
            font=self.SUBTITLE_FONT, fontSize=str(self.SUBTITLE_SIZE),
            fontColor="1 1 1 1", bold="1",
            strokeColor="0 0 0 1", strokeWidth="4",
            shadowColor="0 0 0 0.6", shadowOffset="3 270", shadowBlurRadius="6",
            alignment="center",
            lineSpacing="-8",
        )

    # ── Ken Burns / 填滿目標比例 (effects_engine 算好的明確數值) ──────────
    # DaVinci Resolve 的 FCPXML importer 只讀 <adjust-transform> 的靜態屬性，
    # 不讀 <param><keyframe>。KEYFRAMES=False (預設) → 只寫「推鏡終點」的靜態框
    # (至少對準主體、有推近感)；FCP 使用者要真動畫再開 --fcp-keyframes。
    KEYFRAMES = False

    def _write_transform(self, clip_elem: ET.Element, kb: Dict[str, Any],
                         clip_sec: float, W: int, H: int) -> None:
        s, e = kb["start"], kb["end"]
        at = ET.SubElement(clip_elem, "adjust-transform")
        static = kb.get("type") == "static" or (
            abs(s["scale"] - e["scale"]) < 1e-3
            and abs(s["x"] - e["x"]) < 1e-4 and abs(s["y"] - e["y"]) < 1e-4
        )
        if static or not self.KEYFRAMES:
            f = e if not static else s          # 非靜態時取終點框
            sc = max(1.0, min(2.2, float(f["scale"])))
            at.set("scale", f"{sc:.4f} {sc:.4f}")
            # position 不寫：FCPXML 的 <adjust-transform position> 單位在 Resolve 被
            # 當成「幾個畫面」而非像素 → 一點點位移就把素材整個推出畫面。
            # 只保留 scale（置中推近），精準對焦框留給 Resolve API / 手動。
            return
        end_t = _dur_str(clip_sec)
        p_scale = ET.SubElement(at, "param", name="scale")
        ET.SubElement(p_scale, "keyframe", time="0s", value=f'{s["scale"]:.4f} {s["scale"]:.4f}')
        ET.SubElement(p_scale, "keyframe", time=end_t, value=f'{e["scale"]:.4f} {e["scale"]:.4f}')
        p_pos = ET.SubElement(at, "param", name="position")
        ET.SubElement(p_pos, "keyframe", time="0s", value=f'{s["x"] * W:.2f} {s["y"] * H:.2f}')
        ET.SubElement(p_pos, "keyframe", time=end_t, value=f'{e["x"] * W:.2f} {e["y"] * H:.2f}')

    # ── Ken Burns (舊路徑：camera_motion 字串 / 顯著性框) ──────────────
    def _inject_ken_burns(self, clip_elem: ET.Element, camera_motion: Any, clip_sec: float) -> None:
        if not camera_motion:
            return
        if isinstance(camera_motion, str):
            motion_type, start_box, end_box = camera_motion, None, None
        elif isinstance(camera_motion, dict):
            motion_type = camera_motion.get("motion_type", "Slow Zoom-in")
            start_box = camera_motion.get("start_box")
            end_box = camera_motion.get("end_box")
        else:
            return

        m = (motion_type or "").lower()
        if "zoom-in" in m or "推進" in m or "推鏡" in m:
            s_scale, e_scale, s_pos, e_pos = 1.0, 1.12, (0, 0), (0, 0)
        elif "zoom-out" in m or "拉遠" in m:
            s_scale, e_scale, s_pos, e_pos = 1.12, 1.0, (0, 0), (0, 0)
        elif "pan right" in m or "右移" in m:
            s_scale, e_scale, s_pos, e_pos = 1.1, 1.1, (-40, 0), (40, 0)
        elif "pan left" in m or "左移" in m:
            s_scale, e_scale, s_pos, e_pos = 1.1, 1.1, (40, 0), (-40, 0)
        else:
            s_scale = e_scale = 1.0
            s_pos = e_pos = (0, 0)

        if start_box and end_box and len(start_box) == 4 and len(end_box) == 4:
            sw = max(0.01, start_box[3] - start_box[1])
            ew = max(0.01, end_box[3] - end_box[1])
            s_scale = round(min(2.5, 1.0 / sw), 3)
            e_scale = round(min(2.5, 1.0 / ew), 3)

        # 純無操作 (Static 且沒推沒移) → 不寫 adjust-transform
        if abs(e_scale - 1.0) < 1e-3 and e_pos == (0, 0) and abs(s_scale - 1.0) < 1e-3:
            return
        at = ET.SubElement(clip_elem, "adjust-transform")
        if (abs(s_scale - e_scale) < 1e-3 and s_pos == e_pos) or not self.KEYFRAMES:
            sc = max(1.0, min(2.2, float(e_scale) if e_scale > 1.0 else float(s_scale)))
            at.set("scale", f"{sc:.4f} {sc:.4f}")   # 只 scale，position 見上方註解
        else:
            end_t = _dur_str(clip_sec)
            p_scale = ET.SubElement(at, "param", name="scale")
            ET.SubElement(p_scale, "keyframe", time="0s", value=f"{s_scale} {s_scale}")
            ET.SubElement(p_scale, "keyframe", time=end_t, value=f"{e_scale} {e_scale}")
            p_pos = ET.SubElement(at, "param", name="position")
            ET.SubElement(p_pos, "keyframe", time="0s", value=f"{s_pos[0]} {s_pos[1]}")
            ET.SubElement(p_pos, "keyframe", time=end_t, value=f"{e_pos[0]} {e_pos[1]}")
