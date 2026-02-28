"""Clip ordering engine that enforces narrative flow and visual variety."""
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from flask import current_app
from app.models import UseCase, VideoClip


@dataclass
class ClipScore:
    """Helper structure for scoring clips."""
    clip: VideoClip
    clip_id: int
    content_type: str
    original_order: int
    suggested_order: int
    score: float
    reasoning: str


class ClipOrderingEngine:
    """Determine the optimal sequence for clips based on analysis data."""

    NARRATIVE_SEQUENCE = [
        'hook',
        'problem',
        'emotion',
        'solution',
        'product',
        'demo',
        'lifestyle',
        'motion',
        'social_proof',
        'cta'
    ]

    TRANSITION_SCORES = {
        ('hook', 'problem'): 0.9,
        ('hook', 'emotion'): 0.8,
        ('hook', 'product'): 0.7,
        ('problem', 'solution'): 0.95,
        ('problem', 'emotion'): 0.85,
        ('emotion', 'solution'): 0.9,
        ('emotion', 'product'): 0.8,
        ('solution', 'product'): 0.9,
        ('solution', 'demo'): 0.85,
        ('product', 'demo'): 0.9,
        ('product', 'lifestyle'): 0.8,
        ('demo', 'lifestyle'): 0.85,
        ('demo', 'product'): 0.75,
        ('lifestyle', 'social_proof'): 0.8,
        ('lifestyle', 'motion'): 0.75,
        ('social_proof', 'cta'): 0.9,
        ('motion', 'cta'): 0.85,
        ('product', 'cta'): 0.8,
    }

    VARIETY_FACTORS = {
        'consecutive_same_type': -0.3,
        'missing_hook': -0.5,
        'missing_cta': -0.5,
        'visual_contrast': 0.15
    }

    def recommend_order(
        self,
        clips: List[VideoClip],
        use_case: Optional[UseCase] = None
    ) -> Dict[str, Any]:
        """Generate the recommended ordering for the provided clips."""
        if not clips:
            return {
                'success': False,
                'error': 'No clips available for ordering',
                'recommended_order': []
            }

        clip_data: List[Dict[str, Any]] = []
        missing_analysis: List[int] = []
        for clip in clips:
            content_type = self._infer_content_type(clip)
            if not clip.content_description:
                missing_analysis.append(clip.id)
            clip_data.append({
                'clip': clip,
                'content_type': content_type,
                'duration': clip.duration or 5
            })

        optimized = self._build_narrative_sequence(clip_data)
        sequence_score = self._score_sequence(optimized)
        total_duration = sum(item.clip.duration or 5 for item in optimized)
        duration_target = use_case.duration_target if use_case and use_case.duration_target else 30

        recommendation = []
        # Get upload folder path for verifying thumbnail existence
        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        
        for index, item in enumerate(optimized):
            clip = item.clip
            
            # Only return thumbnail URL if the file actually exists
            thumbnail_url = None
            if clip.thumbnail_path:
                thumb_path = os.path.join(upload_folder, clip.thumbnail_path)
                if os.path.exists(thumb_path):
                    thumbnail_url = f"/uploads/{clip.thumbnail_path}"
            
            recommendation.append({
                'clip_id': clip.id,
                'sequence_order': index,
                'previous_order': clip.sequence_order,
                'content_type': item.content_type,
                'duration': clip.duration or 5,
                'tags': clip.tags or [],
                'thumbnail_url': thumbnail_url,
                'description': self._truncate(clip.content_description),
                'reasoning': item.reasoning,
                'analysis': clip.analysis_metadata or {}
            })

        return {
            'success': True,
            'recommended_order': recommendation,
            'score': round(sequence_score * 100, 1),
            'narrative_flow': ' → '.join([item.content_type for item in optimized]),
            'total_duration': total_duration,
            'duration_target': duration_target,
            'visual_variety': self._summarize_variety(optimized),
            'duration_summary': self.check_duration_constraints(clips, duration_target),
            'missing_analysis': missing_analysis
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _infer_content_type(self, clip: VideoClip) -> str:
        inferred = clip.infer_content_type()
        return inferred or 'general'

    def _calculate_transition_score(self, from_type: str, to_type: str) -> float:
        if from_type == to_type:
            return 0.5
        score = self.TRANSITION_SCORES.get((from_type, to_type), 0.6)
        try:
            distance = abs(
                self.NARRATIVE_SEQUENCE.index(from_type) -
                self.NARRATIVE_SEQUENCE.index(to_type)
            )
            if distance == 1:
                score += 0.1
            elif distance == 2:
                score += 0.05
        except ValueError:
            pass
        return min(score, 1.0)

    def _score_sequence(self, sequence: List[ClipScore]) -> float:
        if len(sequence) <= 1:
            return 1.0
        total = 0.0
        penalties = 0.0
        bonuses = 0.0

        if sequence[0].content_type != 'hook':
            penalties += abs(self.VARIETY_FACTORS['missing_hook'])
        if sequence[-1].content_type != 'cta':
            penalties += abs(self.VARIETY_FACTORS['missing_cta'])

        for index in range(len(sequence) - 1):
            current_type = sequence[index].content_type
            next_type = sequence[index + 1].content_type
            total += self._calculate_transition_score(current_type, next_type)
            if current_type == next_type:
                penalties += abs(self.VARIETY_FACTORS['consecutive_same_type'])
            else:
                bonuses += self.VARIETY_FACTORS['visual_contrast']

        avg = total / (len(sequence) - 1)
        final_score = avg - penalties + bonuses
        return max(0.0, min(1.0, final_score))

    def _build_narrative_sequence(self, clip_data: List[Dict[str, Any]]) -> List[ClipScore]:
        type_groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in clip_data:
            type_groups.setdefault(item['content_type'], []).append(item)

        ordered: List[ClipScore] = []
        used_ids: set[int] = set()

        for role in self.NARRATIVE_SEQUENCE:
            for item in type_groups.get(role, []):
                clip = item['clip']
                if clip.id in used_ids:
                    continue
                ordered.append(ClipScore(
                    clip=clip,
                    clip_id=clip.id,
                    content_type=role,
                    original_order=clip.sequence_order,
                    suggested_order=len(ordered),
                    score=0.0,
                    reasoning=''
                ))
                used_ids.add(clip.id)

        for item in clip_data:
            clip = item['clip']
            if clip.id in used_ids:
                continue
            ordered.append(ClipScore(
                clip=clip,
                clip_id=clip.id,
                content_type=item['content_type'],
                original_order=clip.sequence_order,
                suggested_order=len(ordered),
                score=0.0,
                reasoning=''
            ))
            used_ids.add(clip.id)

        for index, item in enumerate(ordered):
            item.suggested_order = index
            item.reasoning = self._get_position_reasoning(item.content_type, index, len(ordered))
            if index == 0 and item.content_type == 'hook':
                item.score = 1.0
            elif index == len(ordered) - 1 and item.content_type == 'cta':
                item.score = 1.0
            else:
                item.score = 0.8

        return ordered

    def _get_position_reasoning(self, content_type: str, position: int, total: int) -> str:
        reasoning_map = {
            'hook': "Opening hook to stop the scroll",
            'problem': "Sets up the pain point viewers relate to",
            'emotion': "Builds emotional connection",
            'solution': "Presents the solution and transformation",
            'product': "Highlights hero product details",
            'demo': "Shows how the product works",
            'lifestyle': "Places the product in real life context",
            'motion': "Adds energetic movement between sections",
            'social_proof': "Builds trust with real-world validation",
            'cta': "Clear call-to-action and brand memory",
            'general': "Supporting content"
        }
        prefix = '[OPENING]' if position == 0 else '[CLOSING]' if position == total - 1 else '[BODY]'
        return f"{prefix} {reasoning_map.get(content_type, reasoning_map['general'])}"

    def _summarize_variety(self, sequence: List[ClipScore]) -> Dict[str, Any]:
        if not sequence:
            return {'score': 0, 'message': 'No clips', 'unique_types': 0}

        types = [item.content_type for item in sequence]
        unique_types = len(set(types))
        consecutive_matches = sum(
            1 for idx in range(1, len(types)) if types[idx] == types[idx - 1]
        )
        variety_score = round(unique_types / len(types), 2)
        message = 'Great variety' if variety_score >= 0.7 else (
            'Decent variety' if variety_score >= 0.5 else 'Consider mixing up visuals'
        )
        if consecutive_matches:
            message += f" · {consecutive_matches} back-to-back repeats"

        return {
            'score': variety_score,
            'message': message,
            'unique_types': unique_types,
            'consecutive_repeats': consecutive_matches
        }

    def _truncate(self, text: Optional[str], max_chars: int = 140) -> Optional[str]:
        if not text:
            return None
        return text if len(text) <= max_chars else text[: max_chars - 3] + '...'

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def check_duration_constraints(
        self,
        clips: List[VideoClip],
        target_duration: int
    ) -> Dict[str, Any]:
        current_duration = sum(c.duration or 5 for c in clips)
        variance = current_duration - target_duration
        if variance > 5:
            status = 'warning'
            message = f"+{variance}s over target"
            suggestion = 'Trim or drop lower-priority clips'
        elif variance < -5:
            status = 'info'
            message = f"{abs(variance)}s under target"
            suggestion = 'Add filler shots or extend clips'
        else:
            status = 'success'
            message = 'Within acceptable range'
            suggestion = 'Ready for assembly'

        return {
            'target_duration': target_duration,
            'current_duration': current_duration,
            'variance': variance,
            'status': status,
            'message': message,
            'suggestion': suggestion,
            'clips_count': len(clips)
        }

    def apply_sequence(
        self,
        use_case_id: int,
        sequence_order: List[Dict[str, int]]
    ) -> Dict[str, Any]:
        from app import db

        try:
            for payload in sequence_order:
                clip = VideoClip.query.filter_by(
                    id=payload['clip_id'],
                    use_case_id=use_case_id
                ).first()
                if clip:
                    clip.sequence_order = payload['sequence_order']
            db.session.commit()
            return {
                'success': True,
                'message': 'Clip order updated',
                'clips_updated': len(sequence_order)
            }
        except Exception as exc:
            db.session.rollback()
            return {
                'success': False,
                'error': str(exc)
            }
