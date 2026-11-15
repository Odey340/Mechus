## MongoDB Atlas & Data Architecture

MongoDB Atlas is the coordination and memory layer behind Mechus. It does more than store inspection results: it manages live-call baselines, report revisions, exactly-once event processing, and idempotent onboarding across concurrent application workers.

Mechus uses four key data workflows:

### 1. Live-Call Memory

Move-in inspection frames become the visual baseline used to evaluate move-out inspections in real time.

- Move-in camera frames are stored as JPEGs alongside scene information and inspection findings.
- During move-out, Mechus retrieves baseline frames matching the property unit and move-in inspection.
- Baseline frames are sorted by time, with an indexed lookup on `(unit, mode, at)`.
- Up to five representative frames are selected, prioritizing distinct rooms and previously identified damage.
- Gemini Flash-Lite compares the live camera frame against the stored baseline.
- Only newly identified damage is spoken aloud and saved as a finding.

This allows Mechus to focus on changes in property condition rather than repeatedly flagging existing imperfections.

**Data design:** Compound indexes, time-ordered reads, and inline JPEG baseline storage.

### 2. Conflict-Safe Report Lifecycle

Inspection reports support concurrent updates from inspection stages, landlord reviews, and tenant responses without silently overwriting one another.

- When an inspection stage finishes or a landlord reviews a report, Mechus reads the current inspection and its revision token.
- Before saving, the application checks whether the revision has changed.
- Updates are only applied when the revision remains unchanged.
- If another server has written first, the application retries against the latest revision.
- Completed inspections are stored in the `inspections` collection.
- Tenant report access uses a unique `share_token` index, allowing reports to be accessed through `/r/<token>`.

Reports retain their evidence and support tenant responses through the same inspection record.

**Data design:** Compare-and-swap updates, revision tokens, and a unique index on `share_token`.

### 3. Exactly-Once iMessage Processing

Mechus uses MongoDB Atlas to coordinate inbound webhooks and outbound iMessage delivery across application servers.

**Inbound events:**
- Chert webhooks are deduplicated using `insert_id = event_id`.
- Duplicate events are acknowledged and skipped.
- If Atlas is unavailable, Mechus returns HTTP 503 so Chert can redeliver the event.
- First-time events are recorded in `property_events`, using property IDs and sequence numbers (`_pid`, `_seq`).

**Outbound messages:**
- Messages enter an `outbox` collection with pending and retrying states.
- Workers process due messages, prioritizing the oldest first.
- Each send uses an idempotency key to prevent duplicate delivery.
- Successful sends are marked delivered.
- Network errors and HTTP 5xx responses trigger exponential-style backoff, increasing from 3 seconds to 15 minutes.

**Data design:** Unique `_id` locks, 30-day TTL retention, and an index on `(status, created_at)`.

### 4. Idempotent Onboarding

Mechus protects property creation from duplicate submissions, retries, and multiple servers processing the same request.

- Each onboarding submission includes an idempotency key.
- An atomic claim records `insert_id = key`.
- Repeated submissions with identical answers replay the original result.
- Reusing a key with different answers returns HTTP 409, prompting a reload.
- New requests create or retrieve a workspace using a unique email constraint.
- The first property is created and the onboarding request is marked complete.
- Failed operations release their claims to allow safe retries.

**Data design:** Atomic claims, unique email constraints, and seven-day TTL retention for onboarding requests.

### Voice Interaction & ElevenLabs

Mechus uses ElevenLabs for natural-sounding voice generation, allowing the AI inspector to communicate with tenants during live FaceTime walkthroughs.

- **Text-to-Speech:** ElevenLabs converts AI-generated instructions and responses into spoken audio.
- **LiveKit Audio:** Generated speech is published through Mechus's LiveKit audio track and delivered to the tenant during the call.
- **Contextual Guidance:** Mechus generates spoken prompts based on camera analysis and the current inspection stage.
- **Wake Word:** Ordinary assistant calls respond to the wake word "Mechus." During property inspections, Mechus focuses on camera analysis and spoken guidance rather than listening to the tenant.

### Data Layer Guarantees

| Mechanism | Purpose |
|---|---|
| Compound indexes | Efficient inspection and baseline lookups |
| Revision tokens | Prevent conflicting report updates |
| Unique indexes | Enforce deduplication and uniqueness |
| Idempotency keys | Make retries safe |
| Atomic claims | Coordinate concurrent workers |
| TTL indexes | Automatically expire temporary records |
| Outbox pattern | Reliably process outbound messages |
| Local-file fallback | Preserve the same storage interface when MongoDB is unavailable |

Every lookup is indexed, every write is conflict-checked, and temporary records have defined expiration policies.

MongoDB Atlas provides the shared memory and coordination layer, while a local-file fallback maintains the same application interface.
