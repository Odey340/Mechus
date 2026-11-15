# Mechus

**An AI-powered FaceTime inspection assistant that guides tenants through property inspections and documents visible damage for landlords.**

## What It Is

Mechus is an AI-powered property inspection assistant that automates guided move-in and move-out inspections over FaceTime.

A landlord adds a tenant through the web dashboard and initiates an inspection. Mechus calls the tenant through FaceTime Video, guides them through the property room by room, identifies potential damage through the tenant's camera, and documents its findings for the landlord.

Tenants do not need to install an application or create an account. They simply answer a FaceTime call and follow Mechus's spoken instructions.

When the inspection finishes, the landlord receives access to a printable inspection report containing findings, photographs, and a room-by-room record of the inspection.

## How It Works

Mechus combines a Flask dashboard, a LiveKit video pipeline, a FaceTime bridge, and Gemini vision analysis to conduct inspections.

The inspection process works as follows:

1. **Inspection initiated:** The landlord starts an inspection from the web dashboard. Flask launches a dedicated worker process for the call.
2. **LiveKit session:** The worker joins a LiveKit room, publishing its audio track and a video tile containing a status card that displays the current inspection stop.
3. **FaceTime connection:** Chert's FaceTime bridge joins the same room and places a FaceTime call to the tenant. The tenant's camera and audio arrive as ordinary LiveKit tracks.
4. **Frame analysis:** Every three seconds, Mechus sends a camera frame to Gemini Flash-Lite for analysis. The model returns structured JSON describing:
   - The area currently visible.
   - Whether the current tour stop is visible.
   - Potential damage findings, including bounding boxes.
   - A spoken response or instruction.
5. **Spoken guidance:** Mechus communicates with the tenant through its published audio track, guiding them through the inspection.
6. **Report generation:** When the call ends, the inspection results are processed into a report. The landlord is notified through iMessage when messaging is configured.

## Features

### Guided Property Tours

Mechus guides tenants through one room and one inspection item at a time.

The default inspection route is:

- Living room
- Kitchen
- Bathroom
- Bedroom

The route can be customized using `KOOV_INSPECT_TOUR`.

Tour progression is handled automatically:
- A stop passes after two clear looks.
- A stop is flagged when damage is detected.
- A stop is marked as not shown after one reminder.
- Mechus finishes the inspection with a spoken summary.

### Damage Detection and Findings

- Camera frames are analyzed using Gemini Flash-Lite.
- Potential damage findings include bounding boxes.
- Findings are consolidated across frames using Jaccard similarity on word sets.
- Each finding is associated with the relevant inspection stop.

### Inspection Photography

Mechus captures:
- One photograph per finding, with the detected damage outlined in a red bounding box.
- One photograph for each inspection stop that passes.

### Printable Inspection Reports

Each completed inspection produces a printable report containing:

- An overall verdict: **Passed**, **Needs review**, or **Incomplete**.
- Inspection findings and associated photographs.
- A room-by-room inspection record.
- An indication that AI-generated findings require human review.

### Landlord Dashboard

The web dashboard includes several sections:

- **Tenants:** Add tenants and initiate move-in or move-out inspections.
- **Reports:** View completed inspection reports and their results.
- **Overview:** Access the property dashboard overview.
- **Properties:** Manage property information.
- **Meetings:** Initiate FaceTime calls using a phone number or Apple ID email.

### FaceTime Calling

- Calls tenants through Chert's FaceTime bridge.
- Supports phone numbers and Apple ID email addresses.
- Displays understandable call failures, including:
  - Unverified recipient.
  - Line in use, displayed as "Someone else is using the line."
  - No answer.

### Voice Interaction

Mechus supports the wake word **"Mechus"** for its ordinary assistant calls, with speech-to-text handling.

During inspection calls, however, Mechus does not listen to the tenant. It watches the camera and speaks its inspection instructions.

### Landlord Notifications

When Chert messaging is configured, Mechus sends the inspection summary to the landlord through iMessage.

If messaging is unavailable or unconfigured, the summary is logged on the property's timeline.

### API Rate-Limit Handling

The inspection pipeline includes handling for HTTP 429 responses from the vision model.

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
| SQLite | Local database storage |
| JavaScript, HTML, CSS | Dashboard interface |
| Docker | Containerization |
| Vercel | Static deployment |

The report pipeline also supports uploaded inspection images and local-file fallback for inspection frame storage.

## Getting Started

