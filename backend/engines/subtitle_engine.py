import re
from typing import List, Dict, Any

# ── 字幕設計規格（可調：repo 根的 subtitle_preset.json，見 backend/util/subtitle_preset.py）──
from ..util.subtitle_preset import TIMING as _T   # noqa: E402

READING_CPS = _T["reading_cps"]          # 每秒可讀字數 (用於推算顯示秒數)
MIN_CUE_SEC = _T["min_cue_sec"]          # 單則字幕最短顯示秒數 (排版下限)
MIN_READABLE_SEC = _T["min_readable_sec"]  # 實際看得懂的最短秒數 — 壓到比這短就寧可少一則
MAX_CUE_SEC = _T["max_cue_sec"]          # 單則字幕最長顯示秒數
OVERFLOW_TAIL_SEC = _T["overflow_tail_sec"]  # 字幕可延伸到鏡頭結束後多久
CUE_GAP_SEC = _T["cue_gap_sec"]          # 兩則字幕之間的最小間隔 (避免黏連閃爍)
MAX_CHARS_PER_LINE = _T["max_chars_per_line"]  # 每行最大字數 (16:9)；9:16 直式請用 12
MAX_LINES = _T["max_lines"]              # 每則最多行數
MAX_CHARS_PER_CUE = MAX_CHARS_PER_LINE * MAX_LINES   # 每則字幕硬上限 (32)

# 斷句標點優先序
_SENTENCE_ENDERS = "。！？!?…"
_CLAUSE_SEPS = "，、；：,;:—"
_TRAILING_PUNCT = "，、；：。！？!?…,;: "


