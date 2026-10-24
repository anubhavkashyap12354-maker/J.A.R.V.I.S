import asyncio
import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Awaitable, Callable

import psutil
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


logger = logging.getLogger(__name__)
_tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts")
_tts_engine: Any | None = None
_tts_lock = Lock()
_tts_unavailable = False
_AI_SYSTEM_PROMPT = (
    "You are J.A.R.V.I.S., an original AI assistant with a polished, composed, "
    "highly capable presence. Be polite, articulate, concise, subtly witty, and "
    "intelligent. Speak conversationally and naturally. Keep every response to "
    "one to three short sentences. Never claim to have performed an action "
    "unless a tool result confirms it. Do not provide instructions for unsafe "
    "or unauthorized actions. Respond only with a valid JSON object containing "
    'exactly two fields: {"response_text": string, "ui_mode": string}. '
    "Set ui_mode to GLOBE for news, weather, travel, geography, or world-related "
    "requests; set it to METRICS for hardware, CPU, memory, or system diagnostic "
    "requests; use REACTOR for general conversation and all other requests."
)
_UI_MODES = {"GLOBE", "REACTOR", "METRICS"}


class SystemCommandRequest(BaseModel):
    command: str


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[websocket] = asyncio.Lock()

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def send_json(self, websocket: WebSocket, payload: dict[str, object]) -> bool:
        return await self._send(websocket, websocket.send_json, payload)

    async def send_text(self, websocket: WebSocket, message: str) -> bool:
        return await self._send(websocket, websocket.send_text, message)

    async def _send(
        self,
        websocket: WebSocket,
        send: Callable[[Any], Awaitable[None]],
        payload: Any,
    ) -> bool:
        lock = self._connections.get(websocket)
        if lock is None:
            return False

        try:
            async with lock:
                await send(payload)
        except (OSError, RuntimeError, WebSocketDisconnect):
            self.disconnect(websocket)
            return False
        return True

    async def broadcast(self, payload: dict[str, object]) -> None:
        await asyncio.gather(
            *(self.send_json(websocket, payload) for websocket in tuple(self._connections))
        )


manager = ConnectionManager()
_ALLOWED_COMMANDS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "task-manager": "taskmgr.exe",
}
_APP_COMMANDS = {
    "notepad": ("notepad", "Notepad"),
    "calculator": ("calculator", "Calculator"),
    "calc": ("calculator", "Calculator"),
    "task manager": ("task-manager", "Task Manager"),
    "task-manager": ("task-manager", "Task Manager"),
    "taskmgr": ("task-manager", "Task Manager"),
}


def _limit_response_sentences(text: str) -> str:
    sentences = re.findall(r"[^.!?]+(?:[.!?]+|$)", text.strip())
    return " ".join(sentence.strip() for sentence in sentences[:3]).strip()


def _generate_ai_response(query: str) -> dict[str, str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return {
            "response_text": "My AI connection isn't configured yet. Set GEMINI_API_KEY to enable conversational responses.",
            "ui_mode": "REACTOR",
        }

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        result = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=_AI_SYSTEM_PROMPT,
                max_output_tokens=180,
                temperature=0.7,
                response_mime_type="application/json",
            ),
        )
        parsed = json.loads(result.text or "")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("response_text"), str):
            raise ValueError("Gemini response did not contain a response_text string.")
        response_text = _limit_response_sentences(parsed["response_text"])
        if not response_text:
            raise ValueError("Gemini returned an empty response_text.")
        ui_mode = parsed.get("ui_mode")
        if not isinstance(ui_mode, str) or ui_mode not in _UI_MODES:
            logger.warning("Gemini returned an invalid ui_mode; using REACTOR.")
            ui_mode = "REACTOR"
        return {"response_text": response_text, "ui_mode": ui_mode}
    except (json.JSONDecodeError, ValueError):
        logger.exception("Gemini returned an invalid structured response.")
        return {
            "response_text": "I couldn't form a response just now. Please try again.",
            "ui_mode": "REACTOR",
        }
    except ImportError:
        logger.warning("google-genai is unavailable; using text-only AI fallback.")
        return {
            "response_text": "My AI engine isn't installed yet. Install google-genai to enable conversational responses.",
            "ui_mode": "REACTOR",
        }
    except Exception:
        logger.exception("AI response generation failed.")
        return {
            "response_text": "I'm having trouble reaching my AI core right now. Please try again shortly.",
            "ui_mode": "REACTOR",
        }


