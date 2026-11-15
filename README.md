# Mechus

**Your AI property inspector. Just answer the call.**

Mechus is an AI-powered property inspector you can FaceTime. A landlord starts an inspection from a web dashboard, Mechus calls the tenant over FaceTime Video, guides them through the unit room by room, spots potential damage through their camera, and produces a printable report with photographic evidence.

Tenants never install an app or create an account. They answer a call and follow the spoken instructions.

---

## Inspiration

Move-in and move-out inspections are where landlords and tenants often stop trusting each other. One side brings a clipboard, the other brings a handful of blurry photos, and months later a security deposit can come down to whose memory wins.

We wanted an inspection process neither side has to take on faith: one that evaluates the actual condition of a property, captures evidence in real time, and creates a transparent record both parties can access. And since almost everyone with an iPhone already knows how to answer a FaceTime call, we made that the interface.

---

## What It Does

### Live AI walkthroughs
A tenant answers a video call and walks through their unit. Mechus continuously analyzes the camera feed, identifies potential damage, and speaks when it notices something worth a closer look:

> "Can you show me that wall again?"
> "Could you move the camera a little closer?"
> "Okay, everything looks good there."

### Guided property tours
Mechus guides the tenant through one room and one inspection item at a time. The default route is **Living room → Kitchen → Bathroom → Bedroom**, customizable with `MECHUS_INSPECT_TOUR`.

- A stop **passes** after two clear looks.
- A stop is **flagged** when damage is detected.
- A stop is marked **not shown** after one reminder.
- Mechus ends with a spoken summary.

### Damage detection and evidence
- Camera frames are analyzed by Gemini Flash-Lite, which returns findings with bounding boxes.
- Findings are consolidated across frames using Jaccard similarity on word sets.
- Each finding is tied to its inspection stop.
- Mechus captures one photo per finding (damage outlined in a red box) and one photo per passing stop.

### Move-in vs. move-out comparison
During move-in, Mechus saves frames and establishes a baseline of the property's condition. During move-out, the live feed is compared against that baseline so Mechus focuses on what changed instead of flagging every imperfection.

### Printable inspection reports
Each completed inspection produces a report with:
- An overall verdict: **Passed**, **Needs review**, or **Incomplete**
- Findings with photographs
- A room-by-room inspection record
- A notice that AI-generated findings require human review

### Landlord dashboard
| Section | Purpose |
|---|---|
| Tenants | Add tenants and start move-in or move-out inspections |
| Reports | View completed inspection reports and results |
| Overview | Property dashboard overview |
| Properties | Manage property information |
| Meetings | Start FaceTime calls by phone number or Apple ID email |

### FaceTime calling
Calls tenants through Chert's FaceTime bridge using a phone number or Apple ID email, and surfaces readable failures: unverified recipient, "Someone else is using the line," and no answer.

### Landlord notifications and shared history
When Chert messaging is configured, Mechus sends the inspection summary to the landlord over iMessage. Otherwise the summary is logged on the property's timeline. The iMessage integration also supports a shared timeline of repairs, photos, and agreements so both parties see the same record.

### Voice interaction
Mechus responds to the wake word "Mechus" on ordinary assistant calls. During inspection calls it does **not** listen to the tenant: it watches the camera and speaks its instructions.

---

## How It Works

Mechus combines a Flask dashboard, a LiveKit video pipeline, a FaceTime bridge, and Gemini vision analysis.

1. **Inspection initiated.** The landlord starts an inspection from the dashboard. Flask launches a dedicated worker process for the call.
2. **LiveKit session.** The worker joins a LiveKit room, publishing its audio track and a video tile with a status card showing the current inspection stop.
3. **FaceTime connection.** Chert's FaceTime bridge joins the same room and calls the tenant. The tenant's camera and audio arrive as ordinary LiveKit tracks.
4. **Frame analysis.** Every three seconds, a camera frame goes to Gemini Flash-Lite, which returns structured JSON: the visible area, whether the current stop is visible, potential damage with bounding boxes, and a suggested spoken response.
5. **Spoken guidance.** Mechus speaks to the tenant over its published audio track.
6. **Storage.** Inspection photos, move-in baselines, histories, and findings are persisted in **MongoDB**, with a local-file fallback.
7. **Report generation.** When the call ends, results are compiled into a report and the landlord is notified over iMessage if messaging is configured.

