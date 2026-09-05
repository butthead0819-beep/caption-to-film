import os
import sys
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from PIL import Image, ExifTags
import pillow_heif

# 註冊 pillow_heif 讓 PIL 直接支援 HEIC/HEIF
pillow_heif.register_heif_opener()


class MetadataExtractor:
    """
    負責提取影像與影片的各項 Metadata，
    特別專注於提取 iOS 照片/影片中的「說明欄 (Caption / Description / UserComment)」與 GPS、時間軸資訊。
    """

    SUPPORTED_IMAGE_EXTS = {'.heic', '.heif', '.jpg', '.jpeg', '.png', '.tiff', '.webp'}
    SUPPORTED_VIDEO_EXTS = {'.mov', '.mp4', '.m4v'}

    @classmethod
    def is_supported(cls, file_path: str) -> bool:
        ext = Path(file_path).suffix.lower()
        return ext in cls.SUPPORTED_IMAGE_EXTS or ext in cls.SUPPORTED_VIDEO_EXTS

    @classmethod
    def is_video(cls, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in cls.SUPPORTED_VIDEO_EXTS

    @classmethod
    def is_image(cls, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in cls.SUPPORTED_IMAGE_EXTS

    def extract(self, file_path: str) -> Dict[str, Any]:
        """
        統一提取單一檔案的完整 metadata 字典
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"檔案不存在: {file_path}")

        file_stat = path.stat()
        meta: Dict[str, Any] = {
            "file_name": path.name,
            "file_path": str(path.resolve()),
            "file_size": file_stat.st_size,
            "file_ext": path.suffix.lower(),
            "is_video": self.is_video(file_path),
            "is_image": self.is_image(file_path),
            "caption": "",
            "creation_date": None,
            "creation_date_formatted": None,
            "gps": None,  # {"latitude": float, "longitude": float, "altitude": float}
            "width": 0,
            "height": 0,
            "orientation": 1,
            "duration": None,  # 影片時長（秒）
            "camera": {
                "make": None,
                "model": None,
                "lens": None,
                "focal_length": None,
                "iso": None,
                "f_number": None,
                "exposure_time": None
            },
            "raw_metadata": {}
        }

        if meta["is_image"]:
            self._extract_image_metadata(path, meta)
        elif meta["is_video"]:
            self._extract_video_metadata(path, meta)

        # 優先/補充檢查 macOS Extended Attributes (xattr) 與 Spotlight (mdls) 中的說明/註解 (例如 AirDrop/iOS 匯入)
        self._extract_macos_metadata(path, meta)

        # 若 creation_date 仍未取得，則 fallback 至檔案修改時間
        if not meta["creation_date"]:
            meta["creation_date"] = datetime.fromtimestamp(file_stat.st_mtime).isoformat()
        
        if meta["creation_date"]:
            try:
                dt = datetime.fromisoformat(meta["creation_date"].replace("Z", "+00:00"))
                meta["creation_date_formatted"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                meta["creation_date_formatted"] = str(meta["creation_date"])

        return meta

    def _extract_image_metadata(self, path: Path, meta: Dict[str, Any]):
        """從靜態圖片 (HEIC, JPG, PNG 等) 中提取 EXIF/IPTC/XMP/說明欄"""
        try:
            with Image.open(path) as img:
                meta["width"], meta["height"] = img.size
                
                # 取得 EXIF
                exif_data = img.getexif()
                if exif_data:
                    self._parse_exif(exif_data, img, meta)
                
                # 取得 XMP / IPTC (若有)
                self._parse_xmp_and_info(img, meta)

        except Exception as e:
            meta["raw_metadata"]["error"] = f"Image metadata parse error: {str(e)}"

    def _parse_exif(self, exif_data: Image.Exif, img: Image.Image, meta: Dict[str, Any]):
        """解析標準 EXIF 標籤與 GPS / 說明欄"""
        # 標籤映射
        tag_map = {ExifTags.TAGS.get(k, k): v for k, v in exif_data.items()}
        
        # 1. 說明欄提取 (ImageDescription, UserComment)
        if "ImageDescription" in tag_map and tag_map["ImageDescription"]:
            desc = self._fix_text_encoding(tag_map["ImageDescription"])
            if desc and not meta["caption"]:
                meta["caption"] = desc
        
        # 讀取 Exif IFD 取得更深層資訊
        exif_ifd = exif_data.get_ifd(ExifTags.IFD.Exif)
        if exif_ifd:
            ifd_tags = {ExifTags.TAGS.get(k, k): v for k, v in exif_ifd.items()}
            
            # UserComment
            if "UserComment" in ifd_tags and ifd_tags["UserComment"]:
                user_comment = self._decode_user_comment(ifd_tags["UserComment"])
                if user_comment and not meta["caption"]:
                    meta["caption"] = user_comment
            
            # 拍攝時間 DateTimeOriginal / DateTimeDigitized
            date_str = ifd_tags.get("DateTimeOriginal") or ifd_tags.get("DateTimeDigitized")
            if date_str:
                meta["creation_date"] = self._parse_exif_date(str(date_str))
            
            # 相機參數
            meta["camera"]["lens"] = ifd_tags.get("LensModel")
            meta["camera"]["iso"] = ifd_tags.get("ISOSpeedRatings")
            meta["camera"]["f_number"] = self._format_ratio(ifd_tags.get("FNumber"))
            meta["camera"]["focal_length"] = self._format_ratio(ifd_tags.get("FocalLength"))
            meta["camera"]["exposure_time"] = self._format_exposure_time(ifd_tags.get("ExposureTime"))

        # 相機機型
        meta["camera"]["make"] = tag_map.get("Make")
        meta["camera"]["model"] = tag_map.get("Model")
        meta["orientation"] = tag_map.get("Orientation", 1)

        # 頂層 DateTime (若 IFD 沒有)
        if not meta["creation_date"] and "DateTime" in tag_map:
            meta["creation_date"] = self._parse_exif_date(str(tag_map["DateTime"]))

        # 2. GPS IFD 提取
        gps_ifd = exif_data.get_ifd(ExifTags.IFD.GPSInfo)
        if gps_ifd:
            gps_tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
            gps_coords = self._parse_gps_coords(gps_tags)
            if gps_coords:
                meta["gps"] = gps_coords

    def _parse_xmp_and_info(self, img: Image.Image, meta: Dict[str, Any]):
        """解析 XMP / IPTC 中的說明文字 (常見於 iOS 相簿說明欄)"""
        # 1. 檢查 img.info 中的 XMP
        xmp_raw = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp")
        if xmp_raw:
            if isinstance(xmp_raw, bytes):
                xmp_str = xmp_raw.decode('utf-8', errors='ignore')
            else:
                xmp_str = str(xmp_raw)
            
            # 從 XMP 尋找 description, title, alt-text
            # 常見模式: <dc:description><rdf:Alt><rdf:li xml:lang="x-default">我的記憶</rdf:li>
            match = re.search(r'<dc:description>.*?<rdf:li[^>]*>(.*?)</rdf:li>', xmp_str, re.DOTALL)
            if match:
                caption = match.group(1).strip()
                if caption and not meta["caption"]:
                    meta["caption"] = caption
            
            # 或者是 headline 或 caption 標籤
            if not meta["caption"]:
                match_desc = re.search(r'photoshop:Headline="([^"]+)"', xmp_str)
                if match_desc:
                    meta["caption"] = match_desc.group(1).strip()

        # 2. 檢查 IPTC (若有)
        iptc = img.info.get("photoshop", {})
        if isinstance(iptc, dict) and not meta["caption"]:
            caption = iptc.get("caption") or iptc.get("caption-abstract")
            if caption:
                meta["caption"] = str(caption).strip()

    def _parse_finder_comment_into_meta(self, raw_comment: str, meta: Dict[str, Any]):
        """智慧拆解 Finder 註解中的「人工心得」與「AI 摘要/精華標籤」"""
        if "【AI摘要】" not in raw_comment and "【精華剪輯" not in raw_comment:
            meta["caption"] = raw_comment
            meta["user_caption_memory"] = raw_comment
            return

        user_parts = []
        ai_summary = ""

        # 情況 A：使用者寫在 【AI摘要】 之前（最推薦的直覺寫法）
        parts = raw_comment.split("【AI摘要】")
        prefix_text = parts[0].strip()
        if prefix_text:
            clean_prefix = re.sub(r"^【(?:心得|註解|筆記|回憶)】\s*", "", prefix_text).strip()
            if clean_prefix:
                user_parts.append(clean_prefix)

        # 提取 AI 摘要
        rest = parts[1] if len(parts) > 1 else ""
        summary_match = re.search(r"^(.*?)(?=【精華剪輯|【素材特性|【避坑提示|$)", rest, re.DOTALL)
        if summary_match:
            ai_summary = summary_match.group(1).strip()

        # 檢查是否有其他標明 【心得】 的區塊
        notes_match = re.search(r"【(?:心得|筆記|人工備忘|回憶)】\s*(.*?)(?=【|$)", raw_comment, re.DOTALL)
        if notes_match:
            custom_note = notes_match.group(1).strip()
            if custom_note and custom_note not in user_parts:
                user_parts.append(custom_note)

        meta["ai_summary"] = ai_summary
        meta["raw_finder_comment"] = raw_comment

        if user_parts:
            final_user_caption = "\n".join(user_parts)
            meta["caption"] = final_user_caption
            meta["user_caption_memory"] = final_user_caption
        else:
            # 尚未寫入人工心得時，留空 caption 避免 AI 第三方描述污染第一人稱口白
            meta["caption"] = ""
            meta["user_caption_memory"] = ""

    def _extract_video_metadata(self, path: Path, meta: Dict[str, Any]):
        """從影片 (MOV, MP4) 中提取時長、尺寸、說明欄與 GPS"""
        # 1. 優先使用 xattr 讀取 FinderComment (極速)
        try:
            import xattr, plistlib
            raw_c = xattr.getxattr(str(path), 'com.apple.metadata:kMDItemFinderComment')
            if raw_c:
                parsed = plistlib.loads(raw_c)
                if isinstance(parsed, str) and parsed.strip() and not meta["caption"]:
                    self._parse_finder_comment_into_meta(parsed.strip(), meta)
        except Exception:
            pass

        # 2. 透過 media_probe 探測真實尺寸、時長與旋轉後寬高
        try:
            from backend.util.media_probe import probe_video
            pinfo = probe_video(str(path))
            if pinfo.get("duration_s", 0) > 0:
                meta["duration"] = pinfo["duration_s"]
            if pinfo.get("width") and pinfo.get("height"):
                meta["width"] = pinfo["width"]
                meta["height"] = pinfo["height"]
        except Exception:
            pass

        # 3. 若仍無時長尺寸，退回預設保底
        if not meta.get("duration"):
            meta["duration"] = 3.0
        if not meta.get("width"):
            meta["width"] = 1920
            meta["height"] = 1080

    def _extract_macos_metadata(self, path: Path, meta: Dict[str, Any]):
        """從 macOS Spotlight (mdls) 或 xattr 中提取 iOS 說明欄/註解 (kMDItemFinderComment)"""
        if os.name != 'posix' or sys.platform != 'darwin':
            return

        # 1. 優先使用 xattr 直接讀取 com.apple.metadata:kMDItemFinderComment (極速且最精準)
        try:
            import xattr
            import plistlib
            raw_comment = xattr.getxattr(str(path), 'com.apple.metadata:kMDItemFinderComment')
            if raw_comment:
                try:
                    parsed = plistlib.loads(raw_comment)
                    if isinstance(parsed, str) and parsed.strip() and not meta.get("caption"):
                        self._parse_finder_comment_into_meta(parsed.strip(), meta)
                        return
                except Exception:
                    pass
        except Exception:
            pass

    def _fix_text_encoding(self, raw_val: Any) -> Optional[str]:
        """修正 EXIF/IPTC 字串在 PIL 中常以 latin-1 解碼導致 UTF-8 中文亂碼的問題"""
        if raw_val is None:
            return None
        if isinstance(raw_val, bytes):
            for enc in ('utf-8', 'utf-16', 'utf-16le', 'utf-16be', 'big5', 'gbk'):
                try:
                    return raw_val.decode(enc).strip('\x00').strip()
                except Exception:
                    continue
            return raw_val.decode('latin-1', errors='ignore').strip('\x00').strip()
        elif isinstance(raw_val, str):
            # 若為 str 但實際上是 latin1 解碼的 utf-8 bytes
            try:
                fixed = raw_val.encode('latin-1').decode('utf-8')
                return fixed.strip()
            except Exception:
                return raw_val.strip()
        return str(raw_val).strip()

    def _decode_user_comment(self, raw_val: Any) -> Optional[str]:
        """解碼 EXIF UserComment (可能含有 UNICODE / ASCII header)"""
        if isinstance(raw_val, bytes):
            # 前 8 bytes 可能為 "UNICODE\0" 或 "ASCII\0\0\0"
            if raw_val.startswith(b'UNICODE\x00'):
                try:
                    return raw_val[8:].decode('utf-16be').strip('\x00').strip()
                except Exception:
                    try:
                        return raw_val[8:].decode('utf-16le').strip('\x00').strip()
                    except Exception:
                        pass
            elif raw_val.startswith(b'ASCII\x00\x00\x00'):
                return raw_val[8:].decode('ascii', errors='ignore').strip('\x00').strip()
            return raw_val.decode('utf-8', errors='ignore').strip('\x00').strip()
        elif isinstance(raw_val, str):
            return raw_val.strip()
        return None

    def _parse_gps_coords(self, gps_tags: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """轉換 EXIF GPSInfo 字典為緯度/經度浮點數"""
        try:
            lat_raw = gps_tags.get("GPSLatitude")
            lat_ref = gps_tags.get("GPSLatitudeRef", "N")
            lon_raw = gps_tags.get("GPSLongitude")
            lon_ref = gps_tags.get("GPSLongitudeRef", "E")
            alt_raw = gps_tags.get("GPSAltitude")

            if not lat_raw or not lon_raw:
                return None

            lat = self._convert_dms_to_deg(lat_raw)
            if lat_ref.upper() == "S":
                lat = -lat

            lon = self._convert_dms_to_deg(lon_raw)
            if lon_ref.upper() == "W":
                lon = -lon

            alt = 0.0
            if alt_raw:
                alt = float(alt_raw)

            return {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "altitude": round(alt, 1)
            }
        except Exception:
            return None

    def _convert_dms_to_deg(self, dms) -> float:
        """度分秒轉十進位度"""
        if isinstance(dms, (list, tuple)) and len(dms) == 3:
            d = float(dms[0])
            m = float(dms[1])
            s = float(dms[2])
            return d + (m / 60.0) + (s / 3600.0)
        return float(dms)

    def _parse_iso6709_gps(self, iso_str: str) -> Optional[Dict[str, float]]:
        """解析 QuickTime ISO 6709 字串 (例: +25.0330+121.5654+010.5/ 或 +35.6586+139.7454/)"""
        pattern = r'([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:([+-]\d+(?:\.\d+)?))?'
        match = re.match(pattern, iso_str.strip('/'))
        if match:
            lat = float(match.group(1))
            lon = float(match.group(2))
            alt = float(match.group(3)) if match.group(3) else 0.0
            return {
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "altitude": round(alt, 1)
            }
        return None

    def _parse_exif_date(self, date_str: str) -> Optional[str]:
        """將 EXIF 日期格式 'YYYY:MM:DD HH:MM:SS' 轉換為 ISO 格式"""
        try:
            clean_str = date_str.strip()
            # 典型 EXIF 格式: 2024:08:26 14:30:00
            dt = datetime.strptime(clean_str, "%Y:%m:%d %H:%M:%S")
            return dt.isoformat()
        except Exception:
            return None

    def _parse_video_date(self, date_str: str) -> Optional[str]:
        """解析影片日期字串為 ISO 格式"""
        for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                return dt.isoformat()
            except Exception:
                continue
        return None

    def _format_ratio(self, val: Any) -> Optional[float]:
        if val is None:
            return None
        try:
            return round(float(val), 2)
        except Exception:
            return None

    def _format_exposure_time(self, val: Any) -> Optional[str]:
        if val is None:
            return None
        try:
            fval = float(val)
            if fval < 1.0 and fval > 0:
                denom = round(1.0 / fval)
                return f"1/{denom}s"
            return f"{fval:.2f}s"
        except Exception:
            return str(val)
