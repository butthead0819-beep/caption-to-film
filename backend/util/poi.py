"""GPS → 最近的地標名稱（給地圖片段 caption / 章節命名）。

嘗試順序（都有磁碟快取 scripts/poi_cache.json）：
  1. Google Places API (New) — 需要 GOOGLE_MAPS_API_KEY 或 GOOGLE_API_KEY，
     且該 GCP 專案要啟用「Places API (New)」。給的名字最準（"大坡池" 這種）。
  2. OSM Overpass — 免 key，抓 400m 內有 name 的 tourism/natural/leisure/historic/water。
     公共伺服器常忙，會試兩個鏡像、失敗就跳過。
  3. Nominatim 逆地理 — 免 key，只到「鄉/鎮/區」層級。

最好的免費來源其實是 Apple 自己的 place.name（scripts/dump_photos_metadata.py，
Apple 對台灣地標涵蓋很好）——呼叫端應優先用 photos_meta 的 place，再退到這裡。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

try:  # 觸發 .env 載入（GOOGLE_API_KEY 等）
    from ..config import config as _cfg  # noqa: F401
except Exception:
    pass

_CACHE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "poi_cache.json"
_cache: Optional[dict] = None
_OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_UA = {"User-Agent": "caption-to-film/1.0 (github.com/butthead0819-beep/caption-to-film)"}


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text("utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass


# 可當「地標」的類型（越前面越優先）
_GTYPE_PRIO = [
    "national_park", "state_park", "park", "hiking_area", "beach", "natural_feature",
    "tourist_attraction", "historical_landmark", "historical_place", "monument",
    "cultural_landmark", "scenic_point", "observation_deck", "marina", "wildlife_park",
    "garden", "museum", "art_gallery", "aquarium", "zoo", "stadium",
    "train_station", "transit_station", "visitor_center", "city_hall", "locality",
]
# 明顯是「路邊隨便一間店」→ 不要當地標
_GTYPE_JUNK = {
    "restaurant", "food", "store", "shop", "cafe", "coffee_shop", "bakery",
    "convenience_store", "supermarket", "clothing_store", "food_store",
    "meal_takeaway", "meal_delivery", "bar", "lodging", "hotel", "motel",
    "guest_house", "hostel", "real_estate_agency", "insurance_agency",
    "car_repair", "gas_station", "parking", "atm", "bank", "pharmacy",
    "beauty_salon", "hair_care", "gym", "dental_clinic", "doctor",
    "place_of_worship", "church", "hindu_temple", "mosque", "synagogue",
    "corporate_office", "association_or_organization",
}


def _google(lat: float, lon: float) -> Optional[str]:
    key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    try:
        import requests

        r = requests.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.displayName,places.types,places.location"},
            json={"maxResultCount": 20, "rankPreference": "DISTANCE",
                  "locationRestriction": {"circle": {
                      "center": {"latitude": lat, "longitude": lon}, "radius": 900}}},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        best, best_rank = None, 1e9
        for i, p in enumerate(r.json().get("places", [])):
            types = set(p.get("types") or [])
            if types & _GTYPE_JUNK and not (types & set(_GTYPE_PRIO)):
                continue  # 純粹路邊小店 → 跳過
            prio = min((_GTYPE_PRIO.index(t) for t in types if t in _GTYPE_PRIO), default=40)
            rank = prio * 100 + i          # 類型優先度為主、距離順位為輔
            if rank < best_rank:
                nm = (p.get("displayName") or {}).get("text")
                if nm:
                    best, best_rank = nm, rank
        return best
    except Exception:
        pass
    return None


_PRIO = {"natural": 0, "water": 0, "tourism": 1, "historic": 1, "place": 2}


def _overpass(lat: float, lon: float) -> Optional[str]:
    import math

    q = (f"[out:json][timeout:15];("
         f"nwr(around:600,{lat},{lon})[natural][name];"
         f"nwr(around:600,{lat},{lon})[water][name];"
         f"nwr(around:600,{lat},{lon})[tourism][name][tourism!=hotel][tourism!=guest_house];"
         f"nwr(around:600,{lat},{lon})[historic][name];"
         f"nwr(around:600,{lat},{lon})[place][name][place!=locality];);out center 20;")
    try:
        import requests

        for url in _OVERPASS:
            try:
                r = requests.post(url, data={"data": q}, headers=_UA, timeout=25)
                if r.status_code != 200:
                    continue
                cands = []
                for e in r.json().get("elements", []):
                    t = e.get("tags", {})
                    nm = t.get("name")
                    if not nm:
                        continue
                    c = e.get("center") or e
                    ey, ex = c.get("lat"), c.get("lon")
                    if ey is None:
                        continue
                    dist = math.hypot(ey - lat, ex - lon)
                    prio = min((_PRIO[k] for k in _PRIO if k in t), default=3)
                    cands.append((prio, dist, nm))
                if cands:
                    cands.sort()
                    return cands[0][2]
            except Exception:
                continue
    except Exception:
        pass
    return None


def _nominatim(lat: float, lon: float) -> Optional[str]:
    try:
        from geopy.geocoders import Nominatim

        g = Nominatim(user_agent="imovie-script/1.0", timeout=8)
        time.sleep(1.0)
        loc = g.reverse((lat, lon), language="zh-TW", exactly_one=True)
        if not loc:
            return None
        a = loc.raw.get("address", {})
        return (a.get("tourism") or a.get("natural") or a.get("leisure")
                or a.get("historic") or a.get("water") or a.get("village")
                or a.get("town") or a.get("suburb") or a.get("city")
                or a.get("county"))
    except Exception:
        return None


def place_name(lat: float, lon: float) -> Optional[str]:
    """最近的『地標』名（Google Places → Overpass → Nominatim）。
    在馬路邊會回傳最近的店家，不一定是地標——地圖 caption 用 area_name 較穩。"""
    key = f"p:{round(lat, 4)},{round(lon, 4)}"
    c = _load()
    if key in c:
        return c[key] or None
    name = _google(lat, lon) or _overpass(lat, lon) or _nominatim(lat, lon)
    c[key] = name or ""
    _save()
    return name


def area_name(lat: float, lon: float) -> Optional[str]:
    """行政區級地名（縣市 + 鄉鎮 / 景區），適合地圖片段 caption。用 Nominatim。"""
    key = f"a:{round(lat, 3)},{round(lon, 3)}"
    c = _load()
    if key in c:
        return c[key] or None
    name = None
    try:
        from geopy.geocoders import Nominatim

        g = Nominatim(user_agent="imovie-script/1.0", timeout=8)
        time.sleep(1.0)
        loc = g.reverse((lat, lon), language="zh-TW", exactly_one=True)
        if loc:
            a = loc.raw.get("address", {})
            town = (a.get("town") or a.get("village") or a.get("suburb")
                    or a.get("city") or a.get("municipality"))
            county = a.get("county") or a.get("state")
            scenic = a.get("tourism") or a.get("natural") or a.get("leisure")
            base = "".join(dict.fromkeys(x for x in (county, town) if x))
            name = f"{base}·{scenic}" if (scenic and scenic not in (base or "")) else (base or None)
    except Exception:
        pass
    c[key] = name or ""
    _save()
    return name
