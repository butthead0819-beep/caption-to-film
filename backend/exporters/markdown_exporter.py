import os
from typing import Dict, Any, List


class MarkdownExporter:
    """
    將生成的結構化分鏡腳本轉換為格式美觀的 Markdown 報告
    """

    def export(self, script_data: Dict[str, Any], output_path: str = None) -> str:
        title = script_data.get("project_title", "影片剪輯腳本")
        subtitle = script_data.get("subtitle", "")
        logline = script_data.get("narrative_logline", "")
        theme = script_data.get("theme_summary", "")
        notes = script_data.get("director_notes", "")
        ratio = script_data.get("target_aspect_ratio", "16:9")
        total_duration = script_data.get("estimated_total_duration", 0)
        soundtrack = script_data.get("soundtrack_design", {})
        storyboard: List[Dict[str, Any]] = script_data.get("storyboard", [])

        md = []
        md.append(f"# 🎬 {title}")
        if subtitle:
            md.append(f"> **{subtitle}**\n")
        
        md.append("---")
        md.append("## 📋 企劃與故事大綱")
        if logline:
            md.append(f"- **故事一句話梗概 (Logline)**：{logline}")
        md.append(f"- **情感核心與故事弧線**：{theme}")
        md.append(f"- **目標比例**：`{ratio}`")
        try:
            total_sec = float(total_duration)
        except (ValueError, TypeError):
            total_sec = 0.0
        md.append(f"- **預估總片長**：約 `{total_duration}` 秒 ({int(total_sec // 60)}分 {int(total_sec % 60)}秒)")
        md.append(f"- **總鏡頭數**：共 `{len(storyboard)}` 個分鏡")
        if notes:
            md.append(f"- 🎬 **導演剪輯手記**：_{notes}_")
        md.append("")

        if soundtrack:
            md.append("## 🎵 聲音與配樂設計 (Audio & Music)")
            md.append(f"- **音樂風格氛圍**：{soundtrack.get('overall_mood', '未指定')}")
            md.append(f"- **推薦曲目/關鍵字**：`{soundtrack.get('recommended_tracks', '')}`")
            md.append(f"- **聲音動態起伏**：{soundtrack.get('audio_dynamics', '')}")
            md.append("")

        md.append("---")
        md.append("## 🎞️ 分鏡腳本與口白總表 (Storyboard & Voiceover)")
        md.append("")

        for shot in storyboard:
            idx = shot.get("shot_index", 1)
            scene_title = shot.get("scene_title", f"Shot {idx}")
            media_file = shot.get("media_file", "")
            media_type = shot.get("media_type", "image")
            is_live = shot.get("is_live_photo", False)
            dur = shot.get("duration_seconds", 3.0)
            shot_type = shot.get("shot_type", "")
            visual = shot.get("visual_description", "")
            crop_focus = shot.get("crop_focus", "")
            motion = shot.get("camera_motion", "")
            vo = shot.get("voiceover")
            bgm = shot.get("bgm_cue", "")
            sfx = shot.get("sfx_cue", "")
            trans = shot.get("transition", "Cut")
            live_advice = shot.get("live_photo_usage")

            type_badge = "📸 [Live Photo]" if is_live else ("🎥 [影片]" if media_type == "video" else "🖼️ [照片]")

            md.append(f"### #{idx:02d} {scene_title}")
            md.append(f"**素材檔案**：`{media_file}` {type_badge} ｜ **時長**：`{dur}s` ｜ **景別**：`{shot_type}` ｜ **轉場**：`{trans}`")
            md.append("")
            md.append(f"- 👁️ **畫面視覺與焦點**：{visual}")
            if crop_focus:
                md.append(f"- 📐 **裁切取景建議**：{crop_focus}")
            if motion:
                md.append(f"- 🎥 **鏡頭動態 (Ken Burns)**：`{motion}`")
            if is_live and live_advice:
                md.append(f"- 💫 **Live Photo 動態運用**：_{live_advice}_")
            
            md.append("")
            if vo and vo != "null" and not vo.startswith("[留白"):
                md.append(f"> 🎙️ **配音口白 (Voiceover)**：\n> \n> **{vo}**")
            else:
                md.append("> 🎙️ **配音口白 (Voiceover)**：_[留白／專注於現場環境音與配樂]_")
            
            md.append("")
            md.append(f"- 🎼 **配樂情緒**：{bgm}")
            if sfx:
                md.append(f"- 🔊 **音效 Cue 點**：`{sfx}`")
            md.append("\n---\n")

        md.append("💡 _本腳本由 AI 智慧影像腳本生成系統編譯，已自動融合 iOS 照片說明欄記憶與視覺構圖分析。_")

        content = "\n".join(md)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        return content