def _clean(text: str) -> str:
    return (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")


def _char_count(s: str) -> int:
    """全形字算 1，用於排版寬度估計 (中文情境足夠)。"""
    return len(s.strip(_TRAILING_PUNCT)) or len(s.strip())


class SubtitleEngine:
    """智慧字幕切分／排版／分時排程引擎 (依閱讀速度而非固定段數)。

    設計原則：
    1. 依「句 → 子句 → 硬切」逐級切分，讓每則字幕語意完整、不尷尬斷詞。
    2. 每則字數 <= 32 (16 字 x 2 行)；顯示秒數 = clamp(字數/6, 1.2s, 6.0s)。
    3. 不再限制字幕段數 — 20 秒旁白該切 8~10 則就切幾則。
    4. 依序排在鏡頭時間軸上；若旁白太長超出鏡頭，字幕會自然延續 (由匯出器處理)。
    """

    # ── 對外相容介面 ──────────────────────────────────────────────────────
    @classmethod
    def wrap_lines(cls, text: str, max_chars_per_line: int = MAX_CHARS_PER_LINE) -> str:
        """把一則字幕排成最多 2 行。"""
        text = _clean(text)
        if "\n" in text:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            return "\n".join(lines[:MAX_LINES])
        if _char_count(text) <= max_chars_per_line:
            return text

        # 依標點找最接近中點的斷行位置
        parts = re.split(r'([%s])' % re.escape(_CLAUSE_SEPS + _SENTENCE_ENDERS), text)
        chunks: List[str] = []
        buf = ""
        for seg in parts:
            buf += seg
            if seg and seg in (_CLAUSE_SEPS + _SENTENCE_ENDERS):
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)

        if len(chunks) >= 2:
            target = len(text) / 2
            acc, best_i, best_diff = 0, 1, 1e9
            for i, c in enumerate(chunks[:-1]):
                acc += len(c)
                d = abs(acc - target)
                if d < best_diff:
                    best_diff, best_i = d, i + 1
            line1 = "".join(chunks[:best_i]).strip()
            line2 = "".join(chunks[best_i:]).strip()
            return f"{line1}\n{line2}"

        mid = len(text) // 2
        return f"{text[:mid]}\n{text[mid:]}"

    @classmethod
    def split_into_segments(cls, text: str, duration_sec: float = 0.0) -> List[str]:
        """把旁白切成多則字幕文字 (每則 <= 32 字，語意完整)。"""
        text = _clean(text)
        if not text or text in ("null", "無", "(純畫面與環境音)"):
            return []

        # 1) 先切成「句」
        sentences = cls._split_keep(text, _SENTENCE_ENDERS)
        # 2) 過長的句再切成「子句」
        clauses: List[str] = []
        for s in sentences:
            if _char_count(s) <= MAX_CHARS_PER_CUE:
                clauses.append(s)
            else:
                clauses.extend(cls._split_keep(s, _CLAUSE_SEPS))
        # 3) 仍過長 → 硬切
        units: List[str] = []
        for c in clauses:
            if _char_count(c) <= MAX_CHARS_PER_CUE:
                units.append(c)
            else:
                units.extend(cls._hard_wrap(c, MAX_CHARS_PER_CUE))

        # 4) 貪婪合併相鄰短單元，直到接近 (但不超過) 每則上限
        cues: List[str] = []
        cur = ""
        for u in units:
            u = u.strip()
            if not u:
                continue
            if not cur:
                cur = u
            elif _char_count(cur) + _char_count(u) <= MAX_CHARS_PER_CUE:
                cur += u
            else:
                cues.append(cur)
                cur = u
        if cur:
            cues.append(cur)
        return [c.strip() for c in cues if c.strip()]

    @classmethod
    def generate_timed_subtitles(cls, text: str, duration_sec: float) -> List[Dict[str, Any]]:
        """為單一鏡頭產生分時、已排版的字幕清單。

        回傳: [{segment_index, start_offset, duration, raw_text, text}, ...]
        start_offset / duration 皆相對於鏡頭起點 (秒)。
        """
        text = _clean(text)
        if not text or text in ("null", "無", "(純畫面與環境音)"):
            return []

        cues = cls.split_into_segments(text, duration_sec)
        if not cues:
            return []

        pad_start = min(0.15, max(0.0, duration_sec * 0.03))

        # 每則一律用「看得懂」的秒數 (>= MIN_READABLE_SEC，絕不壓到閃一下)。
        # 若旁白比鏡頭長，字幕會自然溢到後面幾顆畫面 (紀錄片常態)；
        # 真正過長的旁白應在 stabilize / 腳本階段就裁短，不是在這裡硬壓。
        subs: List[Dict[str, Any]] = []
        cursor = pad_start
        for cue in cues:
            d = max(MIN_READABLE_SEC, min(MAX_CUE_SEC, _char_count(cue) / READING_CPS))
            subs.append({
                "segment_index": len(subs) + 1,
                "start_offset": round(cursor, 3),
                "duration": round(d, 3),
                "raw_text": cue,
                "text": cls.wrap_lines(cue),
            })
            cursor += d + CUE_GAP_SEC
        return subs

    # ── 內部工具 ─────────────────────────────────────────────────────────
    @staticmethod
    def _split_keep(text: str, seps: str) -> List[str]:
        """依 seps 切分，保留標點在前段結尾。"""
        pattern = "([%s])" % re.escape(seps)
        parts = re.split(pattern, text)
        out: List[str] = []
        buf = ""
        for seg in parts:
            if seg == "":
                continue
            if len(seg) == 1 and seg in seps:
                buf += seg
                out.append(buf.strip())
                buf = ""
            else:
                buf += seg
        if buf.strip():
            out.append(buf.strip())
        return out or [text]

    @staticmethod
    def _hard_wrap(text: str, size: int) -> List[str]:
        return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def normalize_srt_cues(
    cues: List[Dict[str, Any]],
    timeline_end: float | None = None,
    tail_sec: float = OVERFLOW_TAIL_SEC,
) -> List[Dict[str, Any]]:
    """對整條時間軸上的字幕做最終正規化：

    每則至少顯示 MIN_READABLE_SEC 秒、依序不重疊。重疊時把後一則往後推
    （旁白是連續的一條河，不是綁死每顆鏡頭）；但推遲量設上限，超過就丟掉積壓的，
    避免像以前一路飄掉 180 秒。前提是旁白總量已在 stabilize / 腳本階段裁到接近片長。

    `timeline_end`：成片總長（秒）。給了就把字幕硬夾在 `timeline_end + tail_sec` 之內
    ——晚於此就丟、跨越此就把結尾切齊。沒給則不設上限（舊行為）。
    被丟掉的則數會記在 `normalize_srt_cues.last_dropped`（旁白過長的訊號）。

    輸入/輸出: [{"start": float, "end": float, "text": str}, ...]
    """
    MAX_DRIFT = 8.0
    ceiling = (timeline_end + tail_sec) if timeline_end is not None else None
    cues = sorted((c for c in cues if c.get("text", "").strip()), key=lambda c: c["start"])
    out: List[Dict[str, Any]] = []
    dropped = 0
    compressed = 0
    for i, c in enumerate(cues):
        want = max(0.0, float(c["start"]))
        text = c["text"].strip()
        ideal = max(MIN_READABLE_SEC, min(MAX_CUE_SEC, _char_count(text.replace("\n", "")) / READING_CPS))
        start = want
        if out:
            floor = out[-1]["end"] + CUE_GAP_SEC
            if start < floor:
                start = floor
        # 沒有片長上限 → 靠 MAX_DRIFT 擋住旁白一路飄掉。
        # 有片長上限 → 下面「為後面保留時間 + 壓縮 + 沒空間才 break」是更精準的防線，
        # 這裡不再用 drift 丟，讓連續旁白像一條河順順排下去、盡量少丟。
        if ceiling is None and start - want > MAX_DRIFT:
            dropped += 1
            continue

        d = ideal
        if ceiling is not None:
            # 為後面每一則保留至少 MIN_CUE_SEC + 間隔，剩下的才是這則能用的時間
            rest = len(cues) - i - 1
            reserved = rest * (MIN_CUE_SEC + CUE_GAP_SEC)
            room = ceiling - start - reserved
            if room < MIN_CUE_SEC:          # 這則連壓縮都塞不下 → 丟這則，後面的若 want 追上來還有機會
                dropped += 1
                continue
            if d > room:                   # 接近片尾 → 壓縮（下限 MIN_CUE_SEC），不整段消失
                d = max(MIN_CUE_SEC, room)
                compressed += 1

        out.append({"start": round(start, 3), "end": round(start + d, 3), "text": text,
                    "kind": c.get("kind", "quote")})
    normalize_srt_cues.last_dropped = dropped        # type: ignore[attr-defined]
    normalize_srt_cues.last_compressed = compressed  # type: ignore[attr-defined]
    return out


normalize_srt_cues.last_dropped = 0     # type: ignore[attr-defined]
normalize_srt_cues.last_compressed = 0  # type: ignore[attr-defined]
