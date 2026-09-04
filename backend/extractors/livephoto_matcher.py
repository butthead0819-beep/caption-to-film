import os
from pathlib import Path
from typing import List, Dict, Any, Tuple
from .metadata_extractor import MetadataExtractor
from .geocoding import ReverseGeocoder


class LivePhotoMatcher:
    """
    掃描多媒體檔案清單，智慧配對 Live Photo (靜態圖片 + MOV 動態短片)，
    並依據時間軸整合所有的照片與影片資料。
    """

    def __init__(self, metadata_extractor: MetadataExtractor = None, geocoder: ReverseGeocoder = None):
        self.extractor = metadata_extractor or MetadataExtractor()
        self.geocoder = geocoder or ReverseGeocoder()

    def process_directory(self, dir_path: str, resolve_location: bool = True) -> List[Dict[str, Any]]:
        """
        遍歷目錄下的所有支援媒體檔案，提取 metadata、配對 Live Photo，並按拍攝時間排序
        """
        p = Path(dir_path)
        if not p.exists() or not p.is_dir():
            raise NotADirectoryError(f"資料夾不存在: {dir_path}")

        files = [
            str(f) for f in p.iterdir() 
            if f.is_file() and not f.name.startswith('.') and self.extractor.is_supported(str(f))
        ]
        return self.process_files(files, resolve_location=resolve_location)

    def process_files(self, file_paths: List[str], resolve_location: bool = True) -> List[Dict[str, Any]]:
        """
        接收檔案路徑清單，進行 metadata 解析、Live Photo 配對與地理位置解析
        """
        raw_items: List[Dict[str, Any]] = []
        
        # 將檔案按副檔名區分，優先找出圖片 stem
        image_paths = []
        video_paths = []
        for f in file_paths:
            if self.extractor.is_image(f):
                image_paths.append(f)
            elif self.extractor.is_video(f):
                video_paths.append(f)

        image_stems = {Path(p).stem: p for p in image_paths}

        # 1. 提取所有圖片 metadata
        stem_groups: Dict[str, Dict[str, Any]] = {}
        for f in image_paths:
            try:
                meta = self.extractor.extract(f)
                stem = Path(f).stem
                stem_groups[stem] = {"image": meta, "video": None}
            except Exception as e:
                print(f"[Warning] 無法解析圖片 {f}: {e}")

        # 2. 處理影片檔案：若為 Live Photo 伴隨短片則直接關聯，若是獨立影片才深度解析
        for f in video_paths:
            stem = Path(f).stem
            if stem in stem_groups and stem_groups[stem]["image"] is not None:
                # 這是同名 Live Photo MOV 短片
                img_meta = stem_groups[stem]["image"]
                stem_groups[stem]["video"] = {
                    "file_name": Path(f).name,
                    "file_path": str(Path(f).resolve()),
                    "duration": 2.5,
                    "width": img_meta.get("width", 0),
                    "height": img_meta.get("height", 0),
                    "is_video": True,
                    "is_image": False,
                    "caption": ""
                }
            else:
                # 獨立影片
                try:
                    meta = self.extractor.extract(f)
                    stem_groups[stem] = {"image": None, "video": meta}
                except Exception as e:
                    print(f"[Warning] 無法解析影片 {f}: {e}")

        # 3. 組合出統一的多媒體項目清單
        final_media_list: List[Dict[str, Any]] = []

        for stem, group in stem_groups.items():
            img_item = group["image"]
            vid_item = group["video"]

            if img_item and vid_item:
                # 成功配對為 Live Photo！
                # 繼承圖片的說明欄與資訊，附加影片作為 live_video
                live_item = dict(img_item)
                live_item["media_type"] = "live_photo"
                live_item["is_live_photo"] = True
                live_item["live_video"] = {
                    "file_name": vid_item["file_name"],
                    "file_path": vid_item["file_path"],
                    "duration": vid_item["duration"] or 1.5,
                    "width": vid_item["width"],
                    "height": vid_item["height"]
                }
                # 若圖片沒有 caption 但影片有，則補上
                if not live_item["caption"] and vid_item.get("caption"):
                    live_item["caption"] = vid_item["caption"]

                final_media_list.append(live_item)

            elif img_item and not vid_item:
                # 單純靜態照片
                img_item["media_type"] = "image"
                img_item["is_live_photo"] = False
                img_item["live_video"] = None
                final_media_list.append(img_item)

            elif not img_item and vid_item:
                # 獨立影片素材 (B-Roll / 普通影片)
                vid_item["media_type"] = "video"
                vid_item["is_live_photo"] = False
                vid_item["live_video"] = None
                final_media_list.append(vid_item)

        # 4. 逆地理編碼 (解析 GPS 為地名)
        if resolve_location:
            for item in final_media_list:
                if item.get("gps") and not item.get("location"):
                    lat = item["gps"]["latitude"]
                    lon = item["gps"]["longitude"]
                    loc_info = self.geocoder.reverse(lat, lon)
                    if loc_info:
                        item["location"] = loc_info
                    else:
                        item["location"] = {
                            "short_location": f"GPS ({lat}, {lon})",
                            "display_name": f"GPS ({lat}, {lon})"
                        }
                elif not item.get("location"):
                    item["location"] = None

        # 5. 依照拍攝時間排序 (由早到晚，建立時間軸故事線)
        final_media_list.sort(key=lambda x: str(x.get("creation_date") or ""))

        # 6. 編號 (Index)
        for idx, item in enumerate(final_media_list, start=1):
            item["index"] = idx

        return final_media_list
