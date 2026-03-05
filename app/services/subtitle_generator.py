"""Subtitle generation service — creates styled ASS subtitles from script + audio timing."""
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Subtitle style definitions
# Each style produces an ASS [V4+ Styles] line.  Fields:
#   Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,
#   BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing,
#   Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR,
#   MarginV, Encoding
# Colours use &HAABBGGRR (ASS format — note BGR, not RGB).
#
# Size-dependent fields (Fontsize, Outline, Shadow, MarginL/R/V) are
# authored for a **1920 px reference height** and scaled at render time
# by _scale_ass_style() so subtitles look correct on every aspect ratio.
# ---------------------------------------------------------------------------

_REF_HEIGHT = 1920  # reference height the style values below target

SUBTITLE_STYLES: Dict[str, Dict[str, Any]] = {
    "clean": {
        "label": "Clean",
        "description": "White text, thin black outline, bottom-center",
        "ass_style": (
            "Style: Default,Arial,144,&H00FFFFFF,&H000000FF,"
            "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,2,60,60,500,1"
        ),
    },
    "bold_impact": {
        "label": "Bold Impact",
        "description": "Large uppercase white, thick black stroke, centered",
        "ass_style": (
            "Style: Default,Impact,186,&H00FFFFFF,&H000000FF,"
            "&H00000000,&HBE000000,-1,0,0,0,100,100,1,0,1,12,0,2,60,60,500,1"
        ),
        "force_upper": True,
    },
    "boxed": {
        "label": "Boxed",
        "description": "White text on semi-transparent dark box",
        "ass_style": (
            "Style: Default,Arial,138,&H00FFFFFF,&H000000FF,"
            "&H00000000,&HB4000000,0,0,0,0,100,100,0,0,3,0,12,2,60,60,500,1"
        ),
    },
    "karaoke": {
        "label": "Karaoke",
        "description": "Word-by-word highlight (yellow on white)",
        "ass_style": (
            "Style: Default,Arial,156,&H00FFFFFF,&H0000FFFF,"
            "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,0,2,60,60,500,1"
        ),
        "word_level": True,
    },
    "minimal": {
        "label": "Minimal",
        "description": "Small light gray text, bottom-left, no background",
        "ass_style": (
            "Style: Default,Helvetica,108,&H00CCCCCC,&H000000FF,"
            "&H00333333,&H00000000,0,0,0,0,100,100,0,0,1,3,0,1,60,60,500,1"
        ),
    },
    "neon_pop": {
        "label": "Neon Pop",
        "description": "Colored text with glow effect, centered",
        "ass_style": (
            "Style: Default,Arial Black,162,&H0000FFFF,&H000000FF,"
            "&H00FF00FF,&H80000000,-1,0,0,0,100,100,0,0,1,12,6,2,60,60,500,1"
        ),
    },
}


def _scale_ass_style(style_line: str, video_height: int) -> str:
    """Scale size-dependent ASS style fields from _REF_HEIGHT to *video_height*.

    ASS field order (after ``Style: Name,``):
      0  Fontname        6  BackColour     12 ScaleY    18 Alignment
      1  Fontsize  *     7  Bold           13 Spacing   19 MarginL  *
      2  PrimaryColour   8  Italic         14 Angle     20 MarginR  *
      3  SecondaryColour  9  Underline     15 BorderStyle 21 MarginV *
      4  OutlineColour   10 StrikeOut      16 Outline  *  22 Encoding
      5  BackColour      11 ScaleX         17 Shadow   *

    Fields marked * are scaled proportionally.
    """
    if video_height == _REF_HEIGHT:
        return style_line

    scale = video_height / _REF_HEIGHT

    prefix, fields_str = style_line.split(",", 1)  # "Style: Default" , rest
    fields = [f.strip() for f in fields_str.split(",")]

    # indices of size-dependent fields (0-based within fields list)
    # Fontsize=idx 1, Outline=idx 16, Shadow=idx 17, MarginL=19, MarginR=20, MarginV=21
    for idx in (1, 16, 17, 19, 20, 21):
        fields[idx] = str(max(1, round(int(fields[idx]) * scale)))

    return prefix + "," + ",".join(fields)


