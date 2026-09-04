#!/usr/bin/env python3
"""階段 5→1 回饋：把人看完審閱包後寫的批註，patch 回 storyboard `<prefix>.json`。

批註檔（純文字，一行一條）：行首是鏡頭號（審閱包裡的 `#N`），後面接動作：
  12 刪
  45 換序→12                # 把鏡頭 45 移到第 12 個位置
  30 改口白：清晨的風很冷，但我們準備好了。
  30 留白                    # 清掉旁白
  88 換素材：IMG_2601.JPG
  14 加長到 4s
  22 縮到 1.5s

英文別名：del / move->N / vo: … / silent / swap: X / len 4s 都吃。

用法：
  .venv/bin/python scripts/apply_notes.py notes.txt              # 預覽
  .venv/bin/python scripts/apply_notes.py notes.txt --write      # 套用（先備份 .json）
  .venv/bin/python scripts/apply_notes.py --prefix 2026_某旅行 notes.txt --write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._config import MEDIA_DIR, PREFIX  # noqa: E402
from backend.util.media_probe import resolve_existing_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_DELETE = re.compile(r"^(刪|刪除|删|删除|delete|del|移除|拿掉|不要|去掉)\b", re.I)
_SILENT = re.compile(r"^(留白|無旁白|不要旁白|不用旁白|靜音|silent|no vo|mute)\b", re.I)
_MOVE = re.compile(r"(?:換序|换序|移到|移至|序|position|move|pos)\s*(?:->|→|到|:|：)?\s*(\d+)", re.I)
_VO = re.compile(r"(?:改口白|口白|旁白|改字幕|字幕|改旁白|vo|voiceover)\s*[:：]\s*(.+)$", re.I)
_SWAP = re.compile(r"(?:換素材|换素材|素材|改用|改素材|swap|replace)\s*[:：]\s*(\S+)", re.I)
_DUR = re.compile(r"(?:加長到|加长到|縮到|缩到|縮短到|拉長到|改成|時長|长度|長度|len|length|duration)\s*"
                  r"(\d+(?:\.\d+)?)\s*(?:s|秒|sec)?", re.I)


def parse_notes(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ln in text.splitlines():
        ln = ln.strip().lstrip("-*・•").strip()
        if not ln or ln.startswith("#") and not re.match(r"#\d", ln):
            continue
        m = re.match(r"#?\s*(\d+)[\.\)、:：]?\s+(.*)$", ln)
        if not m:
            continue
        out.append((int(m.group(1)), m.group(2).strip()))
    return out


def apply_one(shot: dict, instr: str, media_dir: Path) -> str | None:
    """就地改 shot，回傳做了什麼（None = 看不懂）。"""
    if _DELETE.match(instr):
        shot["skip"] = True
        return "刪除（skip）"
    if _SILENT.match(instr):
        shot["voiceover"] = ""
        shot.pop("timed_subtitles", None)
        return "清掉旁白"
    m = _VO.search(instr)
    if m:
        shot["voiceover"] = m.group(1).strip()
        shot.pop("timed_subtitles", None)
        return f"改口白：{m.group(1).strip()[:30]}…"
    m = _SWAP.search(instr)
    if m:
        p = resolve_existing_path(m.group(1), [str(media_dir)])
        if not p:
            return f"⚠️ 換素材找不到檔：{m.group(1)}"
        shot["file_path"] = str(p)
        shot["media_file"] = p.name
        return f"換素材 → {p.name}"
    m = _DUR.search(instr)
    if m:
        shot["duration_seconds"] = float(m.group(1))
        shot.pop("source_out", None)   # 讓時長重新生效（source_in/out 會覆蓋 duration）
        shot.pop("source_in", None)
        return f"時長 → {m.group(1)}s"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("notes", help="批註檔（純文字）")
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--write", action="store_true", help="套用並寫回 .json（先備份）")
    args = ap.parse_args()

    src = ROOT / f"{args.prefix}.json"
    if not src.exists():
        sys.exit(f"找不到 {src}")
    data = json.loads(src.read_text("utf-8"))
    sb: list[dict] = data["storyboard"]
    # 一個 shot_index 可能對到多筆（Live Photo 拆成 A 微動態 + B 定格）
    idx_entries: dict = {}
    for s in sb:
        idx_entries.setdefault(s.get("shot_index"), []).append(s)

    notes = parse_notes(Path(args.notes).read_text("utf-8"))
    if not notes:
        sys.exit("批註檔沒有可解析的行（格式：`鏡頭號 動作`）")

    moves: list[tuple[int, int]] = []   # (shot_index, 目標位置 1-indexed)
    done, bad = [], []
    for idx, instr in notes:
        entries = idx_entries.get(idx)
        if not entries:
            bad.append(f"#{idx}：storyboard 沒有這個 shot_index")
            continue
        mv = _MOVE.search(instr)
        if mv and not _VO.search(instr):
            moves.append((idx, int(mv.group(1))))
            done.append(f"#{idx}：換序 → 第 {mv.group(1)} 個")
            continue
        # 刪除 / 時長 → 套到該 shot_index 的每一筆（含 Live Photo A/B）；其餘套最後一筆（通常是 B 定格）
        targets = entries if (_DELETE.match(instr) or _DUR.search(instr)) else entries[-1:]
        rs = [apply_one(t, instr, MEDIA_DIR) for t in targets]
        r = next((x for x in rs if x), None)
        (done if r and not r.startswith("⚠️") else bad).append(
            f"#{idx}：{r}" if r else f"#{idx}：看不懂「{instr}」")

    # 換序最後做（依批註順序）
    for idx, target in moves:
        cur = next((i for i, s in enumerate(sb) if s.get("shot_index") == idx), None)
        if cur is None:
            continue
        s = sb.pop(cur)
        sb.insert(max(0, min(len(sb), target - 1)), s)

    print(f"批註 {len(notes)} 條：\n" + "\n".join("  ✓ " + d for d in done))
    if bad:
        print("\n沒套用：\n" + "\n".join("  ✗ " + b for b in bad))

    if not args.write:
        print("\n（預覽。加 --write 套用）")
        return

    bak = src.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
    bak.write_text(json.dumps(json.loads(src.read_text('utf-8')), ensure_ascii=False, indent=1), "utf-8")
    data["storyboard"] = sb
    src.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n✅ 已寫回 {src.name}（備份 {bak.name}）"
          f"\n   下一步：rebuild_all_projects.py（要重寫旁白配合新剪輯再加 --regen-vo）")


if __name__ == "__main__":
    main()
