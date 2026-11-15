# Koov Properties

Walkthrough video in, evidence-grade condition report out. Koov also sits in each property's iMessage group (via Chert) and keeps the log.

Open **`/properties`** on the API server (e.g. `http://127.0.0.1:5050/properties`). Add `?demo` for a fully populated sample (client-side only, no writes).

## What works today
| Area | What happens |
|---|---|
| Properties | Create/edit (landlord, tenant + language, contractors). |
| Inspections | Drag-drop videos/photos/voice memos (per-file progress) → Process → live stage stepper. |
| Pipeline (`koov/estates/pipeline.py`) | ffmpeg keyframes (sharpest per window, pHash dedupe) → **ElevenLabs Scribe** narration transcript → room labeling → move-in inventory **or** move-out comparison against the latest move-in → cost estimates → marked-up evidence → summary + **ElevenLabs** voice. Partial results survive failures. |
| Report | Totals, audio player (re-voice in 8 languages), before/after slider per finding, category/severity/cost/video timestamp, Confirm / Dismiss / Edit cost. Public link `/r/<token>` is print-ready with tenant Acknowledge/Dispute and a SHA-256 evidence trail. |
| Messages | iMessage-style thread for the property's Chert group: send text/photo, report rich link, ElevenLabs voice note (1:1), celebration effect (1:1), tapbacks on any message, edits (1:1 only), typing dots, FaceTime. |
| Inbound (webhook) | Messages/photos/voice notes are filed on the timeline and classified (repair, evidence, agreement, payment…). Koov tapbacks 👍 logged, ‼️ urgent (and texts the landlord), ❓ unclear; photos go into an open draft inspection; "Koov, what did we agree…?" is answered from the log. |
| Timeline | Everything logged, filterable; repairs can be marked fixed. |
| Settings | Chert/AI/ElevenLabs/FaceTime readiness, webhook URL to paste into Chert, test send. |

| Outbox (`koov/estates/outbox.py`) | Every Chert send is saved to disk first, tried immediately, then retried with backoff (3s → 15m) on network/5xx/429 errors using the same idempotency key, so nothing is lost or sent twice. Per-chat order is kept. After sending, Koov polls the message for delivered/read receipts. Messages show Sending / Retrying / Sent / Delivered / Read / Not delivered + Retry. Pending sends resume after a restart. Settings → Outbox lists everything with Retry/Cancel; failed sends appear under Needs attention. |
| Model reports (`koov/estates/examples.py`) | Model findings and a style guide (item, size, landmark, one-sentence reason) are given to the vision model as few-shot examples so every report reads the same way. |
| Sample property (`koov/estates/sample.py`) | "Load sample property" builds a finished move-in + move-out pair with rendered room photos and hand-checked findings (hole, burn, oven cleaning, missing blinds, carpet wear, pre-existing tile). No phone numbers, so nothing can be texted. |
| Past reports | Reports page: search, filters (move-out, move-in, needs review, disputed), copy share link, and **recurring issues** (the same room + item flagged in more than one report). Reports can be re-run or deleted; properties can be deleted from Details. |

AI runs **live** with `KOOV_OPENAI_API_KEY` (vision model `KOOV_ESTATES_VISION_MODEL`, default `gpt-4o`) or **offline** (`KOOV_ESTATES_FAKE_AI=1`) with deterministic results so the UI always works. Wear and tear and pre-existing issues are never chargeable (`koov/estates/costs.py`, prices in `prices.json`).

## Connect Chert
1. Fill the Chert block in `.env` (see `.env.example`): signing secret, tenant slug, your Chert number (`KOOV_CHERT_LINE_PHONE`), line id.
2. Expose the server publicly (tunnel) and set `KOOV_PUBLIC_BASE_URL`.
3. In Chert → Webhook subscriptions, add `<public url>/webhooks/chert` for `message.received` and `reaction.added`; put the subscription secret in `KOOV_CHERT_WEBHOOK_SECRET`.
4. Settings → Test send. Then on a property: Messages → Create iMessage group.

Chert constraints the UI respects: edits only in 1:1 chats within 15 minutes; voice notes and send effects go 1:1 via `/send`; groups take text, media and rich links. FaceTime needs the Chert FaceTime project key + a LiveKit room with the Koov agent publishing audio; until that agent runs, the FaceTime button reports what's missing.

## Code map
- `koov/chert/client.py` – signed Messaging API client · `webhooks.py` – signature verify, dedupe, parsing · `facetime.py` – outbound calls + LiveKit tokens
- `koov/estates/` – `store.py` (JSON files), `media.py`, `scribe.py`, `ai.py`, `costs.py`, `report.py`, `voice.py`, `pipeline.py`, `messaging.py`, `routes.py`
- `koov/static/properties.html` (dashboard) · `koov/static/report.html` (public report)
- Tests: `tests/test_chert.py`, `tests/test_estates.py`

## Production next steps
- Postgres + S3 (signed URLs) + Redis job queue instead of JSON files and in-process threads; resumable chunked uploads.
- LiveKit agent for live FaceTime inspections (Chert BYOR `call.incoming` → room → agent sees `facetime-remote-video`, speaks with ElevenLabs).
- Job verification flow: repairs become work orders; contractor after-photos are compared with the before evidence.
- Warehouse/land survey prompts (hazards, racking, drainage, fencing).
- Eval set of paired walkthroughs with labeled findings as a CI gate; face/document blurring in shared evidence; jurisdiction-specific wear-and-tear rules; roles and audit log.
