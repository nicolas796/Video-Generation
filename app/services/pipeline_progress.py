"""Track and recover pipeline progress for use cases."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from app import db
from app.models import FinalVideo, Product, Script, UseCase, VideoClip, Hook


def build_hook_script_payload(hook: Optional[Hook]) -> Optional[Dict[str, Any]]:
    """Return a normalized payload for feeding hook data into script generation."""

    if not hook or hook.winning_variant_index is None:
        return None

    variants = hook.variants or []
    if not isinstance(variants, list):
        return None

    try:
        variant = variants[hook.winning_variant_index]
    except (IndexError, TypeError):
        return None

    if not isinstance(variant, dict):
        return None

    return {
        "id": hook.id,
        "hook_type": hook.hook_type,
        "status": hook.status,
        "variant_index": hook.winning_variant_index,
        "variant": variant,
        "image_paths": hook.image_paths or [],
        "audio_path": hook.audio_path,
        "video_path": hook.video_path,
    }
from app.scrapers import scrape_product  # noqa: F401 (used for recovery diagnostics)
from app.services.script_gen import ScriptGenerator
from app.services.video_clip_manager import VideoClipManager
from app.services.voiceover import VoiceoverGenerator
from app.services.video_assembly import VideoAssembler
from app.services.clip_analyzer import ClipAnalyzer
from app.utils import retry_operation


class PipelineProgressTracker:
    """Persisted runtime view of where a use case is in the pipeline."""

    STAGES = OrderedDict(
        [
            ("use_case", "Use Case"),
            ("hook", "Hook"),
            ("script", "Script"),
            ("clips", "Clip Generation"),
            ("analysis", "Clip Analysis"),
            ("assembly", "Assembly"),
            ("final_output", "Final Output"),
        ]
    )

    @classmethod
    def _default_payload(cls, status: str = "pending", message: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "meta": meta or {},
            "updated_at": datetime.utcnow().isoformat(),
        }

    @classmethod
    def ensure_state(cls, use_case: UseCase) -> Dict[str, Any]:
        state = dict(use_case.pipeline_state or {})
        changed = False
        for key in cls.STAGES:
            if key not in state:
                state[key] = cls._default_payload()
                changed = True
        if changed:
            use_case.pipeline_state = state
            db.session.commit()
        return state

    @classmethod
    def update_stage(
        cls,
        use_case: UseCase,
        stage: str,
        status: str,
        *,
        message: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> Dict[str, Any]:
        if stage not in cls.STAGES:
            raise ValueError(f"Unknown pipeline stage '{stage}'")
        state = cls.ensure_state(use_case)
        state[stage] = cls._default_payload(status, message, meta)
        use_case.pipeline_state = state
        if commit:
            db.session.commit()
        return state[stage]

    @classmethod
    def summarize(cls, use_case: UseCase) -> Dict[str, Any]:
        """Return a computed view of pipeline health for the provided use case."""

        state = cls.ensure_state(use_case)
        changed = False

        # Use case / spec stage
        use_case_status = (use_case.status or '').lower()
        spec_ready = bool(use_case.voice_id and use_case.target_audience)
        status_label = use_case.status or 'in-progress'
        if spec_ready and use_case_status not in ('draft', ''):
            use_case_payload = cls._default_payload('complete', 'Use case configured', meta={'status': status_label})
        elif use_case_status not in ('draft', '', None):
            use_case_payload = cls._default_payload('running', f'Status: {status_label}', meta={'status': status_label})
        else:
            use_case_payload = cls._default_payload('pending', 'Select voice & duration', meta={'status': status_label})
        if state.get('use_case') != use_case_payload:
            state['use_case'] = use_case_payload
            changed = True

        hook = getattr(use_case, 'hook', None)
        hook_payload = cls._default_payload('pending', 'Hook not started', meta={})
        if hook:
            hook_status = (hook.status or '').lower()
            message = None
            status_value = 'pending'
            if hook_status == 'failed':
                status_value = 'error'
                message = hook.error_message or 'Hook previews failed'
            elif hook_status == 'generating':
                status_value = 'running'
                message = 'Generating hook variants'
            elif not (hook.image_paths or []):
                status_value = 'running'
                message = 'Generating preview assets'
            elif hook.winning_variant_index is None:
                status_value = 'pending'
                message = 'Select winning variant'
            else:
                if hook_status == 'animating':
                    status_value = 'running'
                    message = 'Hook animation in progress'
                elif hook_status in ('complete', 'preview_ready', 'ready_for_animation'):
                    status_value = 'complete'
                    message = 'Hook animated' if hook_status == 'complete' else 'Hook selected'
                else:
                    status_value = hook_status or 'running'
                    message = 'Hook selected'
            hook_payload = cls._default_payload(status_value, message, meta={
                'hook_id': hook.id,
                'hook_status': hook.status,
                'winning_variant_index': hook.winning_variant_index,
            })
        if state.get('hook') != hook_payload:
            state['hook'] = hook_payload
            changed = True

        script = Script.query.filter_by(use_case_id=use_case.id).first()
        script_status = "pending"
        if script:
            if script.status == "approved":
                script_status = "complete"
            elif script.status == "generated":
                script_status = "awaiting_approval"
            else:
                script_status = script.status or "draft"
        state_payload = cls._default_payload(script_status, meta={"script_id": script.id if script else None})
        if state.get("script") != state_payload:
            state["script"] = state_payload
            changed = True

        clips = VideoClip.query.filter_by(use_case_id=use_case.id).all()
        clip_stats = {
            "total": len(clips),
            "complete": sum(1 for clip in clips if clip.status == "complete"),
            "pending": sum(1 for clip in clips if clip.status == "pending"),
            "generating": sum(1 for clip in clips if clip.status == "generating"),
            "error": sum(1 for clip in clips if clip.status == "error"),
        }
        clip_message = None
        clip_status = "pending"
        if clip_stats["error"]:
            clip_status = "error"
            clip_message = f"{clip_stats['error']} clip(s) need attention"
        elif clip_stats["generating"] or clip_stats["pending"]:
            clip_status = "running"
            clip_message = f"{clip_stats['complete']} of {use_case.num_clips} complete"
        elif clip_stats["complete"]:
            clip_status = "complete"
            clip_message = f"{clip_stats['complete']} clips ready"
        clip_payload = cls._default_payload(clip_status, clip_message, meta=clip_stats)
        if state.get("clips") != clip_payload:
            state["clips"] = clip_payload
            changed = True

        analyzed = sum(1 for clip in clips if clip.status == "complete" and clip.content_description)
        analysis_status = "pending"
        analysis_message = None
        if clip_stats["complete"] == 0:
            analysis_message = "No completed clips yet"
        elif analyzed == clip_stats["complete"] and clip_stats["complete"]:
            analysis_status = "complete"
            analysis_message = "All clips analyzed"
        elif analyzed:
            analysis_status = "running"
            analysis_message = f"{analyzed}/{clip_stats['complete']} analyzed"
        else:
            analysis_status = "pending"
            analysis_message = "Analysis ready to run"
        analysis_payload = cls._default_payload(analysis_status, analysis_message, meta={"analyzed": analyzed})
        if state.get("analysis") != analysis_payload:
            state["analysis"] = analysis_payload
            changed = True

        final_video = (
            FinalVideo.query.filter_by(use_case_id=use_case.id)
            .order_by(FinalVideo.created_at.desc())
            .first()
        )
        assembly_status = "pending"
        assembly_message = "Awaiting render"
        final_payload_meta = {}
        if final_video:
            if final_video.status == "complete":
                assembly_status = "complete"
                assembly_message = "Latest render ready"
            elif final_video.status == "error":
                assembly_status = "error"
                assembly_message = final_video.error_message or "Render failed"
            else:
                assembly_status = final_video.status or "running"
                assembly_message = "Rendering in progress"
            final_payload_meta = {
                "final_video_id": final_video.id,
                "status": final_video.status,
                "duration": final_video.duration,
            }
        assembly_payload = cls._default_payload(assembly_status, assembly_message, meta=final_payload_meta)
        if state.get("assembly") != assembly_payload:
            state["assembly"] = assembly_payload
            changed = True

        final_stage_payload = cls._default_payload(
            "complete" if final_video else "pending",
            "Download ready" if final_video else "No final render yet",
            meta={"video_url": f"/uploads/{final_video.file_path}" if final_video and final_video.file_path else None},
        )
        if state.get("final_output") != final_stage_payload:
            state["final_output"] = final_stage_payload
            changed = True

        if changed:
            use_case.pipeline_state = state
            db.session.commit()

        return {
            "use_case_id": use_case.id,
            "stages": state,
            "clip_stats": clip_stats,
            "analyzed": analyzed,
            "has_final_video": bool(final_video),
        }


class PipelineRecoveryService:
    """Attempt to resume stalled stages using deterministic fallbacks."""

    def __init__(self) -> None:
        cfg = current_app.config if current_app else {}
        self.upload_folder = cfg.get("UPLOAD_FOLDER", "./uploads")
        self.api_keys = {
            "pollo": cfg.get("POLLO_API_KEY"),
            "moonshot": cfg.get("MOONSHOT_API_KEY"),
            "elevenlabs": cfg.get("ELEVENLABS_API_KEY"),
        }
        self.ffmpeg_path = cfg.get("FFMPEG_PATH", "ffmpeg")

    def resume(self, use_case: UseCase, *, target_stage: Optional[str] = None) -> Dict[str, Any]:
        summary_before = PipelineProgressTracker.summarize(use_case)
        actions: List[str] = []
        errors: List[str] = []

        try:
            script_result = self._recover_script(use_case, summary_before)
            if script_result:
                actions.append(script_result)
        except Exception as exc:  # pragma: no cover - logged to UI
            errors.append(f"Script recovery failed: {exc}")

        if target_stage and target_stage not in PipelineProgressTracker.STAGES:
            return {
                "success": False,
                "error": f"Unknown target stage '{target_stage}'",
                "summary": summary_before,
            }

        if not target_stage or target_stage in ("clips", "analysis", "assembly", "final_output"):
            try:
                clip_action = self._recover_clips(use_case)
                if clip_action:
                    actions.append(clip_action)
            except Exception as exc:
                errors.append(f"Clip recovery failed: {exc}")

        if not target_stage or target_stage in ("analysis", "assembly", "final_output"):
            try:
                analysis_action = self._recover_analysis(use_case)
                if analysis_action:
                    actions.append(analysis_action)
            except Exception as exc:
                errors.append(f"Analysis recovery failed: {exc}")

        if not target_stage or target_stage in ("assembly", "final_output"):
            try:
                assembly_action = self._recover_assembly(use_case)
                if assembly_action:
                    actions.append(assembly_action)
            except Exception as exc:
                errors.append(f"Assembly recovery failed: {exc}")

        summary_after = PipelineProgressTracker.summarize(use_case)
        return {
            "success": len(errors) == 0,
            "actions": actions,
            "errors": errors,
            "summary": summary_after,
        }

    # ------------------------------------------------------------------
    def _recover_script(self, use_case: UseCase, summary: Dict[str, Any]) -> Optional[str]:
        script = Script.query.filter_by(use_case_id=use_case.id).first()
        if script and script.status == "approved":
            return None

        product = Product.query.get(use_case.product_id)
        if not product:
            raise ValueError("Product not found for use case")

        if script and script.status == "generated":
            PipelineProgressTracker.update_stage(
                use_case,
                "script",
                "awaiting_approval",
                message="Generated script pending approval",
            )
            return "Script already generated – awaiting approval"

        api_key = self.api_keys.get("openai")
        generator = ScriptGenerator(api_key=api_key)
        product_data = {
            "name": product.name,
            "description": product.description,
            "brand": product.brand,
            "price": product.price,
            "currency": product.currency,
            "specifications": product.specifications,
            "reviews": product.reviews,
        }
        use_case_config = {
            "format": use_case.format,
            "style": use_case.style,
            "goal": use_case.goal,
            "target_audience": use_case.target_audience,
            "duration_target": use_case.duration_target,
        }
        hook_payload = build_hook_script_payload(getattr(use_case, "hook", None))
        PipelineProgressTracker.update_stage(use_case, "script", "running", message="Regenerating script")
        result = generator.generate_script(
            product_data,
            use_case_config,
            existing_script=script.content if script else None,
            hook=hook_payload,
        )
        if not result.get("success"):
            PipelineProgressTracker.update_stage(
                use_case,
                "script",
                "error",
                message=result.get("error", "Unable to generate script"),
            )
            raise RuntimeError(result.get("error", "Script generation failed"))

        if not script:
            script = Script(
                use_case_id=use_case.id,
                content=result["content"],
                estimated_duration=result["estimated_duration"],
                tone=use_case.style,
                status="generated",
            )
            db.session.add(script)
        else:
            script.content = result["content"]
            script.estimated_duration = result["estimated_duration"]
            script.status = "generated"
        db.session.commit()
        PipelineProgressTracker.update_stage(use_case, "script", "awaiting_approval", message="Script regenerated")
        return "Regenerated script"

    def _ensure_clip_manager(self) -> VideoClipManager:
        return VideoClipManager(api_key=self.api_keys.get("pollo"), upload_folder=self.upload_folder)

    def _recover_clips(self, use_case: UseCase) -> Optional[str]:
        script = Script.query.filter_by(use_case_id=use_case.id).first()
        if not script or script.status != "approved":
            return None

        manager = self._ensure_clip_manager()
        clips = VideoClip.query.filter_by(use_case_id=use_case.id).order_by(VideoClip.sequence_order).all()
        created = 0
        restarted = 0

        if not clips:
            prompts = manager.generate_clip_prompts(use_case, script.content, use_case.product)
            for prompt in prompts:
                clip = manager.create_clip(
                    use_case_id=use_case.id,
                    sequence_order=prompt["sequence_order"],
                    prompt=prompt["prompt"],
                    length=prompt.get("estimated_duration", 5),
                )
                manager.start_generation(clip.id)
                created += 1
        else:
            for clip in clips:
                if clip.status in ("error", "pending"):
                    manager.regenerate_clip(clip.id)
                    restarted += 1
                elif clip.status == "complete" and not clip.file_path and clip.pollo_job_id:
                    manager._sync_clip_with_pollo(clip)

        if created or restarted:
            PipelineProgressTracker.update_stage(
                use_case,
                "clips",
                "running",
                message=f"{created} new / {restarted} restarted",
            )
            return f"Queued {created} new clips and restarted {restarted}"
        return None

    def _recover_analysis(self, use_case: UseCase) -> Optional[str]:
        completed_clips = VideoClip.query.filter_by(use_case_id=use_case.id, status="complete").all()
        if not completed_clips:
            return None
        unanalyzed = [clip for clip in completed_clips if not clip.content_description]
        if not unanalyzed:
            return None

        analyzer = ClipAnalyzer(api_key=self.api_keys.get("openai"))
        analyzer.analyze_use_case_clips(use_case.id, self.upload_folder, force=False)
        PipelineProgressTracker.update_stage(
            use_case,
            "analysis",
            "running",
            message=f"Analyzing {len(unanalyzed)} clips",
        )
        return f"Triggered analysis for {len(unanalyzed)} clip(s)"

    def _recover_assembly(self, use_case: UseCase) -> Optional[str]:
        script = Script.query.filter_by(use_case_id=use_case.id).first()
        if not script or script.status != "approved":
            return None

        completed_clips = VideoClip.query.filter_by(use_case_id=use_case.id, status="complete").count()
        if completed_clips == 0:
            return None

        voiceover_generator = VoiceoverGenerator(
            api_key=self.api_keys.get("elevenlabs"),
            upload_folder=self.upload_folder,
            ffmpeg_path=self.ffmpeg_path,
        )
        PipelineProgressTracker.update_stage(use_case, "assembly", "running", message="Rebuilding final video")
        voiceover_result = voiceover_generator.generate_voiceover(use_case, script, force=False)
        if not voiceover_result.get("success"):
            PipelineProgressTracker.update_stage(
                use_case,
                "assembly",
                "error",
                message=voiceover_result.get("error"),
            )
            raise RuntimeError(voiceover_result.get("error", "Voiceover failed"))

        assembler = VideoAssembler(upload_folder=self.upload_folder, ffmpeg_path=self.ffmpeg_path)
        assembly_result = assembler.assemble_use_case(
            use_case,
            script,
            audio_relative_path=voiceover_result.get("file_path"),
        )
        if not assembly_result.get("success"):
            PipelineProgressTracker.update_stage(
                use_case,
                "assembly",
                "error",
                message=assembly_result.get("error"),
            )
            raise RuntimeError(assembly_result.get("error", "Assembly failed"))

        PipelineProgressTracker.update_stage(use_case, "assembly", "complete", message="Final video refreshed")
        PipelineProgressTracker.update_stage(use_case, "final_output", "complete", message="Download ready")
        return "Regenerated final video"
