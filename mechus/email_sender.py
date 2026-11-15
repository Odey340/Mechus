"""
Email sender for Mechus — sends action item emails after meetings end.
Looks up participant emails from Supabase profiles table using their name.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import time
import hashlib
import json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

log = logging.getLogger(__name__)
_REMINDER_FILE = "email_deadline_reminders.json"


def _normalize_person_key(name: str) -> str:
    raw = " ".join((name or "").strip().lower().split())
    return re.sub(r"[^a-z0-9]+", "", raw)


def _notes_dir() -> str:
    return os.path.abspath(os.environ.get("MECHUS_NOTES_DIR", "mechus_notes"))


def _reminders_path() -> str:
    return os.path.join(_notes_dir(), _REMINDER_FILE)


def _load_reminders() -> list[dict[str, Any]]:
    """Load queued deadline reminders; malformed state never blocks API startup."""
    path = _reminders_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception:
        log.warning("Could not load deadline reminders from %s", path, exc_info=True)
    return []


def _save_reminders(reminders: list[dict[str, Any]]) -> None:
    os.makedirs(_notes_dir(), exist_ok=True)
    path = _reminders_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(reminders, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _parse_deadline_to_iso(deadline: str) -> str:
    value = (deadline or "").strip()
    if not value:
        return ""
    today = datetime.now(timezone.utc).date()
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d",
        "%b %d",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:
            candidate = parsed.replace(year=today.year).date()
            if candidate < today:
                candidate = parsed.replace(year=today.year + 1).date()
            return candidate.isoformat()
        return parsed.date().isoformat()
    return ""


def _owner_email(owner: str, emails: dict[str, Any] | None) -> str:
    owner_key = _normalize_person_key(owner)
    for name, raw in (emails or {}).items():
        if _normalize_person_key(str(name)) != owner_key:
            continue
        candidates = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for candidate in candidates:
            value = str(candidate or "").strip().lower()
            if "@" in value:
                return value
    return ""


def _enqueue_deadline_reminders(
    summary: dict,
    meeting_id: str,
    participant_emails: dict[str, Any] | None,
) -> None:
    if os.environ.get("MECHUS_EMAIL_DUE_REMINDERS_ENABLED", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return
    reminders = _load_reminders()
    known_ids = {str(item.get("id") or "") for item in reminders}
    added = 0
    for action in summary.get("action_items") or []:
        owner = str(action.get("owner") or "").strip()
        task = str(action.get("task") or "").strip()
        deadline = _parse_deadline_to_iso(str(action.get("deadline") or ""))
        email = _owner_email(owner, participant_emails)
        if not owner or not task or not deadline or not email:
            continue
        reminder_id = hashlib.sha256(
            f"{meeting_id}|{owner}|{task}|{deadline}|{email}".encode("utf-8")
        ).hexdigest()[:24]
        if reminder_id in known_ids:
            continue
        reminders.append({
            "id": reminder_id,
            "meeting_id": meeting_id,
            "owner": owner,
            "email": email,
            "task": task,
            "deadline": deadline,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sent_at": None,
        })
        known_ids.add(reminder_id)
        added += 1
    if added:
        _save_reminders(reminders)
        log.info("Queued %s deadline reminder(s) for meeting_id=%s", added, meeting_id)


def _build_deadline_reminder_email(owner: str, task: str, deadline: str) -> str:
    return (
        f"Hi {owner},\n\n"
        f"Reminder: this action item is due today ({deadline}).\n\n"
        f"  • {task}\n\n"
        "— Mechus"
    )


def process_due_deadline_reminders() -> None:
    """Send queued reminders that are due; safe to call repeatedly from the API loop."""
    if os.environ.get("MECHUS_EMAIL_ENABLED", "").strip() != "1":
        return
    if os.environ.get("MECHUS_EMAIL_DUE_REMINDERS_ENABLED", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return
    reminders = _load_reminders()
    today = datetime.now(timezone.utc).date().isoformat()
    due = [
        item for item in reminders
        if not item.get("sent_at") and str(item.get("deadline") or "") <= today
    ]
    if not due:
        return

    smtp_host = os.environ.get("MECHUS_EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("MECHUS_EMAIL_SMTP_PORT", "587"))
    username = (os.environ.get("MECHUS_EMAIL_USERNAME") or "").strip()
    password = _smtp_password()
    from_addr = (os.environ.get("MECHUS_EMAIL_FROM") or username).strip()
    if not username or not password:
        log.warning("Deadline reminders skipped — SMTP credentials missing")
        return

    sent = 0
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(username, password)
            for item in due:
                to_addr = str(item.get("email") or "").strip().lower()
                task = str(item.get("task") or "").strip()
                deadline = str(item.get("deadline") or "").strip()
                if not to_addr or not task or not deadline:
                    continue
                msg = MIMEMultipart()
                msg["From"] = from_addr
                msg["To"] = to_addr
                msg["Subject"] = "Reminder: action item due today — Mechus"
                msg.attach(MIMEText(_build_deadline_reminder_email(
                    str(item.get("owner") or "there"), task, deadline
                ), "plain"))
                server.sendmail(from_addr, [to_addr], msg.as_string())
                item["sent_at"] = datetime.now(timezone.utc).isoformat()
                sent += 1
    except Exception as exc:
        log.warning("Deadline reminder sending failed: %s", exc)
        return
    if sent:
        _save_reminders(reminders)
        log.info("Sent %s deadline reminder email(s)", sent)


def _delivery_marker_paths(meeting_id: str) -> tuple[str, str]:
    """Return shared-volume paths used to make meeting email delivery idempotent."""
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(meeting_id or "meeting"))[:180]
    notes_dir = os.path.abspath(os.environ.get("MECHUS_NOTES_DIR", "mechus_notes"))
    marker_dir = os.path.join(notes_dir, "email_delivery")
    return (
        os.path.join(marker_dir, f"{safe_id}.sent"),
        os.path.join(marker_dir, f"{safe_id}.sending"),
    )


def _acquire_delivery(meeting_id: str) -> tuple[bool, str, str]:
    """Atomically reserve one meeting's email delivery across worker/API processes."""
    sent_path, lock_path = _delivery_marker_paths(meeting_id)
    os.makedirs(os.path.dirname(sent_path), exist_ok=True)
    if os.path.exists(sent_path):
        return False, sent_path, lock_path

    # A killed worker can leave a reservation behind. The API stop path runs
    # after the worker grace period, so a 90-second lock is safe to reclaim.
    try:
        if os.path.exists(lock_path) and time.time() - os.path.getmtime(lock_path) > 90:
            os.unlink(lock_path)
    except OSError:
        pass

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, sent_path, lock_path
    try:
        os.write(fd, f"pid={os.getpid()} started={time.time():.3f}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return True, sent_path, lock_path


def _finish_delivery(sent_path: str, lock_path: str, *, sent_any: bool) -> None:
    """Commit the reservation after a send, or release it when nothing was sent."""
    try:
        if sent_any:
            os.replace(lock_path, sent_path)
        else:
            os.unlink(lock_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not update meeting email delivery marker: %s", exc)


def _smtp_password() -> str:
    """Gmail app passwords are often pasted with spaces — strip them."""
    return (os.environ.get("MECHUS_EMAIL_PASSWORD") or "").replace(" ", "").strip()


def _smtp_login_hint(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "535" in msg or "badcredentials" in msg or "username and password not accepted" in msg:
        return (
            "Gmail rejected the SMTP login — use a Google App Password "
            "(not your normal password): https://support.google.com/accounts/answer/185833"
        )
    return ""


def _lookup_emails_by_names(names: list[str]) -> dict[str, str]:
    """Query Supabase profiles table to find emails by display name."""
    url = os.environ.get("MECHUS_SUPABASE_URL", "").strip()
    key = os.environ.get("MECHUS_SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        log.debug("Supabase not configured — skipping email lookup")
        return {}

    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("profiles").select("name, display_name, email").execute()
        rows = result.data or []
        mapping: dict[str, list[str]] = {}
        for row in rows:
            email = (row.get("email") or "").strip()
            if not email:
                continue
            for field in ("display_name", "name"):
                val = (row.get(field) or "").strip().lower()
                if val:
                    if val not in mapping:
                        mapping[val] = []
                    if email not in mapping[val]:
                        mapping[val].append(email)
        # Match each requested name
        found: dict[str, list[str]] = {}
        for name in names:
            name_l = name.lower().strip()
            name_parts = name_l.split()
            exact_matches = []
            first_name_matches = []

            for stored_name, emails in mapping.items():
                stored_parts = stored_name.split()
                if stored_name == name_l:
                    exact_matches.extend(emails)
                elif stored_parts[0] == name_parts[0]:
                    first_name_matches.extend(emails)

            if exact_matches:
                found[name] = list(dict.fromkeys(exact_matches))
            elif first_name_matches:
                found[name] = [first_name_matches[0]]

        return found
    except Exception as exc:
        log.warning("Supabase email lookup failed: %s", exc)
        return {}


def _build_individual_email(owner: str, tasks: list[dict], *, disclaimer: bool = False) -> str:
    lines = [f"Hi {owner},", ""]
    if disclaimer:
        lines += [
            "Note: Two Mechus accounts are registered under this name. If these action items aren't yours, please ignore this email.",
            "",
        ]
    lines += ["Here are your action items from the meeting:", ""]
    for t in tasks:
        task = (t.get("task") or "").strip()
        deadline = t.get("deadline")
        line = f"  • {task}"
        if deadline:
            line += f" (by {deadline})"
        lines.append(line)
    lines += ["", "— Mechus"]
    return "\n".join(lines)


def _build_summary_email(summary: dict) -> str:
    lines = ["Meeting Summary", "=" * 40, ""]
    exec_summary = (summary.get("executive_summary") or "").strip()
    if exec_summary:
        lines += ["Summary:", exec_summary, ""]
    decisions = summary.get("decisions") or []
    if decisions:
        lines.append("Decisions:")
        for d in decisions:
            val = (d.get("value") or "").strip()
            if val:
                lines.append(f"  • {val}")
        lines.append("")
    action_items = summary.get("action_items") or []
    if action_items:
        lines.append("Action Items:")
        for a in action_items:
            owner = (a.get("owner") or "Someone").strip()
            task = (a.get("task") or "").strip()
            deadline = a.get("deadline")
            line = f"  • {owner} — {task}"
            if deadline:
                line += f" (by {deadline})"
            lines.append(line)
        lines.append("")
    open_questions = summary.get("open_questions") or []
    if open_questions:
        lines.append("Still Open:")
        for q in open_questions:
            lines.append(f"  • {q}")
        lines.append("")
    lines += ["— Mechus"]
    return "\n".join(lines)


def send_meeting_emails(
    summary: dict,
    meeting_id: str,
    speaker_emails: dict | None = None,
    summary_recipients: list[str] | None = None,
    collection_recipients: list[str] | None = None,
    invite_collection_link: str = "",
) -> bool:
    """
    Send action item emails after meeting ends.
    Priority: speaker_emails captured from Zoom display name
              → Supabase profiles lookup by name
              → fallback summary to MECHUS_EMAIL_RECIPIENTS
    """
    if os.environ.get("MECHUS_EMAIL_ENABLED", "").strip() != "1":
        log.info("Meeting email skipped (MECHUS_EMAIL_ENABLED is not 1)")
        return False

    smtp_host = os.environ.get("MECHUS_EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("MECHUS_EMAIL_SMTP_PORT", "587"))
    username = (os.environ.get("MECHUS_EMAIL_USERNAME") or "").strip()
    password = _smtp_password()
    from_addr = (os.environ.get("MECHUS_EMAIL_FROM") or username).strip() or username
    fallback_recipients = [
        r.strip() for r in
        os.environ.get("MECHUS_EMAIL_RECIPIENTS", "").split(",")
        if r.strip()
    ]
    if summary_recipients:
        for r in summary_recipients:
            rs = (r or "").strip()
            if rs and rs not in fallback_recipients:
                fallback_recipients.append(rs)

    if not username or not password:
        log.warning(
            "Meeting email not sent — set MECHUS_EMAIL_USERNAME and MECHUS_EMAIL_PASSWORD "
            "(Gmail requires an App Password when 2FA is on)"
        )
        return False

    acquired, sent_path, lock_path = _acquire_delivery(meeting_id)
    if not acquired:
        log.info("Meeting email already sent or in progress for meeting_id=%s; skipping", meeting_id)
        return False

    sent_count = 0

    action_items = summary.get("action_items") or []

    # Collect all unique owner names
    owner_names = list({
        (ai.get("owner") or "").strip()
        for ai in action_items
        if (ai.get("owner") or "").strip().lower() not in ("", "someone", "unknown")
    })

    # Build email map: name → email
    email_map: dict[str, str] = {}
    if speaker_emails:
        email_map.update(speaker_emails)

    # Fill gaps from Supabase
    missing = [n for n in owner_names if n not in email_map]
    if missing:
        email_map.update(_lookup_emails_by_names(missing))

    # Persist deadline reminders only after owner addresses have been resolved.
    # The queue is idempotent, so retries cannot duplicate a reminder.
    _enqueue_deadline_reminders(summary, meeting_id, email_map)

    # Group tasks by owner
    owner_tasks: dict[str, list[dict]] = {}
    for ai in action_items:
        owner = (ai.get("owner") or "").strip()
        if owner:
            owner_tasks.setdefault(owner, []).append(ai)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(username, password)

            # Individual emails
            for owner, tasks in owner_tasks.items():
                owner_emails = email_map.get(owner)
                if not owner_emails:
                    log.debug("No email for %s — skipping", owner)
                    continue
                if isinstance(owner_emails, str):
                    owner_emails = [owner_emails]
                disclaimer = len(owner_emails) > 1
                body = _build_individual_email(owner, tasks, disclaimer=disclaimer)
                for owner_email in owner_emails:
                    msg = MIMEMultipart()
                    msg["From"] = from_addr
                    msg["To"] = owner_email
                    msg["Subject"] = "Your action items from the meeting — Mechus"
                    msg.attach(MIMEText(body, "plain"))
                    server.sendmail(from_addr, owner_email, msg.as_string())
                    sent_count += 1
                    log.info("Sent action items to %s (%s)", owner, owner_email)

            # Summary email to opt-in recipients
            if fallback_recipients:
                body = _build_summary_email(summary)
                msg = MIMEMultipart()
                msg["From"] = from_addr
                msg["To"] = ", ".join(fallback_recipients)
                msg["Subject"] = "Meeting summary — Mechus"
                msg.attach(MIMEText(body, "plain"))
                server.sendmail(from_addr, fallback_recipients, msg.as_string())
                sent_count += 1
                log.info("Sent summary to %s", fallback_recipients)

            link = (invite_collection_link or "").strip()
            if link and collection_recipients:
                invite_body = (
                    f"Mechus meeting {meeting_id} ended.\n\n"
                    f"If you would like to receive future summaries, add your contact here:\n{link}\n"
                )
                for dest in collection_recipients:
                    if not dest or "@" not in dest:
                        continue
                    msg2 = MIMEMultipart()
                    msg2["From"] = from_addr
                    msg2["To"] = dest
                    msg2["Subject"] = "Mechus — share your email (optional)"
                    msg2.attach(MIMEText(invite_body, "plain"))
                    server.sendmail(from_addr, dest, msg2.as_string())
                    sent_count += 1
                    log.info("Sent collection invite to %s", dest)

    except Exception as exc:
        hint = _smtp_login_hint(exc)
        if hint:
            log.warning("Meeting email failed: %s — %s", exc, hint)
        else:
            log.warning("Meeting email failed: %s", exc)
        _finish_delivery(sent_path, lock_path, sent_any=sent_count > 0)
        return sent_count > 0

    _finish_delivery(sent_path, lock_path, sent_any=sent_count > 0)
    return sent_count > 0
