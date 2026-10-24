# J.A.R.V.I.S. Command Center

A personal AI command center with a FastAPI backend and an interactive, neon-lit 3D monitoring HUD.

## Features

- **FastAPI backend:** serves the dashboard, system diagnostics, and an allowlisted set of local application commands.
- **WebSockets:** provides a bidirectional `/ws` connection for commands, assistant responses, and live system telemetry.
- **Three.js HUD:** renders an interactive WebGL command center with REACTOR, GLOBE, and METRICS visualization modes, OrbitControls, and restrained bloom effects.
- **Gemini integration:** uses the Google GenAI API for conversational responses and intent-based visualization mode selection.
- **Voice interaction:** uses the browser Web Speech API for continuous speech recognition and speech synthesis, with audio-reactive visual effects.
- **System telemetry:** reports CPU, RAM, and uptime using `psutil`.

## Requirements

- Python 3.10 or later
- A modern browser with Web Speech API support for voice features
- A Gemini API key to enable AI-generated responses

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set your Gemini API key in the current PowerShell session:

```powershell
$env:GEMINI_API_KEY = "your-api-key"
```

Alternatively, set `GOOGLE_API_KEY`. You can optionally set `GEMINI_MODEL` to choose a Gemini model; the default is `gemini-2.0-flash`.

## Run

From the project root, start the development server:

```powershell
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000> in a browser. The dashboard uses the server's `/ws` WebSocket endpoint and `/api/system/stats` diagnostics endpoint. Microphone and voice features require browser permission and may require HTTPS or localhost.

## Safety and configuration notes

- Do not commit API keys or other secrets. `.env` and local development folders are ignored by Git.
- Local application launching is restricted to the backend's explicit allowlist.
- GPU and weather indicators remain unavailable unless corresponding data sources are added.
