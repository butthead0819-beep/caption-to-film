import os
import sys
import io
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

from .config import config
from .extractors.metadata_extractor import MetadataExtractor
from .extractors.geocoding import ReverseGeocoder
from .extractors.livephoto_matcher import LivePhotoMatcher
from .extractors.apple_photos_extractor import ApplePhotosExtractor
from .analyzers.vision_analyzer import VisionAnalyzer
from .engines.script_engine import ScriptEngine
from .exporters.markdown_exporter import MarkdownExporter
from .exporters.json_exporter import JSONExporter
from .exporters.fcpxml_exporter import FCPXMLExporter

app = FastAPI(
    title="iMovie Script & Storyboard Generator",
    description="智慧 iOS 照片說明欄讀取、Live Photo 整合、取景裁切建議與分鏡腳本生成系統",
    version="1.1.0"
)

# CORS 支援
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 實例化各模組
metadata_extractor = MetadataExtractor()
geocoder = ReverseGeocoder()
matcher = LivePhotoMatcher(metadata_extractor=metadata_extractor, geocoder=geocoder)
apple_photos_extractor = ApplePhotosExtractor(geocoder=geocoder)
vision_analyzer = VisionAnalyzer()
script_engine = ScriptEngine()
md_exporter = MarkdownExporter()
json_exporter = JSONExporter()
fcpxml_exporter = FCPXMLExporter()

# 確保暫存目錄存在
scratch_dir = Path(config.temp_dir)
scratch_dir.mkdir(parents=True, exist_ok=True)
uploads_dir = scratch_dir / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)


# Models
class ScanFolderRequest(BaseModel):
    folder_path: str
    resolve_location: bool = True


class ScanApplePhotosRequest(BaseModel):
    album_name: str
    max_photos: int = 150
    resolve_location: bool = True


class AnalyzeRequest(BaseModel):
    items: List[Dict[str, Any]]
    target_aspect_ratio: str = "16:9"


class GenerateScriptRequest(BaseModel):
    items_with_analysis: List[Dict[str, Any]]
    user_prompt: str
    target_duration: Optional[int] = None
    style: str = "自然感人旅行Vlog"
    target_aspect_ratio: str = "16:9"
    story_context: Optional[str] = None


class ExportRequest(BaseModel):
    script_data: Dict[str, Any]
    format: str = "markdown"  # "markdown", "json", "fcpxml"


@app.post("/api/scan-folder")
def scan_folder(req: ScanFolderRequest):
    """掃描指定資料夾內的所有相片與影片，自動提取說明欄、GPS與配對 Live Photo"""
    folder = Path(req.folder_path)
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"找不到指定的資料夾: {req.folder_path}")

    try:
        items = matcher.process_directory(str(folder), resolve_location=req.resolve_location)
        
        # 自動搜尋是否有旅程心得文件 (*心得*.md, *reflection*.md 等)
        detected_reflection = None
        for p in folder.rglob("*"):
            if p.is_file() and any(k in p.name.lower() for k in ("心得", "reflection", "journal", "story", "感想")) and p.suffix.lower() in (".md", ".txt"):
                try:
                    with open(p, "r", encoding="utf-8") as rf:
                        detected_reflection = rf.read().strip()
                        if detected_reflection:
                            break
                except Exception:
                    pass

        return {
            "success": True,
            "count": len(items),
            "folder_path": str(folder.resolve()),
            "detected_reflection": detected_reflection,
            "items": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"掃描失敗: {str(e)}")


@app.get("/api/apple-photos/status")
def apple_photos_status():
    """檢查 Apple Photos 直讀模組是否可用"""
    available = apple_photos_extractor.is_available()
    return {
        "available": available,
        "platform": sys.platform if hasattr(sys, 'platform') else "unknown"
    }


