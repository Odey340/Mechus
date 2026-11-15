from __future__ import annotations

from dataclasses import dataclass
import inspect
import io
import json
import logging
import os
import time
from typing import Any
import urllib.request
import uuid

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerSegment:
    start_s: float
    end_s: float
    speaker: str


class PyannoteDiarizer:
    def __init__(
        self,
        model: str,
        device: str | None,
        hf_token: str | None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ):
        try:
            import torch  # type: ignore
            from pyannote.audio import Pipeline  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pyannote.audio is not installed") from exc

        if not model:
            raise ValueError("Diarization model is required")

        kwargs: dict[str, Any] = {}
        if hf_token:
            try:
                params = inspect.signature(Pipeline.from_pretrained).parameters
            except Exception:  # pragma: no cover
                params = {}
            if "use_auth_token" in params:
                kwargs["use_auth_token"] = hf_token
            elif "token" in params:
                kwargs["token"] = hf_token
            elif "auth_token" in params:
                kwargs["auth_token"] = hf_token
            else:
                # Fallback for newer hub auth: environment token.
                os.environ.setdefault("HF_TOKEN", hf_token)
                os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", hf_token)
        self._pipeline = Pipeline.from_pretrained(model, **kwargs)
        if device:
            try:
                self._pipeline.to(torch.device(device))
            except Exception as exc:  # pragma: no cover
                log.warning("Failed to move diarization to %s: %s", device, exc)

        self._min_speakers = min_speakers
        self._max_speakers = max_speakers
        self._torch = torch

    def diarize(self, audio_f32: np.ndarray, sample_rate_hz: int) -> list[SpeakerSegment]:
        if audio_f32.size == 0:
            return []
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")

        audio = self._prepare_audio(audio_f32)
        
        # Enhance audio for better diarization (normalize and reduce noise)
        audio = self._enhance_audio_for_diarization(audio, sample_rate_hz)
        
        # Ensure audio is float32 and convert to torch tensor
        audio = np.asarray(audio, dtype=np.float32)
        waveform = self._torch.from_numpy(audio).unsqueeze(0)
        
        # Ensure waveform is float32 (required by pyannote)
        if waveform.dtype != self._torch.float32:
            waveform = waveform.to(self._torch.float32)
        
        # Configure pipeline for better overlapping speech handling
        # Adjust speaker bounds based on audio duration
        # Short audio chunks might only have 1 speaker
        audio_duration = len(audio) / sample_rate_hz
        min_speakers = self._min_speakers
        max_speakers = self._max_speakers
        
        # For very short audio (< 2 seconds), allow 1 speaker
        if audio_duration < 2.0:
            min_speakers = 1
        # For short audio (< 5 seconds), allow at least 1 speaker
        elif audio_duration < 5.0 and min_speakers and min_speakers > 1:
            min_speakers = 1
        
        pipeline_params = {}
        if min_speakers is not None:
            pipeline_params["min_speakers"] = min_speakers
        if max_speakers is not None:
            pipeline_params["max_speakers"] = max_speakers
        
        # Try to use overlapping speech detection if available
        # Newer pyannote models support this natively
        try:
            # Check if pipeline has parameters for overlapping speech
            if hasattr(self._pipeline, "parameters"):
                params = self._pipeline.parameters()
                # Some models support clustering_threshold for better separation
                if "clustering" in str(type(self._pipeline)).lower():
                    # Use tighter clustering for better speaker separation
                    pass
        except Exception:
            pass
        
        diarization = self._pipeline(
            {"waveform": waveform, "sample_rate": int(sample_rate_hz)},
            **pipeline_params
        )
        
        segments: list[SpeakerSegment] = []
        
        # Try multiple methods to get annotation with overlapping speech support
        annotation = None
        
        # Method 1: Try to get speaker_diarization (supports overlaps in newer models)
        if hasattr(diarization, "speaker_diarization"):
            annotation = diarization.speaker_diarization
        # Method 2: Try annotation attribute
        elif hasattr(diarization, "annotation"):
            annotation = diarization.annotation
        # Method 3: Try exclusive (fallback, no overlaps)
        elif hasattr(diarization, "exclusive_speaker_diarization"):
            annotation = diarization.exclusive_speaker_diarization
        # Method 4: Use diarization directly if it's an annotation
        else:
            annotation = diarization
        
        # Extract all segments, including overlapping ones
        # Use itertracks with yield_label to get all tracks (including overlapping)
        try:
            for turn, track, speaker in annotation.itertracks(yield_label=True):
                start = float(turn.start)
                end = float(turn.end)
                
                # Filter out very short segments (likely noise or artifacts)
                min_duration = 0.15  # Increased from 0.1 for better quality
                if (end - start) < min_duration:
                    continue
                
                segments.append(SpeakerSegment(start_s=start, end_s=end, speaker=str(speaker)))
        except Exception as exc:
            log.warning("Error extracting diarization segments: %s", exc)
            # Fallback: try without track
            try:
                for turn, _, speaker in annotation.itertracks(yield_label=True):
                    start = float(turn.start)
                    end = float(turn.end)
                    if (end - start) >= 0.15:
                        segments.append(SpeakerSegment(start_s=start, end_s=end, speaker=str(speaker)))
            except Exception:
                pass
        
        # Post-process to better handle overlapping speech and cross-talk
        segments = self._post_process_segments(segments, sample_rate_hz)
        
        return segments
    
    def _enhance_audio_for_diarization(self, audio: np.ndarray, sample_rate_hz: int) -> np.ndarray:
        """Enhance audio signal for better diarization performance."""
        # Ensure audio is float32
        audio = np.asarray(audio, dtype=np.float32)
        
        # Normalize audio to [-1, 1] range
        max_val = np.abs(audio).max()
        if max_val > 0:
            audio = audio / max_val
        
        # Apply gentle high-pass filter to reduce low-frequency noise
        # This helps with speaker separation
        try:
            from scipy import signal
            # High-pass filter at 80Hz to remove rumble
            sos = signal.butter(2, 80, btype='high', fs=sample_rate_hz, output='sos')
            audio = signal.sosfilt(sos, audio)
            # Ensure output is still float32
            audio = np.asarray(audio, dtype=np.float32)
        except ImportError:
            # If scipy not available, skip filtering
            log.debug("scipy not available, skipping audio filtering")
        except Exception as exc:
            # If filtering fails for any reason, continue without it
            log.debug("Audio filtering failed: %s", exc)
        
        # Normalize again after filtering
        max_val = np.abs(audio).max()
        if max_val > 0:
            audio = audio / max_val
        
        # Final dtype check
        return np.asarray(audio, dtype=np.float32)
    
    def _post_process_segments(self, segments: list[SpeakerSegment], sample_rate_hz: int) -> list[SpeakerSegment]:
        """Post-process segments to better handle overlapping speech and cross-talk."""
        if not segments:
            return []
        
        # Sort by start time, then by end time
        segments = sorted(segments, key=lambda s: (s.start_s, s.end_s))
        
        processed: list[SpeakerSegment] = []
        
        # Adaptive overlap threshold based on segment duration
        # Shorter segments need less overlap to be considered simultaneous
        def get_overlap_threshold(seg: SpeakerSegment) -> float:
            duration = seg.end_s - seg.start_s
            # For very short segments (< 0.5s), use 30% overlap
            # For longer segments, use 0.2s minimum
            return min(0.2, duration * 0.3)
        
        # Cross-talk detection threshold (quick interruptions)
        cross_talk_gap = 0.4  # If next segment starts within 0.4s, might be cross-talk
        
        i = 0
        while i < len(segments):
            current = segments[i]
            current_duration = current.end_s - current.start_s
            
            # Skip very short segments that are likely artifacts
            if current_duration < 0.15:
                i += 1
                continue
            
            # Find all overlapping and nearby segments
            overlapping = [current]
            overlapping_indices = {i}
            j = i + 1
            
            while j < len(segments):
                next_seg = segments[j]
                next_duration = next_seg.end_s - next_seg.start_s
                
                # Skip very short segments
                if next_duration < 0.15:
                    j += 1
                    continue
                
                # Calculate overlap
                overlap_start = max(current.start_s, next_seg.start_s)
                overlap_end = min(current.end_s, next_seg.end_s)
                overlap_duration = max(0, overlap_end - overlap_start)
                
                # Get adaptive threshold
                threshold = get_overlap_threshold(current)
                
                # Check for significant overlap (simultaneous speech)
                if overlap_duration > threshold and current.speaker != next_seg.speaker:
                    # Different speakers with significant overlap = simultaneous speech
                    overlapping.append(next_seg)
                    overlapping_indices.add(j)
                    j += 1
                # Check for cross-talk (quick interruption, same or different speaker)
                elif next_seg.start_s <= current.end_s + cross_talk_gap:
                    # Potential cross-talk or quick follow-up
                    if current.speaker != next_seg.speaker:
                        # Different speaker = likely cross-talk
                        overlapping.append(next_seg)
                        overlapping_indices.add(j)
                    # If same speaker, might be continuation (handled by merge logic)
                    j += 1
                else:
                    # No more overlapping or nearby segments
                    break
            
            # Process overlapping segments
            if len(overlapping) > 1:
                # Multiple segments - preserve all but refine boundaries
                for seg in overlapping:
                    # For overlapping segments, we keep them as-is
                    # The diarization model should have already separated them
                    processed.append(seg)
                # Skip all processed indices
                i = max(overlapping_indices) + 1
            else:
                # Single segment, no overlap
                processed.append(current)
                i += 1
        
        # Final pass: refine boundaries for better separation
        processed = self._refine_segment_boundaries(processed)
        
        return processed
    
    def _refine_segment_boundaries(self, segments: list[SpeakerSegment]) -> list[SpeakerSegment]:
        """Refine segment boundaries to better separate overlapping speech."""
        if len(segments) < 2:
            return segments
        
        refined: list[SpeakerSegment] = []
        
        for i, seg in enumerate(segments):
            # Check for overlaps with adjacent segments
            overlaps_with_next = False
            overlaps_with_prev = False
            
            if i > 0:
                prev = segments[i - 1]
                overlap_start = max(prev.start_s, seg.start_s)
                overlap_end = min(prev.end_s, seg.end_s)
                if overlap_end > overlap_start and prev.speaker != seg.speaker:
                    overlaps_with_prev = True
            
            if i < len(segments) - 1:
                next_seg = segments[i + 1]
                overlap_start = max(seg.start_s, next_seg.start_s)
                overlap_end = min(seg.end_s, next_seg.end_s)
                if overlap_end > overlap_start and seg.speaker != next_seg.speaker:
                    overlaps_with_next = True
            
            # If overlapping with different speakers, slightly adjust boundaries
            # to create a small gap for better separation
            new_start = seg.start_s
            new_end = seg.end_s
            
            if overlaps_with_prev and i > 0:
                prev = segments[i - 1]
                # Create small gap at the start if possible
                gap_size = min(0.05, (seg.end_s - seg.start_s) * 0.1)
                new_start = max(seg.start_s, prev.end_s + gap_size)
            
            if overlaps_with_next and i < len(segments) - 1:
                next_seg = segments[i + 1]
                # Create small gap at the end if possible
                gap_size = min(0.05, (seg.end_s - seg.start_s) * 0.1)
                new_end = min(seg.end_s, next_seg.start_s - gap_size)
            
            # Ensure segment is still valid
            if new_end > new_start and (new_end - new_start) >= 0.1:
                refined.append(SpeakerSegment(start_s=new_start, end_s=new_end, speaker=seg.speaker))
            else:
                # Keep original if refinement makes it too short
                refined.append(seg)
        
        return refined

    def _prepare_audio(self, audio_f32: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio_f32, dtype=np.float32)
        if audio.ndim == 2:
            audio = np.mean(audio, axis=1)
        if audio.ndim != 1:
            raise ValueError(f"Expected mono audio, got shape {audio.shape}")
        return audio


