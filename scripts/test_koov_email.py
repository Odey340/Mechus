#!/usr/bin/env python3
"""
Test Koov meeting email: validate config and templates, optionally send a real test message.

Usage (from repo root):
  python3 scripts/test_koov_email.py              # dry-run: no SMTP
  python3 scripts/test_koov_email.py --send       # send test mail (requires KOOV_EMAIL_ENABLED=1 + SMTP)

Set in .env (or the environment) at minimum for --send:
  KOOV_EMAIL_ENABLED=1
  KOOV_EMAIL_USERNAME=...
  KOOV_EMAIL_PASSWORD=...
  KOOV_EMAIL_RECIPIENTS=you@example.com

Optional: KOOV_EMAIL_TEST_TO=you@example.com  (overrides per-owner test address)
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _mask(s: str) -> str:
    if not s or len(s) < 3:
        return "(empty)"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--send",
        action="store_true",
        help="Connect to SMTP and send a short test (uses .env / KOOV_EMAIL_*)",
    )
    args = p.parse_args()

    from koov.envfile import load_env_file

    env_path = os.path.join(REPO_ROOT, os.environ.get("KOOV_ENV_FILE", ".env"))
    load_env_file(env_path)
    if os.path.isfile(env_path):
        print(f"Loaded: {env_path}\n")
    else:
        print(f"No {env_path} (using existing environment only)\n")

    from koov import email_sender as es

    enabled = os.environ.get("KOOV_EMAIL_ENABLED", "").strip() == "1"
    user = (os.environ.get("KOOV_EMAIL_USERNAME") or "").strip()
    password = (os.environ.get("KOOV_EMAIL_PASSWORD") or "").strip()
    from_addr = (os.environ.get("KOOV_EMAIL_FROM") or user).strip() or user
    recipients = [r.strip() for r in (os.environ.get("KOOV_EMAIL_RECIPIENTS") or "").split(",") if r.strip()]

    print("--- Config ---")
    print(f"KOOV_EMAIL_ENABLED:     {enabled}")
    print(f"KOOV_EMAIL_SMTP:        {os.environ.get('KOOV_EMAIL_SMTP_HOST', 'smtp.gmail.com')}:{os.environ.get('KOOV_EMAIL_SMTP_PORT', '587')}")
    print(f"KOOV_EMAIL_USERNAME:    {_mask(user) if user else '(missing)'}")
    print(f"KOOV_EMAIL_PASSWORD:    {'(set)' if password else '(missing)'}")
    print(f"KOOV_EMAIL_FROM:        {from_addr or '(default to username)'}")
    print(f"KOOV_EMAIL_RECIPIENTS:  {recipients or '(empty)'}")
    supa = (os.environ.get("KOOV_SUPABASE_URL") or "").strip()
    print(f"KOOV_SUPABASE_URL:      {'(set)' if supa else '(not set; name lookup skipped)'}")
    print()

    sample = {
        "executive_summary": "Dry-run only: this text exercises the meeting summary body.",
        "action_items": [
            {"owner": "Alex", "task": "Confirm Koov email delivery works in your environment", "deadline": None},
        ],
        "decisions": [{"value": "We will use email for post-meeting follow-ups."}],
        "open_questions": ["Is the SMTP relay allowlisted?"],
    }

    print("--- Sample summary email body ---")
    print(es._build_summary_email(sample))
    print("--- End sample summary ---\n")

    print("--- Sample per-owner email body ---")
    print(
        es._build_individual_email(
            "Alex",
            [{"task": "Confirm Koov email delivery works", "deadline": "EOW"}],
            disclaimer=False,
        )
    )
    print("--- End sample per-owner ---\n")

    if not args.send:
        print("Dry-run only (no email sent). Run with --send to deliver a test message.")
        return 0

    if not enabled:
        print("Refusing --send: set KOOV_EMAIL_ENABLED=1 in .env", file=sys.stderr)
        return 1
    if not user or not password:
        print("Refusing --send: set KOOV_EMAIL_USERNAME and KOOV_EMAIL_PASSWORD", file=sys.stderr)
        return 1
    if not recipients:
        print("Refusing --send: set at least one address in KOOV_EMAIL_RECIPIENTS", file=sys.stderr)
        return 1

    test_to = (os.environ.get("KOOV_EMAIL_TEST_TO") or recipients[0]).strip()
    test_summary = {
        "executive_summary": "This is a Koov automated test email. You can delete it.",
        "action_items": [
            {
                "owner": "Test Owner",
                "task": "If you see this, per-owner action-item email works.",
                "deadline": None,
            },
        ],
        "decisions": [],
        "open_questions": [],
    }
    print(f"Sending test via send_meeting_emails (summary + per-owner) → check {test_to!r} …")
    try:
        es.send_meeting_emails(
            test_summary,
            meeting_id="koov-email-selftest",
            speaker_emails={"Test Owner": test_to},
            summary_recipients=None,
            collection_recipients=[],
            invite_collection_link="",
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("Done. Check the inbox (and spam) for subject lines containing 'Koov'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
