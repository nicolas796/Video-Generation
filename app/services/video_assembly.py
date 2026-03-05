"""Final video assembly service using ffmpeg."""
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import cv2

from app import db
from app.models import UseCase, VideoClip, FinalVideo, Script


class FFmpegError(Exception):
    """Custom exception for ffmpeg errors with user-friendly messages."""
    
    def __init__(self, message: str, original_error: Optional[str] = None):
        self.original_error = original_error
        super().__init__(message)


class VideoAssembler:
    """Handle clip stitching, transitions, pacing, and audio overlay."""

    FORMAT_DIMENSIONS: Dict[str, Tuple[int, int]] = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350)
    }

    QUALITY_PRESETS = {
        "high": {"crf": 18, "preset": "slow"},
        "medium": {"crf": 22, "preset": "medium"},
        "low": {"crf": 27, "preset": "faster"}
    }

    SUPPORTED_TRANSITIONS = {"cut", "fade", "smooth"}

    def __init__(
        self,
        upload_folder: str = "./uploads",
        ffmpeg_path: str = "ffmpeg"
    ) -> None:
        self.upload_folder = upload_folder
        self.final_folder = os.path.join(upload_folder, "final")
        os.makedirs(self.final_folder, exist_ok=True)
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = os.getenv("FFPROBE_PATH") or (
            ffmpeg_path.replace("ffmpeg", "ffprobe") if "ffmpeg" in ffmpeg_path else "ffprobe"
        )

    # ------------------------------------------------------------------
    def assemble_use_case(
        self,
        use_case: UseCase,
        script: Optional[Script],
        audio_relative_path: Optional[str] = None,
        transition: str = "cut",
        quality: str = "medium",
        format_override: Optional[str] = None,
        transition_duration: float = 0.5,
        subtitle_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create the final video for a use case."""
        clips = VideoClip.query.filter_by(
            use_case_id=use_case.id,
            status="complete"
        ).order_by(VideoClip.sequence_order).all()

        # Also include ready clips (generated but not yet downloaded)
        ready_clips = VideoClip.query.filter_by(
            use_case_id=use_case.id,
            status="ready"
        ).order_by(VideoClip.sequence_order).all()
        clips = clips + ready_clips

        if not clips:
            return {"success": False, "error": "No complete clips available for assembly"}

        # Ensure each clip has a downloaded file that actually exists
        missing = []
        missing_with_paths = []
        for clip in clips:
            if not clip.file_path:
                missing.append(clip.id)
            else:
                clip_path = self._resolve_path(clip.file_path)
                if not os.path.exists(clip_path):
                    missing.append(clip.id)
                    missing_with_paths.append({
                        'clip_id': clip.id,
                        'file_path': clip.file_path,
                        'resolved_path': clip_path
                    })
        
        if missing:
            error_msg = f"Clips missing files: {missing}"
            if missing_with_paths:
                error_msg += f". Files not found at resolved paths: {missing_with_paths}"
            return {
                "success": False,
                "error": error_msg
            }

        audio_path = None
        if audio_relative_path:
            audio_path = self._resolve_path(audio_relative_path)
            if not os.path.exists(audio_path):
                return {"success": False, "error": "Voiceover audio not found"}

        format_key = (format_override or use_case.format or "9:16").strip()
        width, height = self.FORMAT_DIMENSIONS.get(format_key, (1080, 1920))
        quality_preset = self.QUALITY_PRESETS.get(quality, self.QUALITY_PRESETS["medium"])
        transition_mode = transition if transition in self.SUPPORTED_TRANSITIONS else "cut"

        tmp_dir = tempfile.mkdtemp(prefix="assembly_")
        try:
            normalized = []
            for clip in clips:
                clip_path = self._resolve_path(clip.file_path)
                normalized.append(
                    self._normalize_clip(
                        clip_path=clip_path,
                        width=width,
                        height=height,
                        tmp_dir=tmp_dir
                    )
                )

            if transition_mode == "cut" or len(normalized) == 1:
                stitched_path = self._concat_with_cuts(normalized, tmp_dir, quality_preset)
            else:
                stitched_path = self._concat_with_transitions(
                    normalized,
                    tmp_dir,
                    transition_mode,
                    transition_duration,
                    quality_preset
                )

            # Match pacing to voiceover (if audio exists)
            audio_duration = self._probe_duration(audio_path) if audio_path else None
            video_duration = self._probe_duration(stitched_path)
            paced_path = stitched_path
            if audio_duration and video_duration and video_duration > 0:
                ratio = audio_duration / video_duration
                if abs(1 - ratio) > 0.02:  # only adjust when off by >2%
                    paced_path = os.path.join(tmp_dir, "paced_video.mp4")
                    self._run_ffmpeg([
                        self.ffmpeg_path,
                        "-y",
                        "-i", stitched_path,
                        "-filter:v", f"setpts={ratio}*PTS",
                        "-an",
                        "-r", "30",
                        "-c:v", "libx264",
                        "-preset", quality_preset["preset"],
                        "-crf", str(quality_preset["crf"]),
                        paced_path
                    ])
                    video_duration = audio_duration

            # ── Burn subtitles (optional) ─────────────────────────
            subtitle_ass_path = None
            if subtitle_style and script and script.content:
                from app.services.subtitle_generator import SubtitleGenerator
                sub_gen = SubtitleGenerator(
                    upload_folder=self.upload_folder,
                    ffmpeg_path=self.ffmpeg_path,
                )
                subtitle_ass_path = sub_gen.generate(
                    script_text=script.content,
                    audio_path=audio_path,
                    output_dir=tmp_dir,
                    style_name=subtitle_style,
                    video_width=width,
                    video_height=height,
                )

            if subtitle_ass_path and os.path.exists(subtitle_ass_path):
                subtitled_path = os.path.join(tmp_dir, "subtitled_video.mp4")
                self._burn_subtitles(
                    paced_path, subtitle_ass_path, subtitled_path, quality_preset
                )
                paced_path = subtitled_path

            # Overlay audio and finalize (or just finalize without audio)
            use_case_folder = os.path.join(self.final_folder, str(use_case.id))
            os.makedirs(use_case_folder, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            final_filename = f"final_{timestamp}.mp4"
            final_path = os.path.join(use_case_folder, final_filename)

            if audio_path:
                # With voiceover audio
                self._run_ffmpeg([
                    self.ffmpeg_path,
                    "-y",
                    "-i", paced_path,
                    "-i", audio_path,
                    "-c:v", "libx264",
                    "-preset", quality_preset["preset"],
                    "-crf", str(quality_preset["crf"]),
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    final_path
                ])
            else:
                # Video only (no voiceover)
                self._run_ffmpeg([
                    self.ffmpeg_path,
                    "-y",
                    "-i", paced_path,
                    "-c:v", "libx264",
                    "-preset", quality_preset["preset"],
                    "-crf", str(quality_preset["crf"]),
                    "-an",  # No audio
                    "-movflags", "+faststart",
                    final_path
                ])

            rel_path = os.path.relpath(final_path, self.upload_folder)
            thumbnail_rel = self._generate_thumbnail(final_path, use_case.id)
            file_size = os.path.getsize(final_path)
            duration = audio_duration or video_duration

            final_video = FinalVideo(
                use_case_id=use_case.id,
                brand_id=use_case.brand_id,
                script_id=script.id if script else None,
                file_path=rel_path,
                thumbnail_path=thumbnail_rel,
                voiceover_path=os.path.relpath(audio_path, self.upload_folder) if audio_path else None,
                duration=duration,
                resolution=f"{width}x{height}",
                file_size=file_size,
                clip_ids=[clip.id for clip in clips],
                assembly_settings={
                    "transition": transition_mode,
                    "quality": quality,
                    "format": format_key,
                    "transition_duration": transition_duration,
                    "video_duration": video_duration,
                    "audio_duration": audio_duration,
                    "subtitle_style": subtitle_style,
                },
                status="complete",
                completed_at=datetime.utcnow()
            )
            db.session.add(final_video)
            db.session.commit()

            video_dict = final_video.to_dict()
            video_dict["video_url"] = f"/uploads/{rel_path}"
            video_dict["thumbnail_url"] = f"/uploads/{thumbnail_rel}" if thumbnail_rel else None

            return {
                "success": True,
                "final_video": video_dict
            }
        except FFmpegError as exc:
            db.session.rollback()
            return {
                "success": False, 
                "error": str(exc),
                "error_type": "ffmpeg_error",
                "original_error": exc.original_error
            }
        except subprocess.CalledProcessError as exc:
            db.session.rollback()
            error_msg = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
            return {
                "success": False, 
                "error": f"Video processing failed: {error_msg[:200]}",
                "error_type": "processing_error"
            }
        except Exception as exc:
            db.session.rollback()
            return {
                "success": False, 
                "error": f"An unexpected error occurred during assembly: {str(exc)}",
                "error_type": "unexpected"
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _burn_subtitles(
        self,
        video_path: str,
        ass_path: str,
        output_path: str,
        quality_preset: Dict[str, Any],
    ) -> None:
        """Hard-burn an ASS subtitle file onto the video."""
        # The ass filter needs the path with escaped colons/backslashes
        escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:")
        self._run_ffmpeg(
            [
                self.ffmpeg_path, "-y",
                "-i", video_path,
                "-vf", f"ass='{escaped}'",
                "-c:v", "libx264",
                "-preset", quality_preset["preset"],
                "-crf", str(quality_preset["crf"]),
                "-an",
                output_path,
            ],
            description="burning subtitles",
        )

    # ------------------------------------------------------------------
    def _normalize_clip(self, clip_path: str, width: int, height: int, tmp_dir: str) -> str:
        normalized = os.path.join(tmp_dir, f"clip_{uuid.uuid4().hex[:8]}_norm.mp4")
        # Use scale then crop for 'cover' behavior (fill frame, crop excess)
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
        self._run_ffmpeg([
            self.ffmpeg_path,
            "-y",
            "-i", clip_path,
            "-vf", vf,
            "-r", "30",
            "-an",
            "-c:v", "libx264",
            "-threads", "1",
            "-preset", "fast",
            "-crf", "20",
            normalized
        ])
        return normalized

    def _concat_with_cuts(self, clips: List[str], tmp_dir: str, quality: Dict[str, str]) -> str:
        output = os.path.join(tmp_dir, "concat_cut.mp4")
        # Use concat demuxer with stream copy — clips are already normalized to
        # the same resolution/codec/framerate, so no re-encoding needed.
        # This uses almost zero memory compared to filter_complex or re-encode.
        list_path = os.path.join(tmp_dir, "concat_list.txt")
        with open(list_path, "w") as f:
            for clip in clips:
                f.write(f"file '{clip}'\n")
        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output
        ]
        self._run_ffmpeg(cmd)
        return output

    def _concat_with_transitions(
        self,
        clips: List[str],
        tmp_dir: str,
        transition: str,
        duration: float,
        quality: Dict[str, str]
    ) -> str:
        current = clips[0]
        transition_name = "fade" if transition == "fade" else "smoothleft"

        for index in range(1, len(clips)):
            next_clip = clips[index]
            current_duration = self._probe_duration(current) or duration
            offset = max(current_duration - duration, 0.001)
            output = os.path.join(tmp_dir, f"xfade_{index}.mp4")
            filter_complex = (
                f"[0:v][1:v]xfade=transition={transition_name}:duration={duration}:offset={offset},"
                f"format=yuv420p[vout]"
            )
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-i", current,
                "-i", next_clip,
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-c:v", "libx264",
                "-threads", "1",
                "-preset", quality["preset"],
                "-crf", str(quality["crf"]),
                output
            ]
            self._run_ffmpeg(cmd)
            current = output
        return current

    def _generate_thumbnail(self, video_path: str, use_case_id: int) -> Optional[str]:
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return None
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            target = max(total_frames // 2, 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            success, frame = cap.read()
            cap.release()
            if not success:
                return None

            folder = os.path.join(self.final_folder, str(use_case_id), "thumbnails")
            os.makedirs(folder, exist_ok=True)
            filename = f"final_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jpg"
            thumb_path = os.path.join(folder, filename)
            cv2.imwrite(thumb_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            return os.path.relpath(thumb_path, self.upload_folder)
        except Exception:
            return None

    def _resolve_path(self, maybe_relative: str) -> str:
        if os.path.isabs(maybe_relative):
            return maybe_relative
        resolved = os.path.join(self.upload_folder, maybe_relative)
        # Guard against doubled 'uploads/' segments (e.g. /var/data/uploads/uploads/clips/...)
        doubled = os.path.join(self.upload_folder, 'uploads')
        if resolved.startswith(doubled + os.sep):
            fixed = os.path.join(self.upload_folder, resolved[len(doubled) + 1:])
            if os.path.exists(fixed):
                return fixed
        return resolved

    def _probe_duration(self, media_path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_path,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    media_path
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                text=True
            )
            return round(float(result.stdout.strip()), 2)
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
            return None

    def _run_ffmpeg(self, cmd: List[str], description: str = "ffmpeg operation") -> None:
        """Run ffmpeg command with proper error handling."""
        try:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or "Unknown ffmpeg error"
            # Provide user-friendly error messages for common issues
            if "No such file or directory" in stderr:
                raise FFmpegError(
                    f"{description} failed: Input file not found. Please check that all video clips exist.",
                    original_error=stderr
                ) from e
            elif "Invalid data found when processing input" in stderr:
                raise FFmpegError(
                    f"{description} failed: Invalid video file. One or more clips may be corrupted.",
                    original_error=stderr
                ) from e
            elif "Decoder not found" in stderr or "Codec not found" in stderr:
                raise FFmpegError(
                    f"{description} failed: Unsupported video codec. Please try regenerating the clips.",
                    original_error=stderr
                ) from e
            elif "Cannot allocate memory" in stderr:
                raise FFmpegError(
                    f"{description} failed: Out of memory. Try reducing video quality or closing other applications.",
                    original_error=stderr
                ) from e
            else:
                raise FFmpegError(
                    f"{description} failed: {stderr[:200]}",
                    original_error=stderr
                ) from e
        except FileNotFoundError:
            raise FFmpegError(
                f"ffmpeg not found. Please ensure ffmpeg is installed and in your PATH.",
                original_error="ffmpeg executable not found"
            )