---

## How We Built It

**Real-time video.** Mechus joins a LiveKit room as an independent participant. Each call gets its own Python worker, so inspections run independently.

**Vision.** We chose Flash-Lite for low latency (about one second per frame in our testing), which suits an interactive walkthrough.

**Speech.** Text-to-speech via ElevenLabs, with OpenAI powering speech-related functionality.

**Storage.** Inspection photos and move-in baselines are stored in MongoDB, which lets Mechus retrieve historical inspections and compare them against later walkthroughs. A local-file fallback is used when MongoDB is unavailable.

**Reporting.** The dashboard can process uploaded walkthrough videos with FFmpeg to extract representative frames, then use vision models to identify rooms, assess condition, and compare move-out footage to the baseline.

**Backend.** Python and Flask. The API server manages inspection sessions, launches worker processes, coordinates the live agent, and serves the dashboard.

### Keeping Mechus from chattering
At one frame every three seconds, a naive agent would announce the same observation over and over. We use word stemming plus Jaccard similarity,

```
J(A, B) = |A ∩ B| / |A ∪ B|
```

and treat any observation scoring above 0.5 against a prior one as a repeat. The same check detects when the camera has moved to a new area or the agent is merely rephrasing a question. Mechus also signs off on an area only after several consecutive damage-free frames of it.

Our philosophy: **let the model decide what could be said, but let code decide whether it should be said.** Code enforces that structural findings take priority, there are at most two questions per location, repeats are suppressed, answered questions aren't re-asked, and quiet periods are preserved.

### Challenges we ran into
- **One `await` broke every video call.** In the LiveKit Python SDK, publishing an audio frame is synchronous while video publishing is asynchronous. A misplaced `await` crashed calls on connect, and our mocks passed anyway. Only a real video stream exposed it.
- **API rate limits.** At 20 requests per minute we hit limits around the 45-second mark, so we added exponential backoff for HTTP 429 responses.
- **Teaching speech recognition a made-up name.** "Mechus" was heard as "mech is." We biased the recognizer, whitelisted common mis-transcriptions, rejoined split forms, and guarded against words like "mechanic" triggering the agent.
- **Two branches, one product.** The live agent and dashboard were built in parallel; the dashboard's FaceTime button initially called into an empty room until we fixed the routing to connect to the right worker.

---

## Tech Stack

| Technology | Purpose |
|---|---|
| Python | Core application and inspection logic |
| Flask | Web dashboard and API server |
| LiveKit | Real-time audio and video transport |
| Chert | FaceTime calling and iMessage bridge |
| Google Gemini Flash-Lite | Camera-frame analysis and damage detection |
| ElevenLabs | Text-to-speech |
| OpenAI | Speech-related API functionality |
| MongoDB | Inspection photo, baseline, and history storage (local-file fallback) |
| FFmpeg | Frame extraction from uploaded walkthrough videos |
| JavaScript, HTML, CSS | Dashboard interface |
| Docker | Containerization |
| Vercel | Static deployment |

---

## Getting Started

### Prerequisites
- Python 3.11
- A MongoDB instance (optional; Mechus falls back to local files)
- A configured LiveKit instance
- Access to Chert for FaceTime calling
- API credentials for the services you intend to use

### Installation
```bash
git clone https://github.com/Odey340/Mechus.git
cd Mechus
pip install -r requirements.txt
cp .env.example .env
```
Fill in the required keys in `.env`. Never commit this file.

### Run
```bash
python scripts/run.py
```
The API listens on port 5000 by default (set `API_PORT` to change it). Open <http://127.0.0.1:5000> and go to the **Tenants** tab to set up an inspection.

---

## Configuration

