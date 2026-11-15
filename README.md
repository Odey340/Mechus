# Mechus

**Your AI property inspector. Just answer the call.**

Mechus is an AI-powered property inspector you can FaceTime. A landlord starts an inspection from a web dashboard, Mechus calls the tenant over FaceTime Video, guides them through the unit room by room, identifies potential damage through their camera, and produces a printable report with photographic evidence.

Tenants never install an app or create an account. They simply answer a call and follow the spoken instructions.

---

## What It Is

Mechus makes property inspections easier, more transparent, and more consistent for landlords and tenants.

Instead of relying on clipboards, manual walkthroughs, and scattered photos, Mechus uses real-time video analysis and AI-guided inspections to document property conditions.

The platform supports both move-in and move-out inspections, allowing landlords to establish an initial property baseline and compare it against future inspections.

## How It Works

Mechus combines a Flask dashboard, a LiveKit video pipeline, a FaceTime bridge, and Gemini vision analysis.

1. **Inspection Initiated:** A landlord starts a move-in or move-out inspection from the web dashboard.
2. **LiveKit Session:** A dedicated Python worker joins a LiveKit room and publishes audio and a video status card displaying the current inspection stop.
3. **FaceTime Connection:** Chert's FaceTime bridge connects to the same room and calls the tenant using their phone number or Apple ID email.
4. **Frame Analysis:** Every three seconds, a camera frame is sent to Gemini Flash-Lite for structured visual analysis.
5. **Spoken Guidance:** Mechus provides real-time spoken instructions, guiding tenants through each inspection area.
6. **Evidence Storage:** Inspection photos, findings, move-in baselines, and historical records are stored in MongoDB, with a local-file fallback.
7. **Report Generation:** Once the inspection ends, results are compiled into a printable report. The landlord receives an iMessage summary when messaging is configured.

## Features

### Live AI Walkthroughs

- FaceTime-based inspections without requiring tenants to install an application.
- Real-time camera analysis using Google Gemini Flash-Lite.
- Spoken instructions and contextual follow-up questions.
- Independent Python worker processes for individual inspection sessions.

### Guided Property Tours

Mechus guides tenants through one room and one inspection item at a time.

The default inspection route is:

`Living Room → Kitchen → Bathroom → Bedroom`

The route can be customized using `MECHUS_INSPECT_TOUR`.

- An inspection stop passes after two clear looks.
- Potential damage triggers a flagged finding.
- An inspection stop is marked as not shown after one reminder.
- Mechus provides a spoken summary when the walkthrough is complete.

### AI Damage Detection

- Camera frames are analyzed using Gemini Flash-Lite.
- Potential damage is returned with bounding boxes.
- Findings are consolidated across frames using Jaccard similarity.
- Each finding is associated with its corresponding inspection stop.
- Photographic evidence is captured for flagged findings and passing inspection stops.

### Move-In and Move-Out Comparisons

Move-in inspections establish a visual baseline of the property's condition.

During move-out inspections, Mechus compares the live camera feed against the saved baseline to identify potential changes rather than flagging every existing imperfection.

### Printable Inspection Reports

Each completed inspection generates a report containing:

- Overall inspection verdict: Passed, Needs Review, or Incomplete.
- Photographic evidence of potential findings.
- Room-by-room inspection records.
- A notice that AI-generated findings require human review.

### Landlord Dashboard

The web dashboard provides several management sections:

| Section | Purpose |
|---|---|
| Tenants | Add tenants and initiate inspections |
| Reports | View completed inspection reports |
| Overview | View the property dashboard |
| Properties | Manage property information |
| Meetings | Start FaceTime calls using phone numbers or Apple ID emails |

### FaceTime Integration

Mechus uses Chert's FaceTime bridge to initiate calls through a phone number or Apple ID email.

The integration also surfaces readable call failures, including unverified recipients, unavailable lines, and unanswered calls.

### Landlord Notifications and Shared History

When Chert messaging is configured, Mechus sends inspection summaries to landlords over iMessage.

