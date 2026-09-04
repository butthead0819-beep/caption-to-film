import time
from typing import Optional, Dict, Any
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError


class ReverseGeocoder:
    """
    將 GPS 經緯度轉換為人類可讀的地名、景點、城市與國家。
    內建記憶體快取以減少 API 呼叫，並支援繁體中文地名轉換。
    """

    def __init__(self, user_agent: str = "imovie_script_generator_app"):
        self.geolocator = Nominatim(user_agent=user_agent, timeout=5)
        self._cache: Dict[str, Dict[str, str]] = {}

    def reverse(self, lat: float, lon: float, language: str = "zh-TW") -> Optional[Dict[str, str]]:
        """
        傳入緯度 (lat) 與經度 (lon)，返回地名詳細資訊字典
        例如: {
            "display_name": "清水寺, 東山區, 京都市, 京都府, 日本",
            "city": "京都市",
            "state": "京都府",
            "country": "日本",
            "poi": "清水寺",
            "short_location": "日本 京都市 清水寺"
        }
        """
        # 四捨五入到小數點後 3 位 (約 100 公尺精度) 作為快取 key
        cache_key = f"{round(lat, 3)}_{round(lon, 3)}_{language}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            # 延遲避免請求過於頻繁
            time.sleep(0.3)
            location = self.geolocator.reverse(
                f"{lat}, {lon}",
                language=language,
                exactly_one=True
            )

            if not location or not location.raw:
                return None

            raw = location.raw
            address = raw.get("address", {})

            # 抓取各級地名
            country = address.get("country", "")
            state = address.get("state") or address.get("province") or address.get("county", "")
            city = address.get("city") or address.get("town") or address.get("suburb") or address.get("village", "")
            poi = (
                address.get("tourism") or
                address.get("historic") or
                address.get("amenity") or
                address.get("leisure") or
                address.get("attraction") or
                address.get("natural") or
                address.get("road", "")
            )

            # 組成精簡地名
            parts = [p for p in [country, city or state, poi] if p]
            short_location = " ".join(parts) if parts else location.address

            res = {
                "display_name": location.address,
                "country": country,
                "state": state,
                "city": city,
                "poi": poi,
                "short_location": short_location
            }

            self._cache[cache_key] = res
            return res

        except (GeocoderTimedOut, GeocoderServiceError) as e:
            # 網路逾時或連線問題時返回基本座標資訊
            return {
                "display_name": f"GPS ({lat:.4f}, {lon:.4f})",
                "country": "",
                "state": "",
                "city": "",
                "poi": "",
                "short_location": f"GPS ({lat:.4f}, {lon:.4f})"
            }
        except Exception:
            return None
