"""Highlight 篩選引擎：給每個鏡頭算精華分數，決定進不進片。

設計見 `.claude/skills/film-edit/references/highlight-scoring.md`。

訊號（全部可缺，缺了就當 0）：
  + 旁白 (voiceover)         → 敘事錨點，強制保留
  + 相簿 favorite / keywords / Apple 影像分數  → 來自 photos_meta 快取（見 scripts/dump_photos_metadata.py）
  + 是影片 / Live Photo、影片夠長
  - 晃動比例 (需 analyze_shake=True，會跑 ffmpeg)
  - 與前一個已保留鏡頭的畫面重複（aHash，只比對照片）
  + 每個「日 / 篇章」的第一個鏡頭

模式：
  narrative  低門檻，只剔除明顯重複 / 沒旁白又超晃的
  highlight  取 top-N，再強制補回每組最佳 + 開場 + 結尾 + 所有旁白鏡頭
  montage    關掉重複與晃動扣分，靠廣度取鏡
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util.media_probe import resolve_existing_path, is_video_path

_DROP_VO = ("", "null", "無", "(純畫面與環境音)", "[留白，專注於現場環境音與配樂]")


# ── 訊號輔助 ────────────────────────────────────────────────────────────────
def _has_vo(shot: Dict[str, Any]) -> bool:
    return (shot.get("voiceover") or "").strip() not in _DROP_VO


def _group_key(shot: Dict[str, Any]) -> str:
    """從 scene_title 取「Day N」/「第X篇」當分組鍵，抓不到就用整個 scene_title。"""
    t = str(shot.get("scene_title") or "")
    m = re.search(r"(Day\s*\d+|第[一二三四五六七八九十\d]+篇|Day\d+)", t)
    if m:
        return m.group(1).replace(" ", "")
    return t.split("：")[0].split(":")[0].strip() or "_"


def _ahash(path: str, size: int = 8) -> Optional[int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("L").resize((size, size), Image.Resampling.BILINEAR)
            px = list(im.tobytes())
        avg = sum(px) / len(px)
        bits = 0
        for i, p in enumerate(px):
            if p >= avg:
                bits |= 1 << i
        return bits
    except Exception:
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _load_photos_meta(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    from ..util.photos_meta import load_photos_meta
    return load_photos_meta(path)


# ── 主流程 ─────────────────────────────────────────────────────────────────
def score_storyboard(
    storyboard: List[Dict[str, Any]],
    *,
    search_dirs: Optional[List[str]] = None,
    photos_meta_path: Optional[str] = None,
    analyze_shake: bool = False,
    shake_threshold: float = 3.2,
) -> None:
    """就地在每個 shot 寫入 highlight_score / _hl (訊號明細)。不做保留判斷。"""
    meta = _load_photos_meta(photos_meta_path)
    kept_hashes: List[int] = []
    seen_groups: set = set()

    for i, shot in enumerate(storyboard):
        raw = shot.get("file_path") or shot.get("media_file") or ""
        resolved = resolve_existing_path(str(raw), search_dirs)
        path = str(resolved) if resolved else str(raw)
        stem = Path(path).stem.lower()
        m = meta.get(stem, {})

        sig: Dict[str, float] = {}

        if m.get("exclude"):
            shot["_hl"] = {"exclude": -99.0}
            shot["highlight_score"] = -99.0
            continue
        if _has_vo(shot):
            sig["voiceover"] = 2.0
        if m.get("favorite"):
            sig["favorite"] = 1.5
        if m.get("keywords"):
            sig["keywords"] = 0.8
        faces = m.get("faces") or []
        if m.get("persons") or faces or m.get("has_people"):
            sig["persons"] = 1.0                       # 畫面有人 → 敘事價值高
            if len({f.get("name") for f in faces if f.get("name")}) >= 2 or len(faces) >= 2:
                sig["group_shot"] = 0.6                # 多人同框 = 情感重點
        if m.get("edited"):
            sig["edited"] = 0.5                        # 使用者自己修過圖 = 在意
        if m.get("kind", {}).get("screenshot"):
            sig["screenshot"] = -2.0
        # Apple ML 分數：優先用 curation / promotion (「這是精華嗎」的專用分)
        det = m.get("score_detail") or {}
        apple = det.get("curation")
        if apple is None:
            apple = det.get("promotion")
        if apple is None:
            apple = m.get("score")
        if apple is not None:
            sig["apple_score"] = round(1.5 * max(0.0, min(1.0, (float(apple) + 1.0) / 2.0)), 3)

        is_vid = is_video_path(path)
        if is_vid or shot.get("is_live_photo") or shot.get("media_type") in ("video", "live_photo"):
            sig["motion_media"] = 1.0

        grp = _group_key(shot)
        if grp not in seen_groups:
            sig["new_group"] = 0.6
            seen_groups.add(grp)
        if i == 0 or i == len(storyboard) - 1:
            sig["bookend"] = 1.0

        # 晃動扣分（可選，慢）
        if analyze_shake and is_vid and resolved:
            try:
                from ..analyzers.motion_stability import analyze_video

                rep = analyze_video(path, shake_threshold=shake_threshold)
                if rep.verdict == "drop":
                    sig["shake"] = -2.0
                elif rep.shaky_fraction:
                    sig["shake"] = round(-1.5 * float(rep.shaky_fraction), 3)
            except Exception:
                pass

        # 畫面重複（只比對得到的照片）
        if not is_vid and resolved:
            h = _ahash(path)
            if h is not None:
                if any(_hamming(h, k) <= 6 for k in kept_hashes):
                    sig["duplicate"] = -1.2
                else:
                    kept_hashes.append(h)

        shot["_hl"] = sig
        shot["highlight_score"] = round(sum(sig.values()), 3)


def select_highlights(
    storyboard: List[Dict[str, Any]],
    *,
    mode: str = "narrative",
    target_count: Optional[int] = None,
    search_dirs: Optional[List[str]] = None,
    photos_meta_path: Optional[str] = None,
    analyze_shake: bool = False,
    shake_threshold: float = 3.2,
) -> List[str]:
    """算分 + 就地標記每個 shot 的 keep(bool) / keep_reason。

    回傳人類可讀的摘要行 list（給 CLI 印）。被剔除的 shot 會設 skip=True。
    """
    if not storyboard:
        return []

    score_storyboard(
        storyboard, search_dirs=search_dirs, photos_meta_path=photos_meta_path,
        analyze_shake=analyze_shake if mode != "montage" else False,
        shake_threshold=shake_threshold,
    )

    n = len(storyboard)
    idx_by_group: Dict[str, List[int]] = {}
    for i, s in enumerate(storyboard):
        idx_by_group.setdefault(_group_key(s), []).append(i)

    keep = [False] * n
    reason = [""] * n
    hard_drop = [bool(s.get("_hl", {}).get("exclude")) for s in storyboard]

    def force(i: int, why: str) -> None:
        if not keep[i] and not hard_drop[i]:
            keep[i], reason[i] = True, why

    for i in range(n):
        if hard_drop[i]:
            reason[i] = "剔除：相簿隱藏 / 已刪除"

    # 開場 / 結尾一律保留
    force(0, "開場")
    force(n - 1, "結尾")

    # 片頭 / 片尾獨立段落（build_bookends.py 打的 segment tag）一律保留、不參與去重
    for i, s in enumerate(storyboard):
        if s.get("segment") and s.get("segment") != "body":
            force(i, f"片頭片尾：{s['segment']}")

    if mode == "narrative":
        # 旁白鏡頭全保留；其餘低門檻，只剔除重複 / 明顯超晃
        for i, s in enumerate(storyboard):
            if _has_vo(s):
                force(i, "旁白錨點")
        for g, idxs in idx_by_group.items():
            force(max(idxs, key=lambda k: storyboard[k]["highlight_score"]), f"{g} 代表鏡頭")
        for i, s in enumerate(storyboard):
            if keep[i]:
                continue
            sig = s.get("_hl", {})
            if s["highlight_score"] <= -0.5 and ("duplicate" in sig or sig.get("shake", 0) <= -1.0):
                reason[i] = "剔除：畫面重複 / 晃動過大"
            else:
                force(i, "保留")
    elif mode in ("curate", "llm-curate"):
        from .curator_engine import CuratorEngine
        tgt = target_count or max(1, round(n / 3))
        curator = CuratorEngine()
        curated_res = curator.curate_shots(storyboard, target_count=tgt)
        curated_idxs = set(curated_res.get("curated_indices", []))
        roles = curated_res.get("shot_roles", {})
        for idx in curated_idxs:
            if 1 <= idx <= n:
                role_info = roles.get(str(idx), {})
                r_desc = role_info.get("reason") or role_info.get("role") or "故事主線"
                force(idx - 1, f"LLM故事精選：{r_desc}")
        for i in range(n):
            if not keep[i] and not reason[i]:
                reason[i] = "剔除：未入選故事主線"
    elif mode == "montage":
        for i, s in enumerate(storyboard):
            if keep[i]:
                continue
            if "duplicate" in s.get("_hl", {}):
                reason[i] = "剔除：畫面重複"
            elif s["highlight_score"] > -1.0:
                force(i, "蒙太奇廣度取鏡")
            else:
                reason[i] = "剔除：分數過低"
    else:  # highlight — 真的要砍到目標數
        tgt = target_count or max(1, round(n / 3))
        # 每組先保底：分數最高的 min(2, 組內數) 個
        for g, idxs in idx_by_group.items():
            for k in sorted(idxs, key=lambda k: storyboard[k]["highlight_score"], reverse=True)[:2]:
                force(k, f"{g} 保底")
        # 其餘名額按全域分數補到 tgt
        for i in sorted(range(n), key=lambda k: storyboard[k]["highlight_score"], reverse=True):
            if sum(keep) >= tgt:
                break
            force(i, "分數 top-N")
        for i in range(n):
            if not keep[i] and not reason[i]:
                reason[i] = "剔除：分數不足"

    summary: List[str] = []
    dropped = 0
    for i, s in enumerate(storyboard):
        s["keep"] = keep[i]
        s["keep_reason"] = reason[i]
        if not keep[i]:
            s["skip"] = True
            dropped += 1
    summary.append(f"精華篩選 [{mode}]：保留 {sum(keep)} / {n}，剔除 {dropped}")
    lo = sorted(storyboard, key=lambda x: x["highlight_score"])[:3]
    hi = sorted(storyboard, key=lambda x: x["highlight_score"], reverse=True)[:3]
    summary.append("  最高：" + "、".join(f"{_group_key(s)}({s['highlight_score']})" for s in hi))
    summary.append("  最低：" + "、".join(f"{s.get('media_file','?')}({s['highlight_score']})" for s in lo))
    return summary
