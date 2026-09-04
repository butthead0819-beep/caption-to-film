import os
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from .metadata_extractor import MetadataExtractor
from .geocoding import ReverseGeocoder


class ApplePhotosExtractor:
    """
    負責直接與 macOS 本機 Apple Photos (照片 App) 資料庫對接，
    支援相簿列舉、說明欄 (Caption)、Live Photo 配對影片路徑、GPS 與 Apple 內建 AI 標籤之完整無損提取。
    """

    def __init__(self, geocoder: Optional[ReverseGeocoder] = None):
        self.geocoder = geocoder or ReverseGeocoder()
        self._photos_db = None

    def is_available(self) -> bool:
        """檢查當前系統與環境是否支援 Apple Photos 本機直接讀取"""
        if sys.platform != "darwin":
            return False
        try:
            import osxphotos
            return True
        except ImportError:
            return False

    def _get_db(self):
        """延遲載入 PhotosDB 實例"""
        if self._photos_db is not None:
            return self._photos_db
        
        if not self.is_available():
            raise RuntimeError("osxphotos 未安裝或當前系統非 macOS，無法存取 Apple Photos 資料庫。")

        import osxphotos
        try:
            self._photos_db = osxphotos.PhotosDB()
            return self._photos_db
        except Exception as e:
            raise RuntimeError(f"無法存取 Apple Photos 本機資料庫: {str(e)}")

    def list_albums(self) -> List[Dict[str, Any]]:
        """
        列出使用者的所有 Apple Photos 相簿
        """
        db = self._get_db()
        albums_info = []

        try:
            # 取得一般與共享相簿
            for album in db.album_info:
                albums_info.append({
                    "title": album.title,
                    "uuid": album.uuid,
                    "count": len(album.photos),
                    "folder_names": album.folder_names if hasattr(album, 'folder_names') else []
                })
        except Exception as e:
            print(f"[Warning] 列舉相簿出錯: {e}")

        # 排序：照片數多者優先
        albums_info.sort(key=lambda x: x["count"], reverse=True)
        return albums_info

    def scan_album(
        self,
        album_name: str,
        max_photos: int = 200,
        resolve_location: bool = True
    ) -> List[Dict[str, Any]]:
        """
        掃描特定名稱的 Apple Photos 相簿，回傳統一格式的媒體資料清單
        """
        db = self._get_db()
        photos = db.photos(albums=[album_name])
        if not photos:
            # 嘗試搜尋部分匹配
            for alb in db.album_info:
                if album_name.lower() in alb.title.lower():
                    photos = alb.photos
                    break

        if not photos:
            return []

        # 限制數量並依拍攝時間排序
        photos = sorted(photos, key=lambda p: p.date or datetime.min)
        if max_photos and len(photos) > max_photos:
            photos = photos[:max_photos]

        results = []
        for p in photos:
            try:
                item = self._convert_photo_to_media_item(p, resolve_location=resolve_location)
                if item:
                    results.append(item)
            except Exception as e:
                print(f"[Warning] 轉換相片 {getattr(p, 'original_filename', 'unknown')} 失敗: {e}")

        return results

    def _convert_photo_to_media_item(self, p, resolve_location: bool = True) -> Optional[Dict[str, Any]]:
        """將 osxphotos PhotoInfo 物件標準化為系統 media item 字典"""
        # 原始圖檔路徑
        file_path = p.path or p.path_edited or p.path_raw
        if not file_path or not Path(file_path).exists():
            # 嘗試從衍生檔案或預覽圖路徑中尋找
            if hasattr(p, 'path_derivatives') and p.path_derivatives:
                file_path = p.path_derivatives[0]
            else:
                return None

        path_obj = Path(file_path)
        is_video = bool(p.isvideo)
        is_live_photo = bool(p.live_photo)
        is_image = not is_video

        # 說明欄 (Caption / Description) 提取
        caption = ""
        if p.description:
            caption = p.description.strip()
        elif p.title:
            caption = p.title.strip()

        # 拍攝時間
        creation_date = p.date.isoformat() if p.date else None
        creation_date_formatted = p.date.strftime("%Y-%m-%d %H:%M:%S") if p.date else ""

        # 尺寸
        width = p.width or 0
        height = p.height or 0

        # 相機資訊
        camera = {
            "make": p.make if hasattr(p, 'make') else None,
            "model": p.model if hasattr(p, 'model') else None,
            "lens": p.lens_model if hasattr(p, 'lens_model') else None,
            "focal_length": None,
            "iso": p.iso if hasattr(p, 'iso') else None,
            "f_number": None,
            "exposure_time": None
        }

        # GPS 與地理位置
        gps = None
        location_info = None
        if p.location and p.location[0] is not None and p.location[1] is not None:
            lat, lon = float(p.location[0]), float(p.location[1])
            alt = float(p.altitude) if (hasattr(p, 'altitude') and p.altitude is not None) else 0.0
            gps = {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "altitude": round(alt, 1)
            }
            if resolve_location and self.geocoder:
                # 優先使用 Apple Photos 內建逆地理編碼 (若有)
                if hasattr(p, 'place') and p.place and p.place.name:
                    location_info = {
                        "short_location": p.place.name,
                        "full_address": f"{p.place.name}, {getattr(p.place, 'country', '')}"
                    }
                else:
                    location_info = self.geocoder.reverse_geocode(lat, lon)

        # 媒體類型判定
        if is_live_photo:
            media_type = "live_photo"
        elif is_video:
            media_type = "video"
        else:
            media_type = "image"

        # 配對 Live Photo MOV 影片路徑
        live_photo_video_path = p.path_live_photo if is_live_photo else None

        item: Dict[str, Any] = {
            "file_name": p.original_filename or path_obj.name,
            "file_path": str(Path(file_path).resolve()),
            "file_size": path_obj.stat().st_size if path_obj.exists() else 0,
            "file_ext": path_obj.suffix.lower(),
            "is_image": is_image,
            "is_video": is_video,
            "is_live_photo": is_live_photo,
            "media_type": media_type,
            "caption": caption,
            "apple_ai_caption": getattr(p, "search_info", {}).get("confidence") if hasattr(p, "search_info") else None,
            "creation_date": creation_date,
            "creation_date_formatted": creation_date_formatted,
            "gps": gps,
            "location": location_info,
            "width": width,
            "height": height,
            "orientation": p.orientation or 1,
            "duration": round(float(p.duration or 0), 2) if is_video else None,
            "camera": camera,
            "live_photo_video": live_photo_video_path,
            "persons": p.persons if hasattr(p, 'persons') else [],
            "keywords": p.keywords if hasattr(p, 'keywords') else [],
            "favorite": bool(p.favorite) if hasattr(p, 'favorite') else False
        }

        return item
