"""Voiceover generation and mixing via ElevenLabs with offline fallback."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

import requests

from app.models import Script, UseCase
from app.utils import api_retry, ExternalAPIError, NonRetryableAPIError


class VoiceoverGenerator:
    """Generate and cache voiceovers for use cases."""

    DEFAULT_MODEL = "eleven_multilingual_v2"
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: Optional[str] = None,
        upload_folder: str = "./uploads",
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self.offline_mode = not bool(self.api_key)
        self.upload_folder = upload_folder
        self.final_folder = os.path.join(upload_folder, "final")
        os.makedirs(self.final_folder, exist_ok=True)
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = os.getenv("FFPROBE_PATH") or (
            ffmpeg_path.replace("ffmpeg", "ffprobe") if "ffmpeg" in ffmpeg_path else "ffprobe"
        )

    # ------------------------------------------------------------------
    @api_retry(label="elevenlabs_tts")
    def _perform_tts_request(self, voice_id: str, headers: Dict[str, str], payload: Dict[str, Any]):
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers=headers,
            json=payload,
            timeout=120,
        )
        if response.status_code >= 400:
            error_payload = self._safe_json(response)
            message = error_payload.get("detail") or error_payload.get("message") or response.text
            if response.status_code in self.RETRYABLE_STATUS or response.status_code >= 500:
                raise ExternalAPIError("ElevenLabs", message, status_code=response.status_code, payload=error_payload)
            raise NonRetryableAPIError("ElevenLabs", message, status_code=response.status_code, payload=error_payload)
        return response

    def _safe_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------------
    def generate_voiceover(
        self,
        use_case: UseCase,
        script: Script,
        *,
        force: bool = False,
        background_music: Optional[str] = None,
        voiceover_format: str = "mp3",
    ) -> Dict[str, Any]:
        if not script or not script.content:
            return {"success": False, "error": "Script content is required for voiceover"}

        voice_id = use_case.voice_id or os.getenv("DEFAULT_VOICE_ID") or "XB0fDUnXU5powFXDhCwa"
        voice_settings = use_case.voice_settings or {
            "stability": 0.45,
            "similarity_boost": 0.8,
            "style": 0.0,
            "use_speaker_boost": True,
        }

        use_case_folder = os.path.join(self.final_folder, str(use_case.id))
        os.makedirs(use_case_folder, exist_ok=True)

        script_hash = hash(script.content)
        meta_path = os.path.join(use_case_folder, "voiceover_meta.json")
        if not force and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as meta_file:
                    meta = json.load(meta_file)
                if meta.get("script_hash") == script_hash and meta.get("file_path"):
                    cached_path = self._resolve_path(meta["file_path"])
                    if os.path.exists(cached_path):
                        duration = meta.get("duration") or self._probe_duration(cached_path)
                        return {"success": True, "file_path": meta["file_path"], "duration": duration, "cached": True}
            except (json.JSONDecodeError, OSError):
                pass

        if self.offline_mode:
            relative_path = self._create_placeholder_voiceover(use_case_folder, script, voiceover_format)
            duration = self._probe_duration(self._resolve_path(relative_path))
            self._persist_meta(meta_path, script_hash, relative_path, duration, voice_id)
            return {"success": True, "file_path": relative_path, "duration": duration, "cached": False, "offline": True}

        payload = {
            "text": script.content,
            "model_id": self.DEFAULT_MODEL,
            "voice_settings": voice_settings,
        }
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}

        try:
            response = self._perform_tts_request(voice_id, headers, payload)
        except ExternalAPIError as exc:
            if self.offline_mode:
                relative_path = self._create_placeholder_voiceover(use_case_folder, script, voiceover_format)
                duration = self._probe_duration(self._resolve_path(relative_path))
                self._persist_meta(meta_path, script_hash, relative_path, duration, voice_id)
                return {"success": True, "file_path": relative_path, "duration": duration, "cached": False, "offline": True}
            return {"success": False, "error": f"ElevenLabs is temporarily unavailable: {exc}"}
        except NonRetryableAPIError as exc:
            return {"success": False, "error": f"ElevenLabs could not synthesize this script: {exc}"}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": f"ElevenLabs network error: {exc}"}

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"voiceover_{timestamp}.{voiceover_format}"
        output_path = os.path.join(use_case_folder, filename)
        with open(output_path, "wb") as audio_file:
            audio_file.write(response.content)

        if background_music:
            background_path = self._resolve_path(background_music)
            if os.path.exists(background_path):
                mixed_path = os.path.join(use_case_folder, f"voiceover_mix_{timestamp}.{voiceover_format}")
                if self._mix_with_background(output_path, background_path, mixed_path):
                    output_path = mixed_path

        duration = self._probe_duration(output_path)
        relative_path = os.path.relpath(output_path, self.upload_folder)
        self._persist_meta(meta_path, script_hash, relative_path, duration, voice_id)
        return {"success": True, "file_path": relative_path, "duration": duration, "cached": False}

    # ------------------------------------------------------------------
    def _persist_meta(self, meta_path: str, script_hash: int, relative_path: str, duration: Optional[float], voice_id: str) -> None:
        payload = {
            "script_hash": script_hash,
            "file_path": relative_path,
            "duration": duration,
            "voice_id": voice_id,
            "generated_at": datetime.utcnow().isoformat(),
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as meta_file:
                json.dump(payload, meta_file, indent=2)
        except OSError:
            pass

    def _create_placeholder_voiceover(self, folder: str, script: Script, fmt: str) -> str:
        duration = max(4, int(len(script.content.split()) / 2.3))
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"voiceover_offline_{timestamp}.{fmt}"
        output_path = os.path.join(folder, filename)
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=mono",
            "-t",
            str(duration),
            "-q:a",
            "9",
            output_path,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError):
            with open(output_path, "wb") as audio_file:
                audio_file.write(b"")
        return os.path.relpath(output_path, self.upload_folder)

    def _resolve_path(self, maybe_relative: str) -> str:
        return maybe_relative if os.path.isabs(maybe_relative) else os.path.join(self.upload_folder, maybe_relative)

    def _probe_duration(self, media_path: str) -> Optional[float]:
        if not media_path or not os.path.exists(media_path):
            return None
        try:
            result = subprocess.run(
                [
                    self.ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    media_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                text=True,
            )
            return round(float(result.stdout.strip()), 2)
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
            return None

    def _mix_with_background(
        self,
        voiceover_path: str,
        background_path: str,
        output_path: str,
        *,
        voice_gain: float = 1.0,
        background_gain: float = 0.25,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i",
            voiceover_path,
            "-i",
            background_path,
            "-filter_complex",
            (
                f"[0:a]volume={voice_gain}[voice];"
                f"[1:a]volume={background_gain}[bg];"
                f"[voice][bg]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
            ),
            "-map",
            "[aout]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            output_path,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except subprocess.CalledProcessError:
            return False