class SubtitleGenerator:
    """Generate ASS subtitle files from script text and audio timing."""

    def __init__(
        self,
        upload_folder: str = "./uploads",
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.upload_folder = upload_folder
        self.ffprobe_path = os.getenv("FFPROBE_PATH") or (
            ffmpeg_path.replace("ffmpeg", "ffprobe") if "ffmpeg" in ffmpeg_path else "ffprobe"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        script_text: str,
        audio_path: Optional[str],
        output_dir: str,
        style_name: str = "clean",
        video_width: int = 1080,
        video_height: int = 1920,
    ) -> Optional[str]:
        """Create an ASS subtitle file and return its absolute path.

        ``audio_path`` is the voiceover file used to determine total
        duration and (in the future) per-word timestamps.  When *None*
        a rough estimate based on word count is used.
        """
        if not script_text or not script_text.strip():
            return None

        style_def = SUBTITLE_STYLES.get(style_name, SUBTITLE_STYLES["clean"])

        total_duration = self._get_audio_duration(audio_path)
        if total_duration is None:
            # Estimate ~2.5 words per second
            total_duration = max(4.0, len(script_text.split()) / 2.5)

        sentences = self._split_sentences(script_text)
        if not sentences:
            return None

        timed = self._distribute_timing(sentences, total_duration)

        if style_def.get("word_level"):
            events = self._build_karaoke_events(timed, style_def)
        else:
            events = self._build_events(timed, style_def)

        scaled_style = _scale_ass_style(style_def["ass_style"], video_height)
        ass_content = self._render_ass(
            scaled_style, events, video_width, video_height
        )

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, "subtitles.ass")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(ass_content)
        return out_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Split script into display-friendly subtitle segments."""
        # Split on sentence-ending punctuation, keeping the punctuation
        raw = re.split(r'(?<=[.!?])\s+', text.strip())
        segments: List[str] = []
        for chunk in raw:
            chunk = chunk.strip()
            if not chunk:
                continue
            # If a segment is too long (>12 words), split further on commas
            words = chunk.split()
            if len(words) > 12:
                parts = re.split(r',\s*', chunk)
                for part in parts:
                    part = part.strip()
                    if part:
                        segments.append(part)
            else:
                segments.append(chunk)
        return segments

    @staticmethod
    def _distribute_timing(
        sentences: List[str], total_duration: float
    ) -> List[Tuple[str, float, float]]:
        """Assign start/end times proportionally by word count."""
        word_counts = [max(len(s.split()), 1) for s in sentences]
        total_words = sum(word_counts)

        # Leave a tiny buffer at start and end
        usable = total_duration * 0.96
        offset = total_duration * 0.02

        result: List[Tuple[str, float, float]] = []
        cursor = offset
        for sentence, wc in zip(sentences, word_counts):
            dur = usable * (wc / total_words)
            # Enforce a small gap between segments for readability
            end = cursor + dur - 0.05
            result.append((sentence, round(cursor, 2), round(end, 2)))
            cursor += dur
        return result

    @staticmethod
    def _format_ass_time(seconds: float) -> str:
        """Convert seconds to ASS timestamp H:MM:SS.cc"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    def _build_events(
        self,
        timed: List[Tuple[str, float, float]],
        style_def: Dict[str, Any],
    ) -> List[str]:
        lines: List[str] = []
        force_upper = style_def.get("force_upper", False)
        for text, start, end in timed:
            display_text = text.upper() if force_upper else text
            lines.append(
                f"Dialogue: 0,{self._format_ass_time(start)},"
                f"{self._format_ass_time(end)},Default,,0,0,0,,"
                f"{display_text}"
            )
        return lines

    def _build_karaoke_events(
        self,
        timed: List[Tuple[str, float, float]],
        style_def: Dict[str, Any],
    ) -> List[str]:
        """Build events with per-word karaoke highlight tags."""
        lines: List[str] = []
        for text, start, end in timed:
            words = text.split()
            if not words:
                continue
            seg_dur_cs = int((end - start) * 100)  # centiseconds
            per_word_cs = max(seg_dur_cs // len(words), 1)
            # Build karaoke override tags: {\kf<dur>}word
            tagged = "".join(f"{{\\kf{per_word_cs}}}{w} " for w in words).rstrip()
            lines.append(
                f"Dialogue: 0,{self._format_ass_time(start)},"
                f"{self._format_ass_time(end)},Default,,0,0,0,,"
                f"{tagged}"
            )
        return lines

    @staticmethod
    def _render_ass(
        style_line: str,
        events: List[str],
        width: int,
        height: int,
    ) -> str:
        return (
            "[Script Info]\n"
            "Title: Video Subtitles\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {width}\n"
            f"PlayResY: {height}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"{style_line}\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            + "\n".join(events)
            + "\n"
        )

    def _get_audio_duration(self, audio_path: Optional[str]) -> Optional[float]:
        if not audio_path:
            return None
        resolved = audio_path if os.path.isabs(audio_path) else os.path.join(
            self.upload_folder, audio_path
        )
        if not os.path.exists(resolved):
            return None
        try:
            result = subprocess.run(
                [
                    self.ffprobe_path, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    resolved,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True, text=True,
            )
            return round(float(result.stdout.strip()), 2)
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
            return None
