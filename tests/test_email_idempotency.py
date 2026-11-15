"""Meeting email delivery is idempotent across worker and API stop paths."""

from __future__ import annotations

import os
from unittest.mock import patch

from mechus.email_sender import _load_reminders, process_due_deadline_reminders, send_meeting_emails


class _FakeSMTP:
    sent: list[tuple[str, object]] = []

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self) -> None:
        pass

    def login(self, username: str, password: str) -> None:
        pass

    def sendmail(self, from_addr: str, recipients: object, message: str) -> None:
        self.sent.append((from_addr, recipients))


class _FailingSMTP(_FakeSMTP):
    def sendmail(self, from_addr: str, recipients: object, message: str) -> None:
        raise RuntimeError("temporary SMTP failure")


def test_worker_and_api_paths_send_only_once(tmp_path) -> None:
    _FakeSMTP.sent = []
    env = {
        "MECHUS_EMAIL_ENABLED": "1",
        "MECHUS_EMAIL_USERNAME": "mechus@example.com",
        "MECHUS_EMAIL_PASSWORD": "app-password",
        "MECHUS_EMAIL_RECIPIENTS": "",
        "MECHUS_NOTES_DIR": str(tmp_path),
    }
    summary = {"executive_summary": "Done", "action_items": []}

    with patch.dict(os.environ, env, clear=False), patch(
        "mechus.email_sender.smtplib.SMTP", _FakeSMTP
    ):
        first = send_meeting_emails(
            summary,
            "meeting-123",
            summary_recipients=["person@example.com"],
        )
        second = send_meeting_emails(
            summary,
            "meeting-123",
            summary_recipients=["person@example.com"],
        )

    assert first is True
    assert second is False
    assert len(_FakeSMTP.sent) == 1
    assert (tmp_path / "email_delivery" / "meeting-123.sent").exists()


def test_failed_attempt_releases_reservation_for_api_retry(tmp_path) -> None:
    _FakeSMTP.sent = []
    env = {
        "MECHUS_EMAIL_ENABLED": "1",
        "MECHUS_EMAIL_USERNAME": "mechus@example.com",
        "MECHUS_EMAIL_PASSWORD": "app-password",
        "MECHUS_EMAIL_RECIPIENTS": "person@example.com",
        "MECHUS_NOTES_DIR": str(tmp_path),
    }
    summary = {"executive_summary": "Done", "action_items": []}

    with patch.dict(os.environ, env, clear=False), patch(
        "mechus.email_sender.smtplib.SMTP", _FailingSMTP
    ):
        assert send_meeting_emails(summary, "meeting-retry") is False

    assert not (tmp_path / "email_delivery" / "meeting-retry.sending").exists()

    with patch.dict(os.environ, env, clear=False), patch(
        "mechus.email_sender.smtplib.SMTP", _FakeSMTP
    ):
        assert send_meeting_emails(summary, "meeting-retry") is True

    assert len(_FakeSMTP.sent) == 1


def test_deadline_reminder_queue_round_trip(tmp_path) -> None:
    _FakeSMTP.sent = []
    env = {
        "MECHUS_EMAIL_ENABLED": "1",
        "MECHUS_EMAIL_USERNAME": "mechus@example.com",
        "MECHUS_EMAIL_PASSWORD": "app-password",
        "MECHUS_EMAIL_RECIPIENTS": "",
        "MECHUS_NOTES_DIR": str(tmp_path),
    }
    summary = {
        "action_items": [{"owner": "Avery", "task": "Inspect the sink", "deadline": "2020-01-01"}]
    }
    with patch.dict(os.environ, env, clear=False), patch(
        "mechus.email_sender.smtplib.SMTP", _FakeSMTP
    ):
        assert send_meeting_emails(
            summary, "meeting-deadline", speaker_emails={"Avery": "avery@example.com"}
        ) is True
        queued = _load_reminders()
        assert len(queued) == 1 and queued[0]["sent_at"] is None
        process_due_deadline_reminders()
        assert _load_reminders()[0]["sent_at"]
    assert len(_FakeSMTP.sent) == 2
