"""Selenium controller for FaceTime web camera selection and toggling.

Browser JavaScript only *collects* candidate controls; the decision about
which control is the camera toggle and what state it reports is made by the
pure :func:`classify_camera_button` so it can be unit tested without a browser.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class CameraBrowserState:
    enumerated: bool = False
    selected: bool = False
    enabled: bool = False
    label: str = ""
    error_code: str = ""
    error: str = ""

    def to_status(self) -> dict[str, object]:
        return asdict(self)


# Label phrases that describe the *action* a button performs (checked first,
# because "turn camera off" also contains the state phrase "camera off").
_ACTION_MEANS_CURRENTLY_OFF = (
    "turn camera on", "turn on camera", "turn video on", "turn on video",
    "start video", "start camera", "enable camera", "enable video", "unmute video",
)
_ACTION_MEANS_CURRENTLY_ON = (
    "turn camera off", "turn off camera", "turn video off", "turn off video",
    "stop video", "stop camera", "disable camera", "disable video", "mute video",
)
_STATE_OFF = ("camera off", "video off", "camera is off", "video is off")
_STATE_ON = ("camera on", "video on", "camera is on", "video is on")
_EXCLUDE = (
    "device", "effect", "select", "choose", "menu", "setting", "share", "background",
    "portrait", "studio", "reaction", "filter", "source",
)
_PLAIN_TOGGLE = re.compile(r"^(camera|video)$")


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("’", "'").split())


def classify_camera_button(label: str, pressed: str | None = None) -> bool | None:
    """Return the camera state a control reports: True=on, False=off, None=not a toggle."""
    text = _norm(label)
    if not text or not ("camera" in text or "video" in text):
        return None
    if any(word in text for word in _EXCLUDE):
        return None
    if any(p in text for p in _ACTION_MEANS_CURRENTLY_OFF):
        return False
    if any(p in text for p in _ACTION_MEANS_CURRENTLY_ON):
        return True
    if any(p in text for p in _STATE_OFF):
        return False
    if any(p in text for p in _STATE_ON):
        return True
    if _PLAIN_TOGGLE.match(text):
        if pressed == "true":
            return True
        if pressed == "false":
            return False
    return None


def pick_camera_toggle(candidates: list[dict[str, Any]]) -> tuple[int | None, bool | None, str]:
    """Choose one toggle from collected candidates.

    Returns ``(index, current_state, error)``. Refuses when candidates disagree
    about the camera state, since clicking would then be a guess.
    """
    classified: list[tuple[int, bool]] = []
    for item in candidates or []:
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        state = classify_camera_button(str(item.get("label") or ""), item.get("pressed"))
        if state is not None:
            classified.append((idx, state))
    if not classified:
        return None, None, "FaceTime camera toggle was not found"
    states = {state for _, state in classified}
    if len(states) > 1:
        return None, None, "FaceTime camera controls are ambiguous"
    return classified[0][0], classified[0][1], ""


# Collect visible button-like controls mentioning camera/video, skipping
# anything inside participant tiles/rosters. Each candidate is tagged with a
# data attribute so a later click targets exactly the classified element.
_COLLECT_JS = r"""
const nodes = Array.from(document.querySelectorAll('button,ui-button,[role="button"],[role="switch"]'));
const out = [];
let i = 0;
for (const el of nodes) {
  el.removeAttribute('data-mechus-cam-idx');
  if (!el || !el.offsetParent) continue;
  if (el.closest('video,[class*="tile"],[class*="participant"],[class*="roster"],[class*="Tile"]')) continue;
  const label = ((el.getAttribute('aria-label') || '') + ' ' +
                 (el.getAttribute('title') || '') + ' ' +
                 (el.innerText || '')).replace(/\s+/g, ' ').trim();
  const low = label.toLowerCase();
  if (!(low.includes('camera') || low.includes('video'))) continue;
  el.setAttribute('data-mechus-cam-idx', String(i));
  out.push({i: i, label: label.slice(0, 160),
            pressed: el.getAttribute('aria-pressed') || el.getAttribute('aria-checked')});
  i += 1;
}
return out;
"""

_CLICK_JS = r"""
const el = document.querySelector('[data-mechus-cam-idx="' + String(arguments[0]) + '"]');
if (!el || !el.offsetParent) return false;
el.scrollIntoView({block:'center', inline:'nearest'});
el.click();
return true;
"""

_DIAGNOSTIC_JS = r"""
function clean(el) {
  const c = el.cloneNode(true);
  for (const n of [c, ...c.querySelectorAll('*')]) {
    for (const a of Array.from(n.attributes || [])) {
      if (!['id','class','role','aria-label','aria-pressed','aria-checked','title'].includes(a.name)) {
        n.removeAttribute(a.name);
      }
    }
    if (n.tagName === 'VIDEO' || n.tagName === 'IMG' || n.tagName === 'CANVAS') n.replaceChildren();
  }
  return c.outerHTML.slice(0, 4000);
}
const parts = [];
const sel = document.querySelector('#video-device-selector');
if (sel) parts.push(clean(sel));
for (const el of Array.from(document.querySelectorAll('button,ui-button,[role="button"],[role="menuitemradio"],[role="menuitem"]'))) {
  const t = ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).toLowerCase();
  if (t.includes('camera') || t.includes('video')) parts.push(clean(el));
  if (parts.length > 40) break;
}
return parts.join('\n');
"""


class FaceTimeCameraController:
    def __init__(self, driver: Any, *, label_hint: str = "mechus") -> None:
        self.driver = driver
        self.label_hint = (label_hint or "mechus").strip().lower()

    def enumerate_video_inputs(self) -> list[str]:
        devices = self.driver.execute_async_script(
            """
            const done = arguments[0];
            if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
              done([]); return;
            }
            navigator.mediaDevices.enumerateDevices()
              .then(ds => done(ds.filter(d => d.kind === 'videoinput').map(d => d.label || '')))
              .catch(() => done([]));
            """
        )
        return [str(item).strip() for item in (devices or []) if str(item).strip()]

    def selection_label(self) -> str:
        value = self.driver.execute_script(
            """
            const root = document.querySelector('#video-device-selector');
            if (!root) return '';
            const value = root.querySelector('.value');
            return ((value ? value.innerText : root.innerText) || '').trim();
            """
        )
        return str(value or "").strip()

    def diagnostic_markup(self) -> str:
        """Sanitized camera-related markup for tuning selectors (no media, no URLs)."""
        try:
            return str(self.driver.execute_script(_DIAGNOSTIC_JS) or "")[:60000]
        except Exception as exc:  # noqa: BLE001
            return f"<!-- diagnostic capture failed: {type(exc).__name__} -->"

    def select(self) -> CameraBrowserState:
        state = CameraBrowserState()
        try:
            labels = self.enumerate_video_inputs()
            matching = [label for label in labels if self.label_hint in label.lower()]
            if not matching:
                state.error_code = "camera_not_enumerated"
                state.error = "Mechus virtual camera was not enumerated by Chromium"
                return state
            state.enumerated = True
            state.label = matching[0][:160]
            # With exactly one camera Chromium has nothing else to choose, and
            # FaceTime may not render a selector at all.
            if len(labels) == 1:
                state.selected = True
                return state
            current = self.selection_label()
            if self.label_hint in current.lower():
                state.selected = True
                return state
            opened = bool(
                self.driver.execute_script(
                    """
                    const root = document.querySelector('#video-device-selector');
                    if (!root || !root.offsetParent) return false;
                    const button = root.querySelector('ui-button,button,[role="button"]') || root;
                    button.scrollIntoView({block:'center', inline:'nearest'});
                    button.click();
                    return true;
                    """
                )
            )
            if not opened:
                state.error_code = "camera_selector_failed"
                state.error = "FaceTime video device selector was not visible"
                return state
            chosen = self.driver.execute_script(
                """
                const hint = String(arguments[0] || 'mechus').toLowerCase();
                const candidates = Array.from(document.querySelectorAll(
                  'ui-menu-item[role="menuitem"],ui-menu-item[role="menuitemradio"],' +
                  '[role="menuitemradio"],[role="menuitem"]'
                ));
                for (const item of candidates) {
                  if (!item || !item.offsetParent) continue;
                  const labelNode = item.querySelector('.unselected-item-label,.selected-item-label');
                  const text = ((labelNode ? labelNode.innerText : item.innerText) || '').trim();
                  if (text.toLowerCase().includes(hint)) {
                    item.scrollIntoView({block:'center', inline:'nearest'});
                    item.click();
                    return text;
                  }
                }
                return '';
                """,
                self.label_hint,
            )
            try:
                self.driver.execute_script(
                    """
                    const done = document.querySelector(
                      '#extended-menu-leave-button,ui-button.close-button,button.close-button'
                    );
                    if (done && done.offsetParent) done.click();
                    """
                )
            except Exception:
                pass
            after = self.selection_label()
            state.selected = bool(chosen) and self.label_hint in after.lower()
            if not state.selected:
                state.error_code = "camera_selector_failed"
                state.error = "FaceTime did not retain the selected Mechus camera"
            return state
        except Exception as exc:
            state.error_code = "camera_selector_failed"
            state.error = str(exc)[:500]
            return state

    def _toggle_candidates(self) -> list[dict[str, Any]]:
        value = self.driver.execute_script(_COLLECT_JS)
        return [item for item in (value or []) if isinstance(item, dict)]

    def camera_enabled(self) -> bool | None:
        _, current, error = pick_camera_toggle(self._toggle_candidates())
        return None if error else current

    def set_enabled(self, enabled: bool, *, require_selection: bool = True) -> CameraBrowserState:
        state = self.select()
        if require_selection and not state.selected:
            return state
        try:
            idx, current, error = pick_camera_toggle(self._toggle_candidates())
            if error:
                state.error_code = "camera_toggle_failed"
                state.error = error
                return state
            if current is enabled:
                state.enabled = enabled
                return state
            clicked = bool(self.driver.execute_script(_CLICK_JS, idx))
            actual = self.camera_enabled()
            state.enabled = actual is True
            if not clicked:
                state.error_code = "camera_toggle_failed"
                state.error = "FaceTime camera toggle disappeared before it could be clicked"
            elif actual is not enabled:
                state.error_code = "camera_toggle_failed"
                state.error = "FaceTime camera state did not match the request"
            else:
                state.error_code = ""
                state.error = ""
            return state
        except Exception as exc:
            state.error_code = "camera_toggle_failed"
            state.error = str(exc)[:500]
            return state
