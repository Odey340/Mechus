"""On-demand, non-retaining visual analysis for a FaceTime worker.

Images exist only in memory for the duration of one request. Queue files hold
the question and the text answer; they never contain image data.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

from koov.facetime_control import enqueue_vision_request, safe_control_id, vision_request_status


DEFAULT_VISION_MODEL = "gpt-4o-mini"
_VISION_DETAILS = frozenset({"low", "high", "auto"})


@dataclass(frozen=True)
class VisionResult:
    request_id: str
    status: str
    answer: str = ""
    error_code: str = ""
    error: str = ""
    completed_at: float = 0.0
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Voice intent
# ---------------------------------------------------------------------------

_VISUAL_VERBS = re.compile(
    r"\b(look at|looking at|take a look|have a look|look here|do you see|can you see|you see|"
    r"read|reading|show|showing|shown|describe|what'?s on|what is on|what'?s written|what is written)\b"
)
_VISUAL_TARGETS = re.compile(
    r"\b(whiteboard|white board|board|screen|camera|diagram|chart|drawing|sketch|slide|this|"
    r"holding|paper|page|document|sign|text|writing|picture|photo|image|object|here|"
    r"in front of|on my|in my hand)\b"
)
_NON_VISUAL = re.compile(r"\b(read back|read out the (summary|notes|action items)|see you|let'?s see)\b")


def is_visual_request(text: str) -> bool:
    """True only when a command clearly asks Koov to look at something visible.

    Requires both a visual verb and a visible target so that meeting questions
    like "show me the action items" or "see you tomorrow" never trigger capture.
    """
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    if not t or _NON_VISUAL.search(t):
        return False
    if re.search(r"\b(action items?|decisions?|summary|recap|risks?|agenda|transcript)\b", t):
        return False
    if "what am i holding" in t or "what do you see" in t or "can you see" in t:
        return True
    return bool(_VISUAL_VERBS.search(t) and _VISUAL_TARGETS.search(t))


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------


def usable_screenshot(png_bytes: bytes) -> tuple[bool, str]:
    if not png_bytes:
        return False, "empty screenshot"
    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:
        return False, f"invalid screenshot: {exc}"
    if image.width < 160 or image.height < 120:
        return False, "screenshot is too small"
    thumb = np.asarray(image.resize((64, 36)), dtype=np.float32)
    if float(thumb.std()) < 2.0 or float(thumb.mean()) < 2.0:
        return False, "screenshot is blank or nearly uniform"
    return True, ""


def crop_to_region(
    png_bytes: bytes,
    rects: Iterable[dict[str, Any]] | None,
    *,
    min_fraction: float = 0.15,
) -> bytes:
    """Crop to the largest visible remote video rectangle when it is reliable.

    ``rects`` are device-pixel rectangles ``{x, y, width, height}`` collected
    from the page. Falls back to the full viewport when no rectangle covers at
    least ``min_fraction`` of the screenshot. Always returns PNG bytes.
    """
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    W, H = image.size
    best: tuple[int, int, int, int] | None = None
    best_area = 0
    for rect in rects or []:
        try:
            x = int(round(float(rect.get("x", 0))))
            y = int(round(float(rect.get("y", 0))))
            w = int(round(float(rect.get("width", 0))))
            h = int(round(float(rect.get("height", 0))))
        except (AttributeError, TypeError, ValueError):
            continue
        left, top = max(0, x), max(0, y)
        right, bottom = min(W, x + w), min(H, y + h)
        if right - left < 32 or bottom - top < 32:
            continue
        area = (right - left) * (bottom - top)
        if area > best_area:
            best_area = area
            best = (left, top, right, bottom)
    if best is None or best_area < min_fraction * W * H:
        return png_bytes
    out = io.BytesIO()
    image.crop(best).save(out, format="PNG")
    return out.getvalue()


def prepare_screenshot(png_bytes: bytes, *, max_width: int = 1280, max_height: int = 900) -> bytes:
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=True)
    return output.getvalue()


# JavaScript run by the worker to find remote <video> tiles (device pixels).
VIDEO_RECTS_JS = r"""
const dpr = window.devicePixelRatio || 1;
const out = [];
for (const v of Array.from(document.querySelectorAll('video'))) {
  const r = v.getBoundingClientRect();
  if (!r || r.width < 48 || r.height < 48) continue;
  const st = window.getComputedStyle(v);
  if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
  // The local self-preview is muted and usually mirrored; skip it.
  const mirrored = /scaleX\(-1\)|matrix\(-1/.test(st.transform || '');
  if (v.muted && mirrored) continue;
  out.push({x: r.left * dpr, y: r.top * dpr, width: r.width * dpr, height: r.height * dpr});
}
return out;
"""


# ---------------------------------------------------------------------------
# Provider call
# ---------------------------------------------------------------------------


def vision_model_from_env() -> str:
    return (os.environ.get("KOOV_OPENAI_VISION_MODEL") or "").strip() or DEFAULT_VISION_MODEL


def vision_detail_from_env() -> str:
    detail = (os.environ.get("KOOV_FT_VISION_DETAIL") or "auto").strip().lower()
    return detail if detail in _VISION_DETAILS else "auto"


def analyze_image(
    image_bytes: bytes,
    question: str,
    *,
    api_key: str,
    model: str,
    timeout_s: float = 25.0,
    detail: str = "auto",
) -> str:
    """Call an OpenAI-compatible vision chat endpoint without retaining the image.

    Error messages never include the request body (which carries the image).
    """
    if not api_key.strip():
        raise RuntimeError("KOOV_OPENAI_API_KEY is not configured")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Koov, a meeting assistant looking at one frame of a video call. "
                    "Answer only the user's question about visible content in at most three short "
                    "spoken sentences. Say plainly when the image is unclear or the thing asked about "
                    "is not visible. Do not identify people, and do not infer emotion, age, health, "
                    "ethnicity, or other sensitive traits. Never solve CAPTCHAs."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": str(question)[:500]},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded}",
                            "detail": detail if detail in _VISION_DETAILS else "auto",
                        },
                    },
                ],
            },
        ],
        "max_tokens": 350,
        "temperature": 0.1,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
        method="POST",
    )
    del encoded, payload
    try:
        with urllib.request.urlopen(request, timeout=max(5.0, timeout_s)) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail_text = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"vision provider HTTP {exc.code}: {detail_text[:300]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"vision provider unreachable: {type(exc).__name__}") from None
    except ValueError:
        raise RuntimeError("vision provider returned invalid JSON") from None
    choices = value.get("choices") if isinstance(value, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("vision provider returned no answer")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    answer = str((message or {}).get("content") or "").strip() if isinstance(message, dict) else ""
    if not answer:
        raise RuntimeError("vision provider returned an empty answer")
    return answer[:2000]


# ---------------------------------------------------------------------------
# File queue (worker side)
# ---------------------------------------------------------------------------


def _queue_root(notes_dir: str | os.PathLike[str], meeting_id: str) -> Path:
    return Path(notes_dir) / f"worker-commands-{safe_control_id(meeting_id)}"


def claim_next_request(notes_dir: str, meeting_id: str) -> tuple[Path, dict[str, Any]] | None:
    root = _queue_root(notes_dir, meeting_id)
    pending = root / "pending"
    processing = root / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    try:
        candidates = sorted(pending.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    for path in candidates:
        target = processing / path.name
        try:
            os.replace(path, target)
        except OSError:
            continue
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            value = {}
        return target, value if isinstance(value, dict) else {}
    return None


def complete_request(path: Path, result: VisionResult) -> None:
    done = path.parent.parent / "done" / path.name
    done.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": result.request_id,
        "status": result.status,
        "answer": result.answer,
        "error_code": result.error_code,
        "error": result.error[:500],
        "completed_at": result.completed_at or time.time(),
        "latency_ms": result.latency_ms,
    }
    temp = done.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, done)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _fail(path: Path, request_id: str, code: str, message: str) -> None:
    complete_request(
        path,
        VisionResult(request_id, "failed", error_code=code, error=message, completed_at=time.time()),
    )


def recover_stale_requests(notes_dir: str, meeting_id: str, *, now: float | None = None) -> int:
    """Resolve requests a previous (crashed) worker left in ``processing/``.

    Unexpired requests are returned to ``pending`` once (marked ``recovered``);
    anything expired or already recovered is failed explicitly.
    """
    current = time.time() if now is None else now
    root = _queue_root(notes_dir, meeting_id)
    processing = root / "processing"
    count = 0
    for path in list(processing.glob("*.json")) if processing.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            value = {}
        request_id = str(value.get("request_id") or path.stem) if isinstance(value, dict) else path.stem
        expired = not isinstance(value, dict) or float(value.get("expires_at") or 0.0) <= current
        if expired or value.get("recovered"):
            _fail(path, request_id, "vision_worker_restarted", "Worker restarted before the request finished")
        else:
            value["recovered"] = True
            target = root / "pending" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        count += 1
    return count


def fail_inflight(notes_dir: str, meeting_id: str, *, reason: str = "Meeting ended") -> int:
    """Give every pending/processing request a terminal result during shutdown."""
    root = _queue_root(notes_dir, meeting_id)
    count = 0
    for state in ("processing", "pending"):
        folder = root / state
        if not folder.is_dir():
            continue
        for path in list(folder.glob("*.json")):
            _fail(path, path.stem, "vision_meeting_ended", reason)
            count += 1
    return count


def prune_done(notes_dir: str, meeting_id: str, *, max_age_s: float = 3600.0, now: float | None = None) -> int:
    current = time.time() if now is None else now
    folder = _queue_root(notes_dir, meeting_id) / "done"
    removed = 0
    for path in list(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            if current - path.stat().st_mtime > max_age_s:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Voice bridge (agent thread -> worker main loop)
# ---------------------------------------------------------------------------


class VoiceVisionBridge:
    """Lets the agent thread ask a visual question through the worker's queue.

    Selenium is not thread-safe, so the agent never touches the browser. It
    enqueues a request and waits; the worker's main loop captures and analyzes
    it exactly like an API-submitted request.
    """

    def __init__(
        self,
        notes_dir: str,
        meeting_id: str,
        *,
        max_per_minute: int = 3,
        poll_interval_s: float = 0.25,
    ) -> None:
        self.notes_dir = notes_dir
        self.meeting_id = meeting_id
        self.max_per_minute = max(1, int(max_per_minute))
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self._lock = threading.Lock()
        self._times: list[float] = []

    def _allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._times = [t for t in self._times if now - t < 60.0]
            if len(self._times) >= self.max_per_minute:
                return False
            self._times.append(now)
            return True

    def ask(self, question: str, *, timeout_s: float = 20.0) -> tuple[str | None, str]:
        """Return ``(answer, error_code)``. ``answer`` is None on any failure."""
        if not self._allow():
            return None, "vision_rate_limited"
        try:
            queued = enqueue_vision_request(
                self.notes_dir, self.meeting_id, question, ttl_s=max(5.0, timeout_s + 5.0)
            )
        except ValueError:
            return None, "vision_request_invalid"
        deadline = time.monotonic() + max(1.0, timeout_s)
        while time.monotonic() < deadline:
            status = vision_request_status(self.notes_dir, self.meeting_id, queued["request_id"])
            if status and status.get("status") == "completed":
                answer = str(status.get("answer") or "").strip()
                return (answer or None), ("" if answer else "vision_model_failed")
            if status and status.get("status") == "failed":
                return None, str(status.get("error_code") or "vision_model_failed")
            time.sleep(self.poll_interval_s)
        return None, "vision_timeout"
