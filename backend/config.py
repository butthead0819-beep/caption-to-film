import os
from pathlib import Path
from pydantic import BaseModel


def _load_env_file():
    """自動從專案根目錄讀取 .env 檔案 (若存在)"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and not os.getenv(k):
                        os.environ[k] = v


_load_env_file()


class AppConfig(BaseModel):
    # Gemini API Key (可從環境變數 GEMINI_API_KEY 或 GOOGLE_API_KEY 或 .env 取得)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    
    # 預設視覺與文字生成模型
    vision_model: str = "gemini-2.5-flash"
    script_model: str = "gemini-2.5-flash"
    
    # 預設目標影片比例
    default_aspect_ratio: str = "16:9"  # 可選 "16:9", "9:16", "4:3", "1:1"
    
    # 支援的目標風格
    default_style: str = "自然感人旅行Vlog"
    
    # 暫存目錄
    temp_dir: str = str(Path(__file__).resolve().parent.parent / "scratch")


config = AppConfig()
