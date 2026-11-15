import pytest

from mechus.facetime_camera import (
    FaceTimeCameraController,
    classify_camera_button,
    pick_camera_toggle,
)


@pytest.mark.parametrize(
    "label,pressed,expected",
    [
        ("Turn Camera Off", None, True),
        ("Turn camera on", None, False),
        ("Stop Video", None, True),
        ("Start Video", None, False),
        ("Camera Off", None, False),
        ("Camera is on", None, True),
        ("Camera", "true", True),
        ("Camera", "false", False),
        ("Camera", None, None),
        ("Video Effects", None, None),
        ("Select video device", None, None),
        ("Camera settings", None, None),
        ("Mute", None, None),
        ("", None, None),
        ("Turn camera off’", None, True),
    ],
)
def test_classify_camera_button(label, pressed, expected):
    assert classify_camera_button(label, pressed) is expected


def test_pick_toggle_chooses_classified_candidate():
    idx, state, err = pick_camera_toggle([
        {"i": 0, "label": "Video Effects", "pressed": None},
        {"i": 1, "label": "Turn Camera Off", "pressed": None},
    ])
    assert (idx, state, err) == (1, True, "")


def test_pick_toggle_refuses_ambiguous_controls():
    idx, state, err = pick_camera_toggle([
        {"i": 0, "label": "Turn Camera Off"},
        {"i": 1, "label": "Turn Camera On"},
    ])
    assert idx is None and state is None and "ambiguous" in err


def test_pick_toggle_reports_missing():
    assert pick_camera_toggle([{"i": 0, "label": "Video Effects"}])[2]


class FakeDriver:
    """Minimal scripted WebDriver: answers by recognizing which script ran."""

    def __init__(self, *, labels, selection="", controls=None, menu_hit="Mechus Virtual Camera 1"):
        self.labels = labels
        self.selection = selection
        self.controls = controls if controls is not None else [{"i": 0, "label": "Turn Camera On"}]
        self.menu_hit = menu_hit
        self.clicked: list[int] = []
        self.selector_visible = True

    def execute_async_script(self, script, *args):
        return list(self.labels)

    def execute_script(self, script, *args):
        if "data-mechus-cam-idx\"=" in script or "querySelector('[data-mechus-cam-idx" in script:
            idx = args[0]
            self.clicked.append(idx)
            # Toggling flips every control's reported state.
            for c in self.controls:
                low = c["label"].lower()
                c["label"] = "Turn Camera Off" if "on" in low.split()[-1:] else "Turn Camera On"
            return True
        if "setAttribute('data-mechus-cam-idx'" in script:
            return [dict(c) for c in self.controls]
        if "const value = root.querySelector('.value')" in script:
            return self.selection
        if "button.click();" in script and "#video-device-selector" in script:
            return self.selector_visible
        if "menuitemradio" in script and "hint" in script:
            if self.menu_hit:
                self.selection = self.menu_hit
            return self.menu_hit
        return None


def test_select_reports_not_enumerated():
    state = FaceTimeCameraController(FakeDriver(labels=["Integrated Camera"])).select()
    assert state.error_code == "camera_not_enumerated"
    assert not state.selected


def test_select_single_mechus_camera_needs_no_menu():
    state = FaceTimeCameraController(FakeDriver(labels=["Mechus Virtual Camera 1"])).select()
    assert state.selected and state.enumerated and not state.error_code


def test_select_via_menu_when_multiple_cameras():
    driver = FakeDriver(labels=["Other Cam", "Mechus Virtual Camera 1"], selection="Other Cam")
    state = FaceTimeCameraController(driver).select()
    assert state.selected, state


def test_select_fails_when_selector_hidden():
    driver = FakeDriver(labels=["Other Cam", "Mechus Virtual Camera 1"], selection="Other Cam")
    driver.selector_visible = False
    state = FaceTimeCameraController(driver).select()
    assert state.error_code == "camera_selector_failed"


def test_set_enabled_turns_camera_on_and_verifies():
    driver = FakeDriver(labels=["Mechus Virtual Camera 1"])
    state = FaceTimeCameraController(driver).set_enabled(True)
    assert driver.clicked == [0]
    assert state.enabled and not state.error_code


def test_set_enabled_is_noop_when_already_in_state():
    driver = FakeDriver(labels=["Mechus Virtual Camera 1"], controls=[{"i": 0, "label": "Turn Camera Off"}])
    state = FaceTimeCameraController(driver).set_enabled(True)
    assert driver.clicked == []
    assert state.enabled


def test_set_enabled_refuses_ambiguous_toggle():
    driver = FakeDriver(
        labels=["Mechus Virtual Camera 1"],
        controls=[{"i": 0, "label": "Turn Camera Off"}, {"i": 1, "label": "Turn Camera On"}],
    )
    state = FaceTimeCameraController(driver).set_enabled(False)
    assert driver.clicked == []
    assert state.error_code == "camera_toggle_failed"


def test_prejoin_off_does_not_require_selection():
    driver = FakeDriver(labels=[], controls=[{"i": 0, "label": "Turn Camera Off"}])
    state = FaceTimeCameraController(driver).set_enabled(False, require_selection=False)
    assert driver.clicked == [0]
    assert state.enabled is False and not state.error_code