async def process_user_query(query: str) -> dict[str, str]:
    normalized = re.sub(r"[.,!?]", " ", query.casefold())
    normalized = re.sub(r"^(?:(?:hey|ok(?:ay)?)\s+)?jarvis[\s,:-]*", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    stats_requests = {
        "system status",
        "system stats",
        "system statistics",
        "fetch system stats",
        "get system stats",
        "run system scan",
        "system scan",
        "cpu and ram",
        "cpu and memory",
    }
    stats_intent = re.fullmatch(
        r"(?:please )?(?:get|fetch|show|check|run|scan)(?: my)? "
        r"(?:system )?(?:status|stats|statistics|scan|cpu(?: and (?:ram|memory))?|memory|ram)"
        r"(?: please)?",
        normalized,
    )
    stats_question = re.fullmatch(
        r"(?:what is|what are|how much|tell me)(?: my)? "
        r"(?:system )?(?:status|stats|statistics|cpu(?: and (?:ram|memory))?|memory|ram)",
        normalized,
    )
    if normalized in stats_requests or stats_intent or stats_question:
        stats = await asyncio.to_thread(get_system_stats)
        return {
            "response_text": (
                f"CPU usage is {stats['cpu_percent']:.0f} percent, and memory usage is "
                f"{stats['ram_used_gb']:.2f} of {stats['ram_total_gb']:.2f} gigabytes. "
                f"System uptime is {stats['uptime_seconds'] // 3600} hours."
            ),
            "ui_mode": "METRICS",
        }

    app_match = re.fullmatch(
        r"(?:please )?(?:open|launch|start) (notepad|calculator|calc|task manager|task-manager|taskmgr)",
        normalized,
    )
    if app_match:
        command, app_name = _APP_COMMANDS[app_match.group(1)]
        try:
            process = await asyncio.to_thread(execute_system_command, command)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Could not launch requested application %s: %s", command, error)
            return {"response_text": f"I couldn't launch {app_name}. {error}", "ui_mode": "REACTOR"}
        return {
            "response_text": f"{app_name} is open. Its process ID is {process.pid}.",
            "ui_mode": "REACTOR",
        }

    return await asyncio.to_thread(_generate_ai_response, query)


def speak_response(text: str) -> bool:
    """Speak text locally; return False when the TTS engine cannot be used."""
    global _tts_engine, _tts_unavailable

    if not isinstance(text, str):
        raise TypeError("Speech text must be a string.")
    if not text.strip():
        return True

    with _tts_lock:
        if _tts_unavailable:
            return False
        try:
            if _tts_engine is None:
                import pyttsx3

                _tts_engine = pyttsx3.init()
            _tts_engine.say(text)
            _tts_engine.runAndWait()
        except ImportError:
            _tts_unavailable = True
            logger.warning("pyttsx3 is unavailable; continuing with text-only responses.")
            return False
        except Exception:
            _tts_engine = None
            logger.exception("Local speech synthesis failed; continuing with text-only response.")
            return False
    return True


def get_system_stats() -> dict[str, float | int]:
    memory = psutil.virtual_memory()
    bytes_per_gb = 1024**3
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_total_gb": round(memory.total / bytes_per_gb, 2),
        "ram_used_gb": round(memory.used / bytes_per_gb, 2),
        "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
    }


def execute_system_command(command: str) -> subprocess.Popen:
    executable = _ALLOWED_COMMANDS.get(command.strip().casefold())
    if executable is None:
        allowed = ", ".join(_ALLOWED_COMMANDS)
        raise ValueError(f"Unsupported command. Allowed commands: {allowed}")
    if os.name != "nt":
        raise RuntimeError("System application launching is only configured for Windows.")

    executable_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / executable
    if not executable_path.is_file():
        raise FileNotFoundError(f"Application is not installed: {command}")

    return subprocess.Popen(
        [str(executable_path)],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


async def broadcast_system_stats() -> None:
    while True:
        stats = await asyncio.to_thread(get_system_stats)
        await manager.broadcast({"type": "system_stats", "data": stats})
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stats_task = asyncio.create_task(broadcast_system_stats())
    try:
        yield
    finally:
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="J.A.R.V.I.S.", lifespan=lifespan)


@app.get("/api/system/stats")
async def system_stats() -> dict[str, float | int]:
    return await asyncio.to_thread(get_system_stats)


@app.post("/api/system/command")
async def launch_system_command(request: SystemCommandRequest) -> dict[str, str | int]:
    try:
        process = await asyncio.to_thread(execute_system_command, request.command)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not launch application: {error}") from error
    return {"command": request.command, "pid": process.pid}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                request: object = json.loads(raw_message)
            except json.JSONDecodeError:
                request = raw_message

            if isinstance(request, dict) and request.get("type") == "transcript":
                query = request.get("text")
                browser_speech = request.get("speech_output") == "browser"
            elif isinstance(request, str):
                query = request
                browser_speech = False
            else:
                query = None
                browser_speech = False

            if not isinstance(query, str) or not query.strip():
                response_payload = {
                    "response_text": "Please send a non-empty text query.",
                    "ui_mode": "REACTOR",
                }
            else:
                response_payload = await process_user_query(query.strip())

            response = {
                "type": "response",
                **response_payload,
                "text": response_payload["response_text"],
            }
            if not browser_speech:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(
                    _tts_executor, speak_response, response_payload["response_text"]
                )

            if not await manager.send_json(websocket, response):
                break
    except WebSocketDisconnect:
        manager.disconnect(websocket)


app.mount(
    "/",
    StaticFiles(directory=Path(__file__).resolve().parent, html=True),
    name="static",
)
