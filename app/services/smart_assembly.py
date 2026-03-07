"""Smart video assembly with AI-powered clip selection and trimming."""
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app import db
from app.models import UseCase, VideoClip, FinalVideo, Script
from app.services.video_assembly import VideoAssembler, FFmpegError


class SmartVideoAssembler(VideoAssembler):
    """Enhanced assembler with AI-powered clip selection and intelligent trimming."""
    
    def assemble_use_case_smart(
        self,
        use_case: UseCase,
        script: Optional[Script],
        audio_relative_path: Optional[str] = None,
        transition: str = "cut",
        quality: str = "medium",
        format_override: Optional[str] = None,
        transition_duration: float = 0.5,
        target_duration: Optional[float] = None,
        max_clips: Optional[int] = None,  # No longer enforced as a strict limit
        min_clips: int = 1,
        subtitle_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create final video with intelligent clip selection and trimming.
        
        Instead of using all clips, the AI analyzes and selects the best segments
        to fit the target duration while maintaining narrative flow.
        """
        # Get all complete or ready clips
        clips = VideoClip.query.filter(
            VideoClip.use_case_id == use_case.id,
            VideoClip.status.in_(["complete", "ready"])
        ).order_by(VideoClip.sequence_order).all()

        if not clips:
            return {"success": False, "error": "No complete clips available for assembly"}

        # Calculate total available content
        total_available_duration = sum((clip.duration or 5) for clip in clips)
        target = target_duration or use_case.duration_target or 30
        
        # Determine assembly strategy based on content vs target
        if total_available_duration <= target * 1.1:  # Within 10% of target
            strategy = "use_all"
            selected_clips = clips
        else:
            strategy = "select_and_trim"
            selected_clips = self._intelligent_clip_selection(clips, target)
        
        # Ensure each selected clip has a downloaded file that actually exists
        missing = []
        missing_with_paths = []
        for clip in selected_clips:
            if not clip.file_path:
                missing.append(clip.id)
            else:
                clip_path = self._resolve_path(clip.file_path)
                if not os.path.exists(clip_path):
                    missing.append(clip.id)
                    missing_with_paths.append({
                        'clip_id': clip.id,
                        'file_path': clip.file_path,
                        'resolved_path': clip_path,
                        'upload_folder': self.upload_folder,
                        'pollo_job_id': clip.pollo_job_id
                    })

        if missing:
            error_msg = f"Selected clips missing files: {missing}"
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

        tmp_dir = tempfile.mkdtemp(prefix="smart_assembly_")
        try:
            # Process clips with potential trimming
            processed = []
            for clip in selected_clips:
                clip_path = self._resolve_path(clip.file_path)
                
                # Determine if trimming is needed
                clip_duration = clip.duration or 5
                if strategy == "select_and_trim" and len(selected_clips) > 1:
                    # Calculate target duration for this clip based on content importance
                    target_clip_duration = self._calculate_clip_target_duration(
                        clip, selected_clips, target
                    )
                    
                    if target_clip_duration < clip_duration * 0.9:  # Trim if significantly shorter
                        processed.append(self._trim_clip(
                            clip_path=clip_path,
                            target_duration=target_clip_duration,
                            width=width,
                            height=height,
                            tmp_dir=tmp_dir,
                            quality_preset=quality_preset,
                            clip_analysis=clip.analysis_metadata
                        ))
                    else:
                        # Use full clip
                        processed.append(self._normalize_clip(
                            clip_path=clip_path,
                            width=width,
                            height=height,
                            tmp_dir=tmp_dir
                        ))
                else:
                    # Use full clip without trimming
                    processed.append(self._normalize_clip(
                        clip_path=clip_path,
                        width=width,
                        height=height,
                        tmp_dir=tmp_dir
                    ))

            if not processed:
                return {"success": False, "error": "No clips could be processed"}

            # Concatenate clips
            if transition_mode == "cut" or len(processed) == 1:
                stitched_path = self._concat_with_cuts(processed, tmp_dir, quality_preset)
            else:
                stitched_path = self._concat_with_transitions(
                    processed,
                    tmp_dir,
                    transition_mode,
                    transition_duration,
                    quality_preset
                )

            # Free memory: delete intermediate normalized clips now that concat is done
            for p in processed:
                if p != stitched_path and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

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
                        "-threads", "1",
                        "-preset", "ultrafast",
                        "-crf", str(quality_preset["crf"]),
                        paced_path
                    ])
                    video_duration = audio_duration
                    # Free stitched file now that pacing is done
                    if os.path.exists(stitched_path):
                        try:
                            os.remove(stitched_path)
                        except OSError:
                            pass

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
                # With voiceover audio — need to re-encode since subtitles were burned
                video_codec = "copy" if not subtitle_ass_path else "libx264"
                cmd = [
                    self.ffmpeg_path,
                    "-y",
                    "-i", paced_path,
                    "-i", audio_path,
                    "-c:v", video_codec,
                ]
                if video_codec == "libx264":
                    cmd.extend([
                        "-preset", quality_preset["preset"],
                        "-crf", str(quality_preset["crf"]),
                    ])
                cmd.extend([
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    final_path
                ])
                self._run_ffmpeg(cmd)
            else:
                # Video only
                video_codec = "copy" if not subtitle_ass_path else "libx264"
                cmd = [
                    self.ffmpeg_path,
                    "-y",
                    "-i", paced_path,
                    "-c:v", video_codec,
                ]
                if video_codec == "libx264":
                    cmd.extend([
                        "-preset", quality_preset["preset"],
                        "-crf", str(quality_preset["crf"]),
                    ])
                cmd.extend([
                    "-an",
                    "-movflags", "+faststart",
                    final_path
                ])
                self._run_ffmpeg(cmd)

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
                clip_ids=[clip.id for clip in selected_clips],
                assembly_settings={
                    "transition": transition_mode,
                    "quality": quality,
                    "format": format_key,
                    "transition_duration": transition_duration,
                    "video_duration": video_duration,
                    "audio_duration": audio_duration,
                    "subtitle_style": subtitle_style,
                    "strategy": strategy,
                    "clips_used": len(selected_clips),
                    "clips_available": len(clips),
                    "total_available_duration": total_available_duration
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
                "final_video": video_dict,
                "assembly_info": {
                    "strategy": strategy,
                    "clips_used": len(selected_clips),
                    "clips_available": len(clips),
                    "total_available_duration": total_available_duration,
                    "target_duration": target,
                    "final_duration": duration
                }
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

    def _intelligent_clip_selection(
        self,
        clips: List[VideoClip],
        target_duration: float
    ) -> List[VideoClip]:
        """Select the best clips to fit target duration based on analysis."""
        if not clips:
            return []
        
        # Score clips based on analysis quality
        scored_clips = []
        for clip in clips:
            score = self._calculate_clip_quality_score(clip)
            scored_clips.append((clip, score))
        
        # Sort by score (highest first)
        scored_clips.sort(key=lambda x: x[1], reverse=True)
        
        # Select clips until we have enough duration
        selected = []
        current_duration = 0
        min_clip_duration = 3  # Minimum 3 seconds per clip
        
        for clip, score in scored_clips:
            clip_duration = clip.duration or 5
            
            # Add clip if it helps reach target
            if current_duration < target_duration:
                selected.append(clip)
                current_duration += max(clip_duration * 0.7, min_clip_duration)  # Assume some trimming
            
            # Stop if we have enough content (with buffer for transitions)
            if current_duration >= target_duration * 1.2:
                break
        
        # Sort selected clips back to original sequence order
        selected.sort(key=lambda c: c.sequence_order or 0)
        
        return selected

    def _calculate_clip_quality_score(self, clip: VideoClip) -> float:
        """Calculate a quality score for clip selection (0-100)."""
        score = 50.0  # Base score
        
        # Bonus for having analysis
        if clip.content_description:
            score += 20
        
        # Bonus for having tags
        if clip.tags and len(clip.tags) > 0:
            score += 10 + min(len(clip.tags) * 2, 10)  # Up to 10 more points
        
        # Bonus for analysis metadata
        if clip.analysis_metadata:
            score += 10
        
        # Prefer clips with reasonable duration (not too short, not too long)
        duration = clip.duration or 5
        if 3 <= duration <= 8:
            score += 10  # Sweet spot duration
        elif duration < 2:
            score -= 10  # Too short
        elif duration > 15:
            score -= 5   # Might need heavy trimming
        
        return max(0, min(100, score))

    def _calculate_clip_target_duration(
        self,
        clip: VideoClip,
        all_clips: List[VideoClip],
        target_total: float
    ) -> float:
        """Calculate how long this clip should be in the final video."""
        clip_duration = clip.duration or 5
        
        # Calculate total quality-weighted duration
        total_score = sum(self._calculate_clip_quality_score(c) for c in all_clips)
        clip_score = self._calculate_clip_quality_score(clip)
        
        if total_score > 0:
            # Allocate duration proportionally based on quality score
            proportion = clip_score / total_score
            target = target_total * proportion
            
            # Clamp to reasonable values
            return max(2, min(target, clip_duration * 0.9))  # Trim at least 10% if selecting
        
        return clip_duration

    def _trim_clip(
        self,
        clip_path: str,
        target_duration: float,
        width: int,
        height: int,
        tmp_dir: str,
        quality_preset: Dict[str, str],
        clip_analysis: Optional[Dict] = None
    ) -> str:
        """Trim a clip to target duration, selecting the best segment."""
        output = os.path.join(tmp_dir, f"trimmed_{uuid.uuid4().hex[:8]}.mp4")
        
        # Get actual clip duration
        actual_duration = self._probe_duration(clip_path) or target_duration
        
        if actual_duration <= target_duration:
            # No trimming needed
            return self._normalize_clip(clip_path, width, height, tmp_dir)
        
        # Calculate trim points
        # For now, trim from the middle to get the best content
        # In the future, this could use clip_analysis to find the best segment
        excess = actual_duration - target_duration
        start_time = excess / 2
        
        # Use scale then crop for 'cover' behavior
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
        
        self._run_ffmpeg([
            self.ffmpeg_path,
            "-y",
            "-ss", str(start_time),
            "-t", str(target_duration),
            "-i", clip_path,
            "-vf", vf,
            "-r", "30",
            "-an",
            "-c:v", "libx264",
            "-threads", "1",
            "-preset", quality_preset["preset"],
            "-crf", str(quality_preset["crf"]),
            output
        ])
        
        return output