### Prerequisites

- Python 3.11
- API credentials for the services you intend to use.
- A configured LiveKit instance.
- Access to Chert for FaceTime calling.

### Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/Mechus.git
cd Mechus
```

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

Copy the example environment file:

```bash
cp .env.example .env
```

Fill in the required API keys and configuration values in `.env`.

### Run the Application

Start the Flask application:

```bash
python scripts/run.py
```

The default API port is `5000`, configurable through `API_PORT`.

Open the dashboard:

```text
http://127.0.0.1:5000
```

Navigate to the **Tenants** tab to begin setting up an inspection.

## Configuration

Mechus uses environment variables for API credentials, service configuration, and inspection settings.

Create a `.env` file in the project root. Never commit this file or expose its credentials publicly.

### Environment Variables

| Variable | Description |
|---|---|
| `CHERT_API_KEY` | Chert API authentication key |
| `CHERT_PHONE_NUMBER` | Chert phone configuration |
| `LIVEKIT_URL` | LiveKit server URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `GEMINI_API_KEY` | Google Gemini API key |
| `KOOV_ELEVEN_VOICE_ID` | ElevenLabs voice ID |
| `KOOV_OPENAI_API_KEY` | OpenAI API key |
| `KOOV_INSPECT_TOUR` | Optional custom inspection route |
| `KOOV_INSPECT_INTERVAL_S` | Optional inspection frame-analysis interval |
| `KOOV_PUBLIC_BASE_URL` | Public base URL used for report links |
| `KOOV_CHERT_SIGNING_SECRET` | Chert signing secret |
| `KOOV_CHERT_TENANT` | Chert tenant configuration for iMessage |

**Note:** Some configuration settings retain the historical `koov` / `KOOV_` naming prefix.

Optional inspection settings can be left unset to use their application defaults.

## Project Structure

The main application is organized into the following components:

```text
Mechus/
│
├── koov/
│   ├── api_server.py          # Flask API server and dashboard backend
│   ├── chert_worker.py        # Per-call FaceTime worker
│   ├── chert_livekit.py       # LiveKit audio and video publishing
│   ├── inspection.py          # Frame analysis, guided tours, photo capture
│   ├── estates/
│   │   └── checks.py          # Inspection report page
│   ├── static/
│   │   └── properties/        # Dashboard static assets
│   └── brand.py               # Assistant branding and wake word
│
├── scripts/
│   └── run.py                 # Application entry point
│
├── tests/                     # Pytest test suite
│
├── .env.example               # Example environment configuration
├── requirements.txt           # Python dependencies
└── README.md
```

## Testing

Mechus uses `pytest` for automated testing.

Run the test suite from the repository root:

```bash
python -m pytest tests
```

The test suite has 661 passing tests on Windows.

One virtual-camera test is Linux-specific and fails when run in that environment. This is an environment-specific limitation rather than an indication that the entire test suite is failing.

## Limitations

Mechus is an active project with several known limitations:

- **Shared FaceTime line:** A shared Chert line accepts and handles one call at a time.
- **No tenant listening during inspections:** During inspection calls, Mechus does not listen to the tenant. It watches the camera and provides spoken guidance.
- **AI accuracy:** Findings are AI-generated estimates and can be incorrect or incomplete. Landlords must review the results before making decisions about property condition or tenant responsibility.
- **Real-world testing:** The guided inspection tour has had limited testing on real calls.
- **Public report links:** Report links are publicly accessible. They should not be treated as private or access-controlled.
- **iMessage configuration:** Sending reports through iMessage requires Chert messaging to be configured. Without it, summaries are logged on the property timeline.

## Team

**Mechus Development Team**

Built as an AI-powered property inspection project, combining real-time communication, computer vision, and automated reporting to make property inspections easier to conduct and document.


- **TTS:** `KOOV_TTS_BACKEND=elevenlabs`, `KOOV_ELEVEN_API_KEY`, `KOOV_ELEVEN_VOICE_ID`.
- **LLM:** `KOOV_LLM_BACKEND=openai`, `KOOV_OPENAI_API_KEY`.
- **Zoom SDK join:** `ZOOM_APP_CLIENT_ID`, `ZOOM_APP_CLIENT_SECRET`.
- **FaceTime (Chert):** `CHERT_API_KEY`, `CHERT_PROJECT_ID`, `CHERT_LINE_ID`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

All behavior is driven by environment variables so you can deploy and scale without code changes.
