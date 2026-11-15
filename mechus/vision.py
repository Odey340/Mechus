"""Vision for FaceTime Video calls: answer a spoken question from the caller's camera frame.

The Chert worker gives ``MechusAgent`` a frame provider (latest JPEG of the caller's camera).
When a wake command is about what Mechus can see, the agent calls :func:`answer_with_frame`
instead of the text-only responder.

Env:
  MECHUS_VISION_MODEL      OpenAI vision model (default ``gpt-4.1-mini``)
  MECHUS_VISION_ALWAYS     Send the frame with every question on a video call (default on;
                         ``0`` = only for questions that mention seeing/looking/showing)
  MECHUS_VISION_TIMEOUT_S  Request timeout (default 12)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

from mechus.brand import ASSISTANT_NAME

log = logging.getLogger(__name__)

_VISUAL_RE = re.compile(
    r"\b("
    r"see|seeing|look|looks|looking|watch|watching|show|showing|shown|holding|hold|wearing|wear|"
    r"camera|video|picture|image|screen|view|visible|"
    r"read (this|that|it|the)|what does (this|that|it) say|"
    r"what('s| is| are) (this|that|these|those)|what am i|where am i|who am i|"
    r"how many|what colou?r|which colou?r|describe|identify|recognize|recognise|"
    r"in front of (me|you)|behind me|next to me|around me|my (hand|face|shirt|room|desk|outfit)"
    r")\b",
    re.IGNORECASE,
)

_SYSTEM = (
    f"You are {ASSISTANT_NAME}, a voice assistant on a live FaceTime video call. The image is the "
    "caller's camera right now. Answer the caller's question from what you can see, in one "
    "or two short spoken sentences, plain words, no lists or markdown. If the question is about the scene "
    "but the image does not show it, say so briefly. If the question has nothing to do with "
    "the image, just answer it normally without describing the image. Ignore the small dark "
    "rounded box in a lower corner and the small icon in the top-left; they are call "
    "interface overlays, not part of the scene. Do not identify real people by name from "
    "their face; describe what is visible instead."
)


def vision_always() -> bool:
    # Default on: callers ask about the scene without saying "see" or "look"
    # ("any structural defects in the building?"), and a text-only answer is wrong there.
    return (os.environ.get("MECHUS_VISION_ALWAYS") or "1").strip().lower() in ("1", "true", "yes", "on")


def looks_visual(text: str) -> bool:
    """True when the spoken command is plausibly about what the camera shows."""
    return bool(_VISUAL_RE.search(text or ""))


def answer_with_frame(
    *,
    api_key: str,
    question: str,
    jpeg: bytes,
    context: str = "",
    model: str | None = None,
) -> str:
    """Ask the vision model ``question`` about ``jpeg``. Returns "" on failure."""
    if not api_key or not jpeg or not (question or "").strip():
        return ""
    model = (model or os.environ.get("MECHUS_VISION_MODEL") or "gpt-4.1-mini").strip()
    timeout_s = float(os.environ.get("MECHUS_VISION_TIMEOUT_S", "12") or "12")
    user_text = question.strip()
    if context.strip():
        user_text = f"Recent conversation:\n{context.strip()[-600:]}\n\nCaller asks: {user_text}"
    payload = {
        "model": model,
        "max_tokens": 120,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                            "detail": "low",
                        },
                    },
                ],
            },
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as exc:
        log.warning("Vision request failed: HTTP %s %s", exc.code, exc.read()[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("Vision request failed: %s", exc)
    return ""
