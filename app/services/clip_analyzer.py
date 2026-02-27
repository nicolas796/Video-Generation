"""Clip analysis service using Kimi (Moonshot) multimodal models."""
import base64
import json
import os
from textwrap import dedent
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
import requests

from app import db
from app.models import VideoClip


class ClipAnalyzer:
    """Analyze video clips to extract semantic tags and visual intelligence."""

    ANALYSIS_PROMPT = dedent("""
        You are a senior creative director reviewing AI-generated product video clips. For every set of frames you receive, respond with JSON that matches the schema below:
        {
          "description": "2-3 vivid sentences describing what is happening",
          "primary_category": "one of: hook, problem, solution, product, demo, lifestyle, social_proof, emotion, motion, cta",
          "content_type_confidence": 0.0-1.0,
          "objects": ["Main objects or props"],
          "actions": ["Notable actions or movements"],
          "setting": "Short description of location or vibe",
          "visual_elements": ["Key visual ingredients (lighting, camera, composition)"],
          "mood": "Overall emotional tone",
          "tags": ["5-8 concise lowercase tags"],
          "quality_score": 1-10,
          "recommended_role": "hook/problem/solution/... whichever fits best"
        }
    """)



    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.getenv("MOONSHOT_API_KEY")
        if not self.api_key:
            raise ValueError("MOONSHOT_API_KEY is required for clip analysis")

        self.model = model or os.getenv("CLIP_ANALYSIS_MODEL", "kimi-k2.5")
        self.base_url = "https://api.moonshot.ai/v1"

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def analyze_clip(
        self,
        clip: VideoClip,
        upload_folder: str = "./uploads",
        force: bool = False
    ) -> Dict[str, Any]:
        """Analyze a single clip and persist results."""

        if clip.status != 'complete':
            return {
                'success': False,
                'clip_id': clip.id,
                'error': 'Clip must be complete before analysis'
            }

        if clip.content_description and not force:
            return {
                'success': True,
                'clip_id': clip.id,
                'skipped': True,
                'reason': 'Existing analysis preserved'
            }

        frames = self._gather_visual_inputs(clip, upload_folder)
        if not frames:
            return {
                'success': False,
                'clip_id': clip.id,
                'error': 'No visual data found (video or thumbnail missing)'
            }

        payload = self._build_payload(clip, frames)

        try:
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=90
            )
            response.raise_for_status()
            raw_content = response.json()['choices'][0]['message']['content']
            analysis = json.loads(raw_content)
        except requests.exceptions.RequestException as exc:
            detail = self._extract_http_error(exc)
            return {
                'success': False,
                'clip_id': clip.id,
                'error': detail
            }
        except json.JSONDecodeError:
            return {
                'success': False,
                'clip_id': clip.id,
                'error': 'Vision model returned invalid JSON'
            }

        self._persist_analysis(clip, analysis, len(frames))

        return {
            'success': True,
            'clip_id': clip.id,
            'analysis': analysis
        }

    def analyze_use_case_clips(
        self,
        use_case_id: int,
        upload_folder: str = './uploads',
        force: bool = False
    ) -> Dict[str, Any]:
        """Analyze all complete clips for a use case."""
        clips = VideoClip.query.filter_by(
            use_case_id=use_case_id,
            status='complete'
        ).order_by(VideoClip.sequence_order).all()

        if not clips:
            return {
                'success': False,
                'error': 'No complete clips available for analysis',
                'total': 0,
                'analyzed': 0,
                'failed': 0
            }

        results: List[Dict[str, Any]] = []
        analyzed = 0
        failed = 0

        for clip in clips:
            result = self.analyze_clip(clip, upload_folder, force=force)
            results.append(result)

            if result.get('success'):
                if not result.get('skipped'):
                    analyzed += 1
            else:
                failed += 1

        return {
            'success': failed == 0,
            'use_case_id': use_case_id,
            'total': len(clips),
            'analyzed': analyzed,
            'failed': failed,
            'results': results
        }

    def get_clip_content_type(self, clip: VideoClip) -> str:
        """Expose inferred content type for other services."""
        return clip.infer_content_type() or 'general'

    def compare_clips(self, clip1: VideoClip, clip2: VideoClip) -> Dict[str, Any]:
        """Simple similarity check based on existing tags."""
        tags1 = set(clip1.tags or [])
        tags2 = set(clip2.tags or [])
        intersection = tags1.intersection(tags2)
        union = tags1.union(tags2)
        similarity = len(intersection) / len(union) if union else 0

        return {
            'tag_similarity': round(similarity, 2),
            'shared_tags': list(intersection),
            'is_similar': similarity >= 0.5,
            'type_1': self.get_clip_content_type(clip1),
            'type_2': self.get_clip_content_type(clip2)
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_payload(self, clip: VideoClip, frames: List[str]) -> Dict[str, Any]:
        user_content: List[Dict[str, Any]] = [
            {
                'type': 'text',
                'text': (
                    "Analyze these frames from a generated marketing clip. "
                    f"Original prompt: {clip.prompt or 'N/A'}. "
                    "Extract objects, actions, scene details, and marketing role."
                )
            }
        ]

        for frame_b64 in frames:
            user_content.append({
                'type': 'image_url',
                'image_url': {
                    'url': f'data:image/jpeg;base64,{frame_b64}',
                    'detail': 'high'
                }
            })

        return {
            'model': self.model,
            'temperature': 1.0,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': self.ANALYSIS_PROMPT},
                {'role': 'user', 'content': user_content}
            ]
        }

    def _gather_visual_inputs(self, clip: VideoClip, upload_folder: str) -> List[str]:
        frames: List[str] = []

        if clip.file_path:
            video_path = os.path.join(upload_folder, clip.file_path)
            if os.path.exists(video_path):
                frames.extend(self._extract_video_frames(video_path))

        if not frames and clip.thumbnail_path:
            thumb_path = os.path.join(upload_folder, clip.thumbnail_path)
            if os.path.exists(thumb_path):
                frames.append(self._encode_image(thumb_path))

        return frames[:3]

    def _extract_video_frames(self, video_path: str, max_frames: int = 3) -> List[str]:
        frames: List[str] = []
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return frames

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        positions = [0.15, 0.5, 0.85][:max_frames]

        for position in positions:
            frame_index = int(total_frames * position)
            frame_index = min(max(frame_index, 0), total_frames - 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            success, frame = cap.read()
            if not success or frame is None:
                continue
            encoded = self._encode_frame(frame)
            if encoded:
                frames.append(encoded)

        cap.release()
        return frames

    def _encode_image(self, image_path: str) -> str:
        with open(image_path, 'rb') as file:
            return base64.b64encode(file.read()).decode('utf-8')

    def _encode_frame(self, frame) -> Optional[str]:
        success, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not success:
            return None
        return base64.b64encode(buffer.tobytes()).decode('utf-8')

    def _persist_analysis(self, clip: VideoClip, analysis: Dict[str, Any], frames_used: int) -> None:
        metadata = clip.analysis_metadata or {}
        metadata.update({
            'primary_category': analysis.get('primary_category') or metadata.get('primary_category'),
            'recommended_role': analysis.get('recommended_role') or analysis.get('primary_category') or metadata.get('recommended_role'),
            'visual_elements': analysis.get('visual_elements') or metadata.get('visual_elements', []),
            'objects': analysis.get('objects') or metadata.get('objects', []),
            'actions': analysis.get('actions') or metadata.get('actions', []),
            'setting': analysis.get('setting') or metadata.get('setting'),
            'mood': analysis.get('mood') or metadata.get('mood'),
            'quality_score': analysis.get('quality_score', metadata.get('quality_score', 6)),
            'confidence': analysis.get('content_type_confidence', metadata.get('confidence', 0.7)),
            'analysis_model': self.model,
            'frames_used': frames_used,
            'analyzed_at': datetime.utcnow().isoformat()
        })

        clip.content_description = analysis.get('description') or clip.content_description
        clip.tags = analysis.get('tags') or clip.tags or []
        clip.analysis_metadata = metadata

        db.session.commit()

    def _extract_http_error(self, exc: requests.exceptions.RequestException) -> str:
        if hasattr(exc, 'response') and exc.response is not None:
            try:
                payload = exc.response.json()
                return payload.get('error', {}).get('message', str(exc))
            except ValueError:
                return exc.response.text[:200]
        return str(exc)
