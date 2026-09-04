from typing import Dict, Any, List, Optional

from ..engines.subtitle_engine import (
    SubtitleEngine,
    normalize_srt_cues,
    MAX_CHARS_PER_LINE,
)


class SRTExporter:
    """SRT 獨立字幕匯出器 (相容 DaVinci Resolve / Premiere / Final Cut / VLC)。

    時間軸依 storyboard 逐鏡頭累加；每則字幕的顯示秒數由閱讀速度決定，
    最後統一做正規化 (排序、去重疊、補間隔、套用最短/最長秒數)。
    """

    def export(self, script_data: Dict[str, Any], output_path: Optional[str] = None,
               search_dirs: Optional[List[str]] = None) -> str:
        from .timeline_layout import timeline_layout

        from ..engines.subtitle_engine import OVERFLOW_TAIL_SEC

        flat: List[Dict[str, Any]] = []

        # 用與 FCPXML 完全相同的時間軸佈局（晃動剪除、影片夾長、影格對齊、skip 都一致）
        layout = timeline_layout(script_data, search_dirs)
        timeline_end = (layout[-1]["start"] + layout[-1]["dur"]) if layout else None
        from ..engines.subtitle_engine import READING_CPS, MIN_READABLE_SEC, MAX_CUE_SEC

        for entry in layout:
            shot = entry["shot"]
            shot_start, dur = entry["start"], entry["dur"]
            kind = shot.get("voiceover_kind", "quote")

            # 畫布鏡頭的「感觸」：多句、各自帶鏡頭內的時間點 t，慢慢浮現
            refl = shot.get("reflection_cues") or []
            if refl:
                for rc in refl:
                    text = (rc.get("text") or "").strip()
                    if not text:
                        continue
                    s = shot_start + float(rc.get("t", 0.0))
                    d = max(MIN_READABLE_SEC + 0.6,
                            min(MAX_CUE_SEC, len(text.replace("\n", "")) / (READING_CPS * 0.85)))
                    flat.append({"start": s, "end": s + d,
                                 "text": self._wrap(text), "kind": "reflection"})
                continue

            timed_subs = shot.get("timed_subtitles") or []
            vo_text = (shot.get("voiceover") or "").strip()
            if not timed_subs and vo_text and vo_text not in ("null", "無", "(純畫面與環境音)"):
                timed_subs = SubtitleEngine.generate_timed_subtitles(vo_text, dur)

            # 字幕最多溢到後面 ~8s（跨幾顆 B-roll），再長就砍尾（旁白該在腳本階段裁）
            hard_end = shot_start + dur + max(OVERFLOW_TAIL_SEC, 8.0)
            for sub in timed_subs:
                text = (sub.get("text") or sub.get("raw_text") or "").strip()
                if not text or text in ("null", "無", "(純畫面與環境音)"):
                    continue
                s = shot_start + float(sub.get("start_offset", 0.0))
                if s >= hard_end:
                    break
                e = min(hard_end, s + float(sub.get("duration", 2.0)))
                flat.append({"start": s, "end": e, "text": self._wrap(text), "kind": kind})

        cues = normalize_srt_cues(flat, timeline_end=timeline_end)
        dropped = getattr(normalize_srt_cues, "last_dropped", 0)
        compressed = getattr(normalize_srt_cues, "last_compressed", 0)
        if dropped or compressed:
            print(f"[SRT] ⚠️ 旁白比片長長：{compressed} 則被壓到最短秒數、{dropped} 則被丟棄 — "
                  f"建議用 --regen-vo 依剪好的時間軸重寫旁白，或人工精簡")

        from ..util.subtitle_preset import STYLE as _S
        narr = "#" + _S.get("narration_fill", "F0DFA8")
        refl = "#" + _S.get("reflection_fill", "E8C88C")

        # kind → 顏色標記（libass 讀 SRT 的 <font color>；make_ass 再依顏色套版位/字級）
        #   annotation / bridge → 白（不標）
        #   narration（舊）      → 暖金
        #   reflection          → 琥珀（+ 置中放大，在 make_ass 處理）
        blocks = []
        for i, c in enumerate(cues, start=1):
            txt = c["text"]
            k = c.get("kind")
            if k == "narration":
                txt = f'<font color="{narr}">{txt}</font>'
            elif k == "reflection":
                txt = f'<font color="{refl}">{txt}</font>'
            blocks.append(
                f"{i}\n{self._ts(c['start'])} --> {self._ts(c['end'])}\n{txt}\n"
            )
        srt_content = "\n".join(blocks)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
        return srt_content

    def _wrap(self, text: str) -> str:
        return SubtitleEngine.wrap_lines(text, max_chars_per_line=MAX_CHARS_PER_LINE)

    @staticmethod
    def _ts(seconds: float) -> str:
        if seconds < 0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000))
        h, rem = divmod(total_ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
