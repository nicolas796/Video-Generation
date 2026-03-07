"""Utilities for downloading Pollo clip assets and thumbnails."""
from __future__ import annotations

import os
from typing import Dict, Optional

import requests

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None


def download_clip_assets(clip, video_url: str, upload_root: str, logger=None) -> Dict[str, Optional[str]]:
    """Download a clip video and thumbnail relative to the upload root."""
    if not video_url:
        raise ValueError("Missing video URL for clip download")

    upload_root = os.path.abspath(upload_root or './uploads')
    use_case_folder = os.path.join(upload_root, 'clips', str(clip.use_case_id))
    os.makedirs(use_case_folder, exist_ok=True)

    video_filename = f"clip_{clip.id:03d}_{clip.sequence_order:02d}.mp4"
    video_path = os.path.join(use_case_folder, video_filename)

    if logger:
        logger.info(
            "Downloading Pollo clip",
            extra={
                'clip_id': clip.id,
                'use_case_id': clip.use_case_id,
                'target_path': video_path,
                'video_url': video_url[:200]
            }
        )

    response = requests.get(video_url, stream=True, timeout=120)
    response.raise_for_status()

    with open(video_path, 'wb') as video_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                video_file.write(chunk)

    thumbnail_rel_path = generate_clip_thumbnail(video_path, clip.use_case_id, clip.id, upload_root, logger=logger)
    video_rel_path = os.path.relpath(video_path, upload_root)

    if logger:
        logger.info(
            "Clip download complete",
            extra={
                'clip_id': clip.id,
                'use_case_id': clip.use_case_id,
                'video_rel_path': video_rel_path,
                'thumbnail_rel_path': thumbnail_rel_path
            }
        )

    return {
        'video': video_rel_path,
        'thumbnail': thumbnail_rel_path
    }


def generate_clip_thumbnail(video_path: str, use_case_id: int, clip_id: int, upload_root: str, logger=None) -> Optional[str]:
    """Generate a thumbnail for the downloaded clip."""
    if cv2 is None:
        if logger:
            logger.warning('cv2 not available; skipping thumbnail generation', extra={'clip_id': clip_id})
        return None
    try:
        cap = cv2.VideoCapture(video_path)
    except Exception as exc:  # pragma: no cover - best effort logging only
        if logger:
            logger.warning('Failed to open video for thumbnail', extra={'clip_id': clip_id, 'error': str(exc)})
        return None

    if not cap.isOpened():
        cap.release()
        if logger:
            logger.warning('Video capture failed for thumbnail', extra={'clip_id': clip_id})
        return None

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(total_frames // 2, 0))

    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        if logger:
            logger.warning('Unable to read frame for thumbnail', extra={'clip_id': clip_id})
        return None

    thumb_folder = os.path.join(upload_root, 'clips', str(use_case_id), 'thumbnails')
    os.makedirs(thumb_folder, exist_ok=True)
    thumb_filename = f"clip_{clip_id:03d}_thumb.jpg"
    thumb_path = os.path.join(thumb_folder, thumb_filename)

    height, width = frame.shape[:2]
    max_width = 480
    if width > max_width and width > 0:
        ratio = max_width / float(width)
        frame = cv2.resize(frame, (max_width, int(height * ratio)), interpolation=cv2.INTER_AREA)

    cv2.imwrite(thumb_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return os.path.relpath(thumb_path, os.path.abspath(upload_root))
