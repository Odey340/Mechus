# Getting Mechus demo-ready

Three things that make a live demo hold up: the report reaching the landlord, proof on
real footage before you dial, and a Mechus that responds when the tenant speaks.

## 1. Get the report to the landlord

When a check ends, Mechus sends the landlord a one-line result and the report. The
landlord contact on a tenant decides the channel:

| Landlord contact | Channel | Needs |
|---|---|---|
| An email address | Email with the PDF attached | `MECHUS_EMAIL_ENABLED=1`, `MECHUS_EMAIL_USERNAME`, `MECHUS_EMAIL_PASSWORD` (Gmail: an app password), optional `MECHUS_EMAIL_FROM`, `MECHUS_EMAIL_SMTP_HOST`, `MECHUS_EMAIL_SMTP_PORT` |
| A phone number | iMessage through Chert, with a link to the report | `MECHUS_CHERT_SIGNING_SECRET`, `MECHUS_CHERT_TENANT`, `MECHUS_CHERT_LINE_PHONE`, and `MECHUS_PUBLIC_BASE_URL` so the link opens outside your machine |

The iMessage settings come from the Chert Messaging dashboard (a signing secret and your
tenant slug); they are separate from the FaceTime project key. Check them in the
dashboard under Settings -> Test send.

The Reports tab has a **Send to landlord** button per report for sending it again. If the
channel is not set up, the button says what is missing.

## 2. Rehearse without a phone call

```bash
python scripts/rehearse_inspection.py walkthrough.mp4
python scripts/rehearse_inspection.py photos/ --tour "kitchen: counters, floor"
python scripts/rehearse_inspection.py clip.mp4 --say "8=skip" --say "20=that's it"
```

This runs a video (or a folder of photos) through the same inspector a call uses, with
the real vision model, prints everything Mechus would say, and writes the report PDF. Use
footage of a room with real, visible damage: it is the quickest way to see whether Mechus
flags it and what the photos in the report look like. It uses your vision API quota.

Then do two or three real calls and keep a screen recording of the best one as a backup.

## 3. The tenant can talk

During a tour the tenant can say:

| They say | Mechus does |
|---|---|
| "skip", "we don't have one", "there's no tub", "move on" | Skips the current stop (reported as "Skipped by tenant") |
| "repeat", "what did you say", "what's next" | Repeats what to show |
| "that's it", "I'm done", "end the tour" | Ends the tour and gives the summary |

Anything else is ignored; Mechus does not hold a conversation on inspection calls. Audio
is ignored while Mechus itself is speaking. Set `MECHUS_INSPECT_LISTEN=0` to turn this
off, or raise `MECHUS_INSPECT_LISTEN_RMS` (default 550) in a noisy room.
