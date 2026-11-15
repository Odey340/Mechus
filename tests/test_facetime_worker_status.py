"""FaceTime worker heartbeat fields remain stable across incremental writes."""

from koov.facetime_worker import _facetime_status_payload


def test_ready_persists_while_waiting_and_clears_after_admission() -> None:
    waiting = _facetime_status_payload(
        {"facetime_nav_url": "https://facetime.apple.com/join#redacted"},
        status="joining",
        meeting_id="m1",
        meeting_link="https://facetime.apple.com/join#secret",
        now=1.0,
        extra={
            "facetime_state": "waiting_for_admit",
            "facetime_join_clicked": True,
        },
    )
    assert waiting["facetime_ready"] is True

    heartbeat = _facetime_status_payload(
        waiting,
        status="joining",
        meeting_id="m1",
        meeting_link="https://facetime.apple.com/join#secret",
        now=2.0,
        extra={"facetime_state": "waiting_for_admit"},
    )
    assert heartbeat["facetime_ready"] is True
    assert heartbeat["facetime_nav_url"] == "https://facetime.apple.com/join#redacted"

    admitted = _facetime_status_payload(
        heartbeat,
        status="active",
        meeting_id="m1",
        meeting_link="https://facetime.apple.com/join#secret",
        now=3.0,
        extra={"facetime_state": "in_call"},
    )
    assert admitted["facetime_ready"] is False
    assert admitted["facetime_phase"] == "in_meeting"


def test_env_file_quotes_are_stripped_for_koov_keys() -> None:
    from koov.facetime_worker import _strip_env_file_quotes

    env = {
        "KOOV_ELEVEN_VOICE_ID": '"abc123"',
        "KOOV_ACTIONABLE_PHRASES": "'a, b'",
        "KOOV_PLAIN": "value",
        "KOOV_MISMATCHED": "\"oops'",
        "KOOV_ONE": '"',
        "PATH": '"/usr/bin"',
    }
    changed = _strip_env_file_quotes(env)
    assert sorted(changed) == ["KOOV_ACTIONABLE_PHRASES", "KOOV_ELEVEN_VOICE_ID"]
    assert env["KOOV_ELEVEN_VOICE_ID"] == "abc123"
    assert env["KOOV_ACTIONABLE_PHRASES"] == "a, b"
    assert env["KOOV_MISMATCHED"] == "\"oops'"
    assert env["KOOV_ONE"] == '"'
    assert env["PATH"] == '"/usr/bin"'


def test_admit_timeout_ignores_stalls_but_has_a_hard_cap() -> None:
    from koov.facetime_worker import _admit_timed_out

    # Normal wait: times out once active time reaches the limit.
    assert not _admit_timed_out(elapsed_s=179, stalled_s=0, current_pass_s=1, timeout_s=180)
    assert _admit_timed_out(elapsed_s=180, stalled_s=0, current_pass_s=1, timeout_s=180)
    # A 120s WebDriver stall right as the call connects is not counted.
    assert not _admit_timed_out(elapsed_s=290, stalled_s=115, current_pass_s=1, timeout_s=180)
    # The pass in progress being blocked is not counted either.
    assert not _admit_timed_out(elapsed_s=250, stalled_s=0, current_pass_s=125, timeout_s=180)
    # Permanently saturated browser: hard cap (4x) still ends the wait.
    assert not _admit_timed_out(elapsed_s=719, stalled_s=690, current_pass_s=1, timeout_s=180)
    assert _admit_timed_out(elapsed_s=720, stalled_s=700, current_pass_s=1, timeout_s=180)
    # Disabled timeout never fires.
    assert not _admit_timed_out(elapsed_s=99999, stalled_s=0, current_pass_s=0, timeout_s=0)
