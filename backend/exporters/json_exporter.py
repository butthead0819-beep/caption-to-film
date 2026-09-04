import json
from typing import Dict, Any


class JSONExporter:
    """
    匯出為標準 JSON 格式
    """
    def export(self, script_data: Dict[str, Any], output_path: str = None) -> str:
        content = json.dumps(script_data, ensure_ascii=False, indent=2)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        return content
