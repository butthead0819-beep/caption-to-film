"""從「資料夾內的檔案本身」讀 metadata → 寫成 photos_meta.json。

不開 Apple Photos 資料庫（大相簿要開好幾分鐘）。只讀檔案內嵌的：
  - EXIF：GPS 經緯度 / 海拔、DateTimeOriginal、相機、焦距、ISO
  - XMP  ：mwg-rs 人臉框 + 人名、dc:subject 關鍵字、xmp:Rating
  - 啟發式：panorama（長寬比 > 2）、screenshot（無 EXIF 相機資訊 + 常見尺寸）

拿不到的（需 Apple Photos DB，用 scripts/dump_photos_metadata.py 另外補）：
  Apple ML 場景標籤 labels、美學分數 score、favorite 旗標

輸出 key = 檔名 stem（小寫），格式與 dump_photos_metadata.py 相容（可互相 merge）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
_VID = {".mov", ".mp4", ".m4v"}

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}


def _dms_to_deg(dms, ref) -> Optional[float]:
    try:
        d = dms[0][0] / dms[0][1]
        m = dms[1][0] / dms[1][1]
        s = dms[2][0] / dms[2][1]
        val = d + m / 60.0 + s / 3600.0
        if ref in (b"S", b"W", "S", "W"):
            val = -val
        return round(val, 6)
    except Exception:
        return None


def _exif(path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import piexif

        ex = piexif.load(path)
        gps = ex.get("GPS", {})
        lat = _dms_to_deg(gps.get(piexif.GPSIFD.GPSLatitude), gps.get(piexif.GPSIFD.GPSLatitudeRef))
        lon = _dms_to_deg(gps.get(piexif.GPSIFD.GPSLongitude), gps.get(piexif.GPSIFD.GPSLongitudeRef))
        if lat is not None and lon is not None:
            alt = None
            a = gps.get(piexif.GPSIFD.GPSAltitude)
            if a:
                try:
                    alt = round(a[0] / a[1], 1)
                    if gps.get(piexif.GPSIFD.GPSAltitudeRef) in (1, b"\x01"):
                        alt = -alt
                except Exception:
                    pass
            out["gps"] = {"lat": lat, "lon": lon, "alt": alt}

        exif_ifd = ex.get("Exif", {})
        dto = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
        if dto:
            try:
                out["taken"] = datetime.strptime(dto.decode(), "%Y:%m:%d %H:%M:%S").isoformat()
            except Exception:
                pass
        zeroth = ex.get("0th", {})
        make = zeroth.get(piexif.ImageIFD.Make)
        model = zeroth.get(piexif.ImageIFD.Model)
        out["_has_camera"] = bool(make or model)
        fl = exif_ifd.get(piexif.ExifIFD.FocalLengthIn35mmFilm)
        if fl:
            out["focal35"] = int(fl)
    except Exception:
        pass
    return out


def _xmp_faces(xmp: str, W: int, H: int) -> List[Dict[str, Any]]:
    faces: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xmp.encode("utf-8", "ignore"))
    except Exception:
        # XMP 常帶前綴/多個 packet，抓第一個 <x:xmpmeta>..</x:xmpmeta>
        m = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", xmp, re.S)
        if not m:
            return faces
        try:
            root = ET.fromstring(m.group(0).encode("utf-8", "ignore"))
        except Exception:
            return faces
    for li in root.iter("{%s}li" % _NS["rdf"]):
        area = li.find(".//{%s}Area" % _NS["mwg-rs"])
        rtype = li.get("{%s}Type" % _NS["mwg-rs"]) or li.findtext("{%s}Type" % _NS["mwg-rs"])
        name = li.get("{%s}Name" % _NS["mwg-rs"]) or li.findtext("{%s}Name" % _NS["mwg-rs"])
        # Area 屬性可能在 li 上或子元素上
        def g(attr):
            for el in (li, area if area is not None else li):
                v = el.get("{%s}%s" % (_NS["stArea"], attr)) if el is not None else None
                if v is not None:
                    return v
            return None
        x, y, w = g("x"), g("y"), g("w")
        if x is None or y is None:
            continue
        if rtype and str(rtype).lower() not in ("face", ""):
            continue
        try:
            faces.append({
                "name": name or "",
                "x": round(float(x), 4), "y": round(float(y), 4),
                "w": round(float(w), 4) if w is not None else None,
            })
        except Exception:
            continue
    return faces


def _xmp_meta(path: str, W: int, H: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from PIL import Image

        with Image.open(path) as im:
            xmp = im.info.get("xmp")
        if not xmp:
            return out
        s = xmp.decode("utf-8", "ignore") if isinstance(xmp, bytes) else str(xmp)
        faces = _xmp_faces(s, W, H)
        if faces:
            out["faces"] = faces
            out["persons"] = sorted({f["name"] for f in faces if f["name"]})
        kws = re.findall(r"<dc:subject>.*?</dc:subject>", s, re.S)
        if kws:
            out["keywords"] = re.findall(r"<rdf:li[^>]*>([^<]+)</rdf:li>", kws[0])
        r = re.search(r'xmp:Rating="(\d+)"', s) or re.search(r"<xmp:Rating>(\d+)</xmp:Rating>", s)
        if r:
            out["rating"] = int(r.group(1))
            if int(r.group(1)) >= 4:
                out["favorite"] = True
    except Exception:
        pass
    return out


def probe_folder(folder: str) -> Dict[str, Dict[str, Any]]:
    from PIL import Image

    meta: Dict[str, Dict[str, Any]] = {}
    fp = Path(folder)
    for f in sorted(fp.iterdir()):
        if f.name.startswith(".") or f.suffix.lower() not in (_IMG | _VID):
            continue
        stem = f.stem.lower()
        rec = meta.setdefault(stem, {
            "favorite": False, "keywords": [], "persons": [], "faces": [],
            "score": None, "score_detail": {}, "labels": [], "activities": [],
            "gps": None, "taken": None, "camera": None, "kind": {}, "edited": False, "exclude": False,
        })
        if f.suffix.lower() in _IMG:
            try:
                with Image.open(f) as im:
                    W, H = im.size
            except Exception:
                W = H = 0
            ex = _exif(str(f))
            xm = _xmp_meta(str(f), W, H)
            if ex.get("gps"):
                rec["gps"] = ex["gps"]
            if ex.get("taken") and not rec["taken"]:
                rec["taken"] = ex["taken"]
            if ex.get("focal35"):
                rec["focal35"] = ex["focal35"]
            for k in ("faces", "persons", "keywords"):
                if xm.get(k):
                    rec[k] = xm[k]
            if xm.get("favorite"):
                rec["favorite"] = True
            rec["kind"]["panorama"] = bool(W and H and max(W, H) / max(1, min(W, H)) > 2.2)
            rec["kind"]["screenshot"] = bool(not ex.get("_has_camera") and not ex.get("gps")
                                             and (W, H) in {(1170, 2532), (1179, 2556), (1290, 2796),
                                                            (1125, 2436), (1284, 2778), (750, 1334)})
            m = _exif_camera(str(f))
            if m:
                rec["camera"] = m
        else:
            v = _video_meta(str(f))          # MOV/MP4：拍攝時間 + 相機型號
            if v.get("taken"):
                rec["taken"] = v["taken"]
            if v.get("camera"):
                rec["camera"] = v["camera"]
            if v.get("gps"):
                rec["gps"] = v["gps"]
    return meta


def _exif_camera(path: str) -> Optional[str]:
    try:
        import piexif

        ex = piexif.load(path)
        z = ex.get("0th", {})
        mk = z.get(piexif.ImageIFD.Make)
        md = z.get(piexif.ImageIFD.Model)
        md = md.decode(errors="ignore").strip("\x00 ") if isinstance(md, bytes) else md
        return md or (mk.decode(errors="ignore").strip("\x00 ") if isinstance(mk, bytes) else None)
    except Exception:
        return None


def _video_meta(path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from pymediainfo import MediaInfo

        mi = MediaInfo.parse(path)
        g = next((t for t in mi.tracks if t.track_type == "General"), None)
        if not g:
            return out
        # 拍攝時間：Apple QuickTime 的當地時間最準，其次 recorded/encoded
        raw = (getattr(g, "comapplequicktimecreationdate", None)
               or getattr(g, "recorded_date", None)
               or getattr(g, "encoded_date", None))
        if raw:
            iso = str(raw).replace("UTC", "").strip().replace(" ", "T", 1)
            iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
            out["taken"] = iso[:19] if "T" in iso else iso
        md = getattr(g, "comapplequicktimemodel", None)
        mk = getattr(g, "comapplequicktimemake", None)
        if md or mk:
            out["camera"] = str(md or mk).strip()
        lat = getattr(g, "xyz", None) or getattr(g, "comapplequicktimelocationiso6709", None)
        if lat:
            mm = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", str(lat))
            if mm:
                out["gps"] = {"lat": round(float(mm.group(1)), 6),
                              "lon": round(float(mm.group(2)), 6), "alt": None}
    except Exception:
        pass
    return out