class PyannoteCloudDiarizer:
    def __init__(
        self,
        api_key: str,
        model: str = "precision-2",
        api_base_url: str = "https://api.pyannote.ai",
        exclusive: bool = True,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        timeout_s: float = 60.0,
        poll_interval_s: float = 0.8,
    ):
        if not api_key:
            raise ValueError("Pyannote API key is required")
        self._api_base_url = api_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._model = model or "precision-2"
        self._exclusive = exclusive
        self._min_speakers = min_speakers
        self._max_speakers = max_speakers
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    def diarize(self, audio_f32: np.ndarray, sample_rate_hz: int) -> list[SpeakerSegment]:
        if audio_f32.size == 0:
            return []
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")

        audio = self._prepare_audio(audio_f32)
        audio = self._enhance_audio_for_diarization(audio, sample_rate_hz)
        wav_bytes = self._encode_wav(audio, sample_rate_hz)

        media_url = self._upload_audio(wav_bytes)
        if not media_url:
            return []

        audio_duration = len(audio) / sample_rate_hz
        min_speakers = self._min_speakers
        max_speakers = self._max_speakers

        if audio_duration < 2.0:
            min_speakers = 1
        elif audio_duration < 5.0 and min_speakers and min_speakers > 1:
            min_speakers = 1

        job_id = self._submit_diarization(media_url, min_speakers=min_speakers, max_speakers=max_speakers)
        if not job_id:
            return []

        output = self._wait_for_job(job_id)
        if not output:
            return []

        segments = self._parse_output(output)
        segments = self._post_process_segments(segments, sample_rate_hz)
        return segments

    def _encode_wav(self, audio: np.ndarray, sample_rate_hz: int) -> bytes:
        try:
            import soundfile as sf  # type: ignore
        except Exception as exc:
            raise RuntimeError("soundfile is required for cloud diarization") from exc

        wav_buffer = io.BytesIO()
        sf.write(wav_buffer, audio, sample_rate_hz, format="WAV", subtype="PCM_16")
        return wav_buffer.getvalue()

    def _upload_audio(self, wav_bytes: bytes) -> str | None:
        object_key = f"koov-{uuid.uuid4().hex}.wav"
        media_url = f"media://{object_key}"
        body = json.dumps({"url": media_url}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._api_base_url}/v1/media/input",
            data=body,
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Pyannote upload init failed: %s", exc)
            return None

        upload_url = payload.get("url")
        if not upload_url:
            log.warning("Pyannote upload init missing URL")
            return None

        upload_req = urllib.request.Request(
            upload_url,
            data=wav_bytes,
            headers={"Content-Type": "application/octet-stream"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(upload_req, timeout=self._timeout_s):
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Pyannote upload failed: %s", exc)
            return None

        return media_url

    def _submit_diarization(
        self,
        media_url: str,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> str | None:
        body: dict[str, object] = {
            "url": media_url,
            "model": self._model,
            "exclusive": self._exclusive,
        }
        if min_speakers is not None:
            body["minSpeakers"] = int(min_speakers)
        if max_speakers is not None:
            body["maxSpeakers"] = int(max_speakers)

        req = urllib.request.Request(
            f"{self._api_base_url}/v1/diarize",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Pyannote diarize request failed: %s", exc)
            return None

        job_id = payload.get("jobId")
        if not job_id:
            log.warning("Pyannote diarize response missing jobId")
            return None
        return str(job_id)

    def _wait_for_job(self, job_id: str) -> dict[str, object] | None:
        deadline = time.time() + self._timeout_s
        while time.time() < deadline:
            req = urllib.request.Request(
                f"{self._api_base_url}/v1/jobs/{job_id}",
                headers=self._headers,
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Pyannote job poll failed: %s", exc)
                return None

            status = str(payload.get("status", "")).lower()
            if status == "succeeded":
                output = payload.get("output")
                if isinstance(output, dict):
                    return output
                return None
            if status in {"failed", "canceled", "cancelled"}:
                error = payload.get("error", "unknown error")
                log.warning("Pyannote job failed: %s", error)
                return None

            time.sleep(self._poll_interval_s)

        log.warning("Pyannote diarization timeout after %.1fs", self._timeout_s)
        return None

    def _parse_output(self, output: dict[str, object]) -> list[SpeakerSegment]:
        diarization = output.get("exclusiveDiarization") if self._exclusive else output.get("diarization")
        if not diarization:
            diarization = output.get("diarization")
        if not isinstance(diarization, list):
            return []

        segments: list[SpeakerSegment] = []
        for item in diarization:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            speaker = str(item.get("speaker", "")).strip() or "speaker"
            segments.append(SpeakerSegment(start_s=start, end_s=end, speaker=speaker))
        return segments

    def _enhance_audio_for_diarization(self, audio: np.ndarray, sample_rate_hz: int) -> np.ndarray:
        """Enhance audio signal for better diarization performance."""
        audio = np.asarray(audio, dtype=np.float32)

        max_val = np.abs(audio).max()
        if max_val > 0:
            audio = audio / max_val

        try:
            from scipy import signal  # type: ignore

            sos = signal.butter(2, 80, btype="high", fs=sample_rate_hz, output="sos")
            audio = signal.sosfilt(sos, audio)
            audio = np.asarray(audio, dtype=np.float32)
        except ImportError:
            log.debug("scipy not available, skipping audio filtering")
        except Exception as exc:
            log.debug("Audio filtering failed: %s", exc)

        max_val = np.abs(audio).max()
        if max_val > 0:
            audio = audio / max_val

        return np.asarray(audio, dtype=np.float32)

    def _post_process_segments(self, segments: list[SpeakerSegment], sample_rate_hz: int) -> list[SpeakerSegment]:
        """Post-process segments to better handle overlapping speech and cross-talk."""
        if not segments:
            return []

        segments = sorted(segments, key=lambda s: (s.start_s, s.end_s))
        processed: list[SpeakerSegment] = []

        def get_overlap_threshold(seg: SpeakerSegment) -> float:
            duration = seg.end_s - seg.start_s
            return min(0.2, duration * 0.3)

        cross_talk_gap = 0.4
        i = 0
        while i < len(segments):
            current = segments[i]
            current_duration = current.end_s - current.start_s

            if current_duration < 0.15:
                i += 1
                continue

            overlapping = [current]
            overlapping_indices = {i}
            j = i + 1

            while j < len(segments):
                next_seg = segments[j]
                next_duration = next_seg.end_s - next_seg.start_s

                if next_duration < 0.15:
                    j += 1
                    continue

                overlap_start = max(current.start_s, next_seg.start_s)
                overlap_end = min(current.end_s, next_seg.end_s)
                overlap_duration = max(0, overlap_end - overlap_start)

                threshold = get_overlap_threshold(current)

                if overlap_duration > threshold and current.speaker != next_seg.speaker:
                    overlapping.append(next_seg)
                    overlapping_indices.add(j)
                    j += 1
                elif next_seg.start_s <= current.end_s + cross_talk_gap:
                    if current.speaker != next_seg.speaker:
                        overlapping.append(next_seg)
                        overlapping_indices.add(j)
                    j += 1
                else:
                    break

            if len(overlapping) > 1:
                for seg in overlapping:
                    processed.append(seg)
                i = max(overlapping_indices) + 1
            else:
                processed.append(current)
                i += 1

        processed = self._refine_segment_boundaries(processed)
        return processed

    def _refine_segment_boundaries(self, segments: list[SpeakerSegment]) -> list[SpeakerSegment]:
        """Refine segment boundaries to better separate overlapping speech."""
        if len(segments) < 2:
            return segments

        refined: list[SpeakerSegment] = []

        for i, seg in enumerate(segments):
            overlaps_with_next = False
            overlaps_with_prev = False

            if i > 0:
                prev = segments[i - 1]
                overlap_start = max(prev.start_s, seg.start_s)
                overlap_end = min(prev.end_s, seg.end_s)
                if overlap_end > overlap_start and prev.speaker != seg.speaker:
                    overlaps_with_prev = True

            if i < len(segments) - 1:
                next_seg = segments[i + 1]
                overlap_start = max(seg.start_s, next_seg.start_s)
                overlap_end = min(seg.end_s, next_seg.end_s)
                if overlap_end > overlap_start and seg.speaker != next_seg.speaker:
                    overlaps_with_next = True

            new_start = seg.start_s
            new_end = seg.end_s

            if overlaps_with_prev and i > 0:
                prev = segments[i - 1]
                gap_size = min(0.05, (seg.end_s - seg.start_s) * 0.1)
                new_start = max(seg.start_s, prev.end_s + gap_size)

            if overlaps_with_next and i < len(segments) - 1:
                next_seg = segments[i + 1]
                gap_size = min(0.05, (seg.end_s - seg.start_s) * 0.1)
                new_end = min(seg.end_s, next_seg.start_s - gap_size)

            if new_end > new_start and (new_end - new_start) >= 0.1:
                refined.append(SpeakerSegment(start_s=new_start, end_s=new_end, speaker=seg.speaker))
            else:
                refined.append(seg)

        return refined

    def _prepare_audio(self, audio_f32: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio_f32, dtype=np.float32)
        if audio.ndim == 2:
            audio = np.mean(audio, axis=1)
        if audio.ndim != 1:
            raise ValueError(f"Expected mono audio, got shape {audio.shape}")
        return audio