@app.get("/api/apple-photos/albums")
def list_apple_photos_albums():
    """列舉 macOS 照片 App 中的所有相簿"""
    if not apple_photos_extractor.is_available():
        raise HTTPException(status_code=400, detail="當前環境不支援直接存取 Apple Photos（需要 macOS 及本機照片資料庫權限）")
    try:
        albums = apple_photos_extractor.list_albums()
        return {
            "success": True,
            "albums": albums
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取 Apple 相簿失敗: {str(e)}")


@app.post("/api/apple-photos/scan-album")
def scan_apple_photos_album(req: ScanApplePhotosRequest):
    """掃描指定 Apple Photos 相簿，提取照片、說明欄、GPS與配對 Live Photo 影片"""
    if not apple_photos_extractor.is_available():
        raise HTTPException(status_code=400, detail="當前環境不支援直接存取 Apple Photos")
    try:
        items = apple_photos_extractor.scan_album(
            album_name=req.album_name,
            max_photos=req.max_photos,
            resolve_location=req.resolve_location
        )
        return {
            "success": True,
            "album_name": req.album_name,
            "count": len(items),
            "items": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"掃描 Apple 相簿失敗: {str(e)}")


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """支援直接從瀏覽器上傳照片/影片到暫存庫進行分析"""
    saved_paths = []
    for f in files:
        file_path = uploads_dir / f.filename
        content = await f.read()
        with open(file_path, "wb") as buffer:
            buffer.write(content)
        saved_paths.append(str(file_path))

    items = matcher.process_files(saved_paths, resolve_location=True)
    return {
        "success": True,
        "count": len(items),
        "items": items
    }


@app.post("/api/analyze-items")
def analyze_items(req: AnalyzeRequest):
    """對各照片/影片進行視覺焦點、構圖主體與裁切建議 (Crop Advisor) 分析"""
    analyzed_items = []
    for item in req.items:
        try:
            analysis = vision_analyzer.analyze_media(item, target_aspect_ratio=req.target_aspect_ratio)
            item_copy = dict(item)
            item_copy["analysis"] = analysis
            analyzed_items.append(item_copy)
        except Exception as e:
            item_copy = dict(item)
            item_copy["analysis"] = {"error": str(e)}
            analyzed_items.append(item_copy)

    return {
        "success": True,
        "items": analyzed_items
    }


@app.post("/api/generate-script")
def generate_script(req: GenerateScriptRequest):
    """彙整素材記憶與視覺分析，依使用者 Prompt 編譯產生分鏡腳本"""
    try:
        script = script_engine.generate_script(
            media_items_with_analysis=req.items_with_analysis,
            user_prompt=req.user_prompt,
            target_duration_seconds=req.target_duration,
            style=req.style,
            target_aspect_ratio=req.target_aspect_ratio,
            story_context=req.story_context
        )
        return {
            "success": True,
            "script": script
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"腳本生成失敗: {str(e)}")


@app.post("/api/export")
def export_script(req: ExportRequest):
    """匯出 Markdown, JSON 或 FCPXML"""
    fmt = req.format.lower()
    script_data = req.script_data

    if fmt == "markdown":
        content = md_exporter.export(script_data)
        return Response(content=content, media_type="text/markdown")
    elif fmt == "json":
        content = json_exporter.export(script_data)
        return Response(content=content, media_type="application/json")
    elif fmt == "fcpxml":
        content = fcpxml_exporter.export(script_data)
        return Response(content=content, media_type="application/xml")
    else:
        raise HTTPException(status_code=400, detail=f"不支援的匯出格式: {fmt}")


@app.get("/api/thumbnail")
def get_thumbnail(path: str = Query(..., description="檔案的絕對路徑"), size: int = Query(400, description="縮圖最大長邊")):
    """動態產生並回傳縮圖，支援 HEIC/JPG/PNG"""
    decoded_path = urllib.parse.unquote(path)
    file_path = Path(decoded_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="檔案不存在")

    try:
        with Image.open(file_path) as img:
            # 轉換為 RGB
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            buf.seek(0)
            return StreamingResponse(buf, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"無法生成縮圖: {str(e)}")


# 掛載前端靜態目錄
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