All behavior is driven by environment variables, so you can deploy and scale without code changes.

| Variable | Description |
|---|---|
| `CHERT_API_KEY` | Chert API authentication key |
| `CHERT_PROJECT_ID` | Chert project ID |
| `CHERT_LINE_ID` | Chert FaceTime line ID |
| `CHERT_PHONE_NUMBER` | Chert phone configuration |
| `MECHUS_CHERT_SIGNING_SECRET` | Chert signing secret |
| `MECHUS_CHERT_TENANT` | Chert tenant configuration for iMessage |
| `LIVEKIT_URL` | LiveKit server URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `GEMINI_API_KEY` | Google Gemini API key |
| `MECHUS_TTS_BACKEND` | Set to `elevenlabs` |
| `MECHUS_ELEVEN_API_KEY` | ElevenLabs API key |
| `MECHUS_ELEVEN_VOICE_ID` | ElevenLabs voice ID |
| `MECHUS_LLM_BACKEND` | Set to `openai` |
| `MECHUS_OPENAI_API_KEY` | OpenAI API key |
| `MONGODB_URI` | MongoDB connection string for photo and baseline storage *(confirm name against `.env.example`)* |
| `MECHUS_INSPECT_TOUR` | Optional custom inspection route |
| `MECHUS_INSPECT_INTERVAL_S` | Optional frame-analysis interval |
| `MECHUS_PUBLIC_BASE_URL` | Public base URL used for report links |
| `ZOOM_APP_CLIENT_ID`, `ZOOM_APP_CLIENT_SECRET` | Optional Zoom SDK join |

Some settings keep the historical `mechus` / `MECHUS_` prefix. Optional settings can be left unset to use defaults.

---

## Project Structure

```
Mechus/
├── mechus/
│   ├── api_server.py        # Flask API server and dashboard backend
│   ├── chert_worker.py      # Per-call FaceTime worker
│   ├── chert_livekit.py     # LiveKit audio and video publishing
│   ├── inspection.py        # Frame analysis, guided tours, photo capture
│   ├── estates/checks.py    # Inspection report page
│   ├── static/properties/   # Dashboard static assets
│   └── brand.py             # Assistant branding and wake word
├── scripts/run.py           # Application entry point
├── tests/                   # Pytest test suite
├── .env.example
├── requirements.txt
└── README.md
```

---

## Testing

```bash
python -m pytest tests
```
The suite has 661 passing tests on Windows. One virtual-camera test is Linux-specific and fails in that environment; this is an environment limitation, not a broader failure.

---

## Limitations

- **Shared FaceTime line:** a shared Chert line handles one call at a time.
- **No tenant listening during inspections:** Mechus watches the camera and speaks guidance only.
- **AI accuracy:** findings are estimates and can be wrong or incomplete. Landlords must review results before any decision about property condition or tenant responsibility.
- **Limited real-world testing:** the guided tour has had limited testing on real calls.
- **Public report links:** report links are publicly accessible and should not be treated as private.
- **iMessage:** requires Chert messaging; without it, summaries are logged on the property timeline.

---

## What's Next

- **Automated wrap-up:** feed live-call findings directly into the final report and notify landlords (e.g. "Tenant Joey completed his check-out inspection!").
- **Guided checklists:** dynamic room-by-room checklists that track what has been inspected and prompt tenants to show anything missed.
- **Human-in-the-loop safeguards:** AI findings should never independently set financial penalties. We plan mandatory human review before landlords finalize charges or deposit deductions, with tenants able to acknowledge or dispute individual findings.

---

## What We Learned

- **Test against real hardware early.** Mocks can hide async bugs that only real video streams reveal.
- **Timing is a feature.** Knowing when to stay silent matters as much as what to say.
- **Separate intelligence from execution.** The model proposes; deterministic code decides.
- **Small, fast models matter** when someone is standing in a room waiting for an answer.
- **Integration is its own engineering challenge.**

---

## Team

- Odey
- Rajiv
- Goutham

*No clipboard. No arguing over blurry photos. Just answer the call.*
