#!/usr/bin/env python3
"""本地視覺模型產生「場景標籤」→ 併進 scripts/photos_meta.json。

gemini_scene_labels.py 的離線/免額度替代（Gemini 每日上限撞牆時用）。
介面刻意做得跟它一樣：同一份 photos_meta.json、同樣只補沒有 labels 的、
同樣可中斷續跑、詞彙對齊 grading_engine._PRESETS。

backend：
  openclip  （預設）零樣本比對詞表 → 場景標籤 + mood + time_of_day + has_people
  yolo      YOLOv8 物件偵測 → 硬標籤（person/bicycle/vehicle/dog…）+ 準確的 has_people
  both      跑兩個，標籤取聯集，has_people 以 yolo 為準

用法：
  .venv/bin/python scripts/local_scene_labels.py                       # openclip 掃預設素材夾
  .venv/bin/python scripts/local_scene_labels.py --backend both --limit 50
  .venv/bin/python scripts/local_scene_labels.py --force -i /path/folder
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.util.media_probe import IMAGE_EXTS, VIDEO_EXTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "scripts" / "photos_meta.json"
from scripts._config import MEDIA_DIR  # noqa: E402
DEFAULT_FOLDER = str(MEDIA_DIR)

# 與 gemini_scene_labels.VOCAB 對齊（grading_engine preset + abroll/highlight 用得到的）
SCENE_VOCAB = [
    "sunrise", "sunset", "dusk", "dawn", "golden hour", "daylight", "night",
    "beach", "ocean", "sea", "coast", "wave", "lake", "river", "reflection", "waterfall", "harbor",
    "forest", "tree", "grass", "field", "rice", "mountain", "hill", "valley", "cliff",
    "road", "highway", "bridge", "tunnel",
    "urban", "street", "building", "architecture", "sign", "temple", "shrine", "market",
    "food", "meal", "drink", "dessert",
    "snow", "fog", "mist", "cloud", "rain",
    "bicycle", "vehicle", "train",
    "portrait", "selfie", "group photo", "people", "crowd", "animal", "dog",
    "indoor", "vehicle interior", "sky", "panorama",
]
MOOD_VOCAB = ["溫馨", "壯闊", "寧靜", "歡樂", "疲憊", "緊張", "孤獨", "希望"]
MOOD_EN = ["warm and tender", "epic and vast", "calm and quiet", "joyful and lively",
           "tired and worn", "tense", "lonely", "hopeful"]
TOD_VOCAB = ["dawn", "morning", "noon", "afternoon", "golden hour", "dusk", "night"]

# YOLO(COCO) class → 我們的詞彙
YOLO_MAP = {
    "person": "people", "bicycle": "bicycle", "car": "vehicle", "motorcycle": "vehicle",
    "bus": "vehicle", "truck": "vehicle", "train": "train", "boat": "vehicle",
    "dog": "dog", "cat": "animal", "horse": "animal", "cow": "animal", "sheep": "animal",
    "bird": "animal", "backpack": "bicycle", "umbrella": "rain",
    "bottle": "drink", "cup": "drink", "bowl": "food", "sandwich": "food",
    "pizza": "food", "cake": "dessert", "donut": "dessert",
    "traffic light": "street", "stop sign": "sign",
}


def load_frame(path: Path):
    """回傳 PIL.Image（影片抽中間幀）。失敗回 None。"""
    from PIL import Image, ImageOps

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    if path.suffix.lower() in IMAGE_EXTS:
        try:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1024, 1024))
            return im
        except Exception:
            return None
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip() or 2.0)
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.1, dur / 2):.2f}",
             "-i", str(path), "-frames:v", "1", "-vf", "scale=1024:-2",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=40).stdout
        if not raw:
            return None
        import io
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


class OpenCLIPBackend:
    def __init__(self) -> None:
        import open_clip
        import torch

        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32-quickgelu", pretrained="openai")
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer("ViT-B-32-quickgelu")
        self._cache: dict[tuple, "torch.Tensor"] = {}

    def _text_feats(self, prompts: tuple[str, ...]):
        if prompts not in self._cache:
            with self.torch.no_grad():
                tok = self.tokenizer(list(prompts))
                f = self.model.encode_text(tok)
                f /= f.norm(dim=-1, keepdim=True)
            self._cache[prompts] = f
        return self._cache[prompts]

    def _img_feat(self, im):
        with self.torch.no_grad():
            x = self.preprocess(im).unsqueeze(0)
            f = self.model.encode_image(x)
            f /= f.norm(dim=-1, keepdim=True)
        return f

    def analyze(self, im) -> dict:
        imf = self._img_feat(im)

        scene_prompts = tuple(f"a photo of {w}" for w in SCENE_VOCAB)
        sims = (imf @ self._text_feats(scene_prompts).T).squeeze(0)
        probs = sims.softmax(dim=-1)
        order = probs.argsort(descending=True)
        labels = []
        for i in order[:6].tolist():
            if probs[i].item() > 0.04 or len(labels) < 3:
                labels.append(SCENE_VOCAB[i])
            if len(labels) >= 5:
                break

        mood_prompts = tuple(f"a {m} scene" for m in MOOD_EN)
        mi = int((imf @ self._text_feats(mood_prompts).T).squeeze(0).argmax())
        mood = MOOD_VOCAB[mi]

        tod_prompts = tuple(f"a photo taken at {t}" for t in TOD_VOCAB)
        ti = int((imf @ self._text_feats(tod_prompts).T).squeeze(0).argmax())
        tod = TOD_VOCAB[ti]

        ppl_prompts = ("a photo with people in it", "a photo with no people, only scenery")
        has_people = bool((imf @ self._text_feats(ppl_prompts).T).squeeze(0).argmax().item() == 0)

        return {"labels": labels, "mood": mood, "time_of_day": tod, "has_people": has_people}


class YOLOBackend:
    def __init__(self) -> None:
        from ultralytics import YOLO
        wdir = ROOT / "scripts" / ".weights"
        wdir.mkdir(exist_ok=True)
        self.model = YOLO(str(wdir / "yolov8n.pt"))

    def analyze(self, im) -> dict:
        res = self.model.predict(im, verbose=False)[0]
        names = res.names
        counts: dict[str, int] = {}
        for c in res.boxes.cls.tolist():
            counts[names[int(c)]] = counts.get(names[int(c)], 0) + 1
        labels = sorted({YOLO_MAP[k] for k in counts if k in YOLO_MAP})
        return {"labels": labels, "has_people": counts.get("person", 0) > 0,
                "_person_count": counts.get("person", 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", default=DEFAULT_FOLDER)
    ap.add_argument("--meta", default=str(META))
    ap.add_argument("--backend", choices=["openclip", "yolo", "both"], default="openclip")
    ap.add_argument("--limit", type=int, default=10000)
    ap.add_argument("--force", action="store_true", help="連已有 labels 的也重跑")
    args = ap.parse_args()

    meta_path = Path(args.meta)
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}

    files = sorted(f for f in Path(args.input).iterdir()
                   if f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS) and not f.name.startswith("."))
    seen: set[str] = set()
    todo: list[Path] = []
    for f in files:
        st = f.stem.lower()
        if st in seen:
            continue
        seen.add(st)
        if meta.get(st, {}).get("labels") and not args.force:
            continue
        todo.append(f)

    print(f"backend={args.backend}｜待分析 {len(todo)} 張（上限 {args.limit}）")
    print("載入模型…（首次會下載權重）")
    oc = OpenCLIPBackend() if args.backend in ("openclip", "both") else None
    yo = YOLOBackend() if args.backend in ("yolo", "both") else None

    done = 0
    for f in todo[:args.limit]:
        st = f.stem.lower()
        im = load_frame(f)
        if im is None:
            print(f"  · 跳過 {f.name}（抽幀失敗）")
            continue

        labels: list[str] = []
        rec_out: dict = {}
        if oc:
            r = oc.analyze(im)
            labels += r["labels"]
            rec_out.update(mood=r["mood"], has_people=r["has_people"])
            if r["time_of_day"] in ("golden hour", "dusk", "dawn"):
                labels.append("golden hour")
            if r["time_of_day"] == "night":
                labels.append("night")
        if yo:
            r = yo.analyze(im)
            labels += r["labels"]
            rec_out["has_people"] = r["has_people"]  # yolo 的 has_people 較準

        rec = meta.setdefault(st, {})
        rec["labels"] = sorted(set(x.lower().strip() for x in labels if x))
        rec.setdefault("activities", [])
        for k, v in rec_out.items():
            rec[k] = v
        rec["labels_source"] = args.backend
        done += 1
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        print(f"  ✓ {f.name}: {rec['labels']} | {rec.get('mood','')} | 人={rec.get('has_people')}")

    n_lab = sum(1 for v in meta.values() if v.get("labels"))
    print(f"\n完成本輪 {done} 張。photos_meta.json 目前 {n_lab} 張有場景標籤。")


if __name__ == "__main__":
    main()