The integration also supports a shared property timeline containing repairs, photographs, and agreements.

Without messaging configuration, inspection summaries are logged on the property's timeline.

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
| MongoDB | Inspection photo, baseline, and history storage |
| FFmpeg | Frame extraction from uploaded walkthrough videos |
| JavaScript, HTML, CSS | Dashboard interface |
| Docker | Containerization |
| Vercel | Static deployment |

## Getting Started

### Prerequisites

- Python 3.11
- MongoDB instance (optional; local-file fallback available)
- Configured LiveKit instance
- Access to Chert for FaceTime calling
- API credentials for the services you intend to use

### Installation

Clone the repository:

```bash
git clone https://github.com/Odey340/Mechus.git
cd Mechus
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your environment configuration:

```bash
cp .env.example .env
```

Add the required API keys and service credentials to your `.env` file.

**Never commit your `.env` file or expose API credentials.**

### Running Mechus

Start the application:

```bash
python scripts/run.py
```

The API listens on port `5000` by default.

Open the dashboard:

```text
http://127.0.0.1:5000
```

Navigate to the **Tenants** tab to configure and initiate an inspection.

## Configuration

Mechus uses environment variables to configure its integrations and inspection behavior.

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
| `MECHUS_TTS_BACKEND` | Text-to-speech backend (`elevenlabs`) |
| `MECHUS_ELEVEN_API_KEY` | ElevenLabs API key |
| `MECHUS_ELEVEN_VOICE_ID` | ElevenLabs voice ID |
| `MECHUS_LLM_BACKEND` | Language model backend (`openai`) |
| `MECHUS_OPENAI_API_KEY` | OpenAI API key |
| `MONGODB_URI` | MongoDB connection string |
| `MECHUS_INSPECT_TOUR` | Optional custom inspection route |
| `MECHUS_INSPECT_INTERVAL_S` | Optional frame-analysis interval |
| `MECHUS_PUBLIC_BASE_URL` | Public base URL used for report links |
| `ZOOM_APP_CLIENT_ID` | Optional Zoom SDK client ID |
| `ZOOM_APP_CLIENT_SECRET` | Optional Zoom SDK client secret |

Optional settings can be left unset to use their defaults.

## Project Structure

```text
Mechus/
├── mechus/
│   ├── api_server.py        # Flask API server and dashboard backend
│   ├── chert_worker.py      # Per-call FaceTime worker
│   ├── chert_livekit.py     # LiveKit audio and video publishing
│   ├── inspection.py        # Frame analysis, guided tours, photo capture
│   ├── estates/
│   │   └── checks.py        # Inspection report page
│   ├── static/
│   │   └── properties/      # Dashboard static assets
│   └── brand.py             # Assistant branding and wake word
├── scripts/
│   └── run.py               # Application entry point
├── tests/                   # Pytest test suite
├── .env.example             # Environment variable template
├── requirements.txt         # Python dependencies
└── README.md
```

## Testing

Mechus uses Pytest for automated testing.

Run the test suite:

```bash
python -m pytest tests
```

The project currently has 661 passing tests on Windows.

One virtual-camera test is Linux-specific and fails in that environment. This is an environment limitation rather than a broader test-suite failure.

## Limitations

- **Shared FaceTime Line:** A shared Chert line handles one call at a time.
- **Tenant Interaction:** During inspections, Mechus analyzes the camera feed and provides spoken guidance but does not listen to the tenant.
- **AI Accuracy:** Findings are estimates and can be incorrect or incomplete. Human review is required before decisions about property condition or tenant responsibility.
- **Real-World Testing:** The guided inspection process has had limited testing on real calls.
- **Public Report Links:** Report links are publicly accessible and should not be treated as private.
- **iMessage Dependency:** Landlord notifications require Chert messaging. Without it, summaries are logged on the property timeline.

## Team

- **Odey**
- **Rajiv**
- **Goutham**

---

*No clipboard. No arguing over blurry photos. Just answer the call.*
