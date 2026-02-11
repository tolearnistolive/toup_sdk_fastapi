"""
ToupCamera FastAPI Web Streaming Application
FastAPI runs in its own thread
Serial listener runs in its own thread
Arduino commands:
  N -> create new folder (lock session)
  C -> capture image into current folder
  S -> unlock session (allow next N)
"""

import os
import time
import threading
import serial
import asyncio
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from camera_manager import camera_manager

# =========================
# FastAPI Models
# =========================

class ExposureSettings(BaseModel):
    time_us: int

class GainSettings(BaseModel):
    percent: int

class ResolutionSettings(BaseModel):
    index: int

class WhiteBalanceSettings(BaseModel):
    temp: Optional[int] = None
    tint: Optional[int] = None

class AutoExposureSettings(BaseModel):
    enabled: bool

# =========================
# Paths & State
# =========================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CAPTURE_ROOT = os.path.join(BASE_DIR, "captures")
os.makedirs(CAPTURE_ROOT, exist_ok=True)

current_session_dir = None
session_locked = False
state_lock = threading.Lock()

# =========================
# FastAPI Setup
# =========================

templates_dir = os.path.join(BASE_DIR, "templates")
static_dir = os.path.join(BASE_DIR, "static")

os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

templates = Jinja2Templates(directory=templates_dir)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(
    title="ToupCamera Web Streaming",
    version="1.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# =========================
# MJPEG Stream
# =========================

async def generate_mjpeg():
    while True:
        if not camera_manager.is_open:
            await asyncio.sleep(0.5)
            continue

        frame = camera_manager.get_current_frame()
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        await asyncio.sleep(0.033)

# =========================
# Routes
# =========================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/stream")
async def video_stream():
    return StreamingResponse(
        generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.post("/capture")
async def capture_http():
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")

    filename = datetime.now().strftime("%Y%m%d_%H%M%S.jpg")
    path = camera_manager.capture_image(filename)
    return {"success": True, "file": path}

# =========================
# Capture Session Logic
# =========================

def create_new_session():
    global current_session_dir, session_locked
    with state_lock:
        if session_locked:
            print("[SERIAL] Session locked, ignoring N")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        current_session_dir = os.path.join(CAPTURE_ROOT, f"session_{ts}")
        os.makedirs(current_session_dir, exist_ok=True)
        session_locked = True

        print(f"[SERIAL] New session created: {current_session_dir}")

def capture_to_session():
    with state_lock:
        if not current_session_dir:
            print("[SERIAL] No active session, ignoring C")
            return
        target_dir = current_session_dir

    if not camera_manager.is_open:
        print("[SERIAL] Camera not open")
        return

    filename = datetime.now().strftime("%H%M%S_%f.jpg")
    filepath = os.path.join(target_dir, filename)

    try:
        camera_manager.capture_image(filepath)
        print(f"[SERIAL] Captured: {filepath}")
    except Exception as e:
        print(f"[SERIAL] Capture failed: {e}")

def end_session():
    global session_locked
    with state_lock:
        session_locked = False
    print("[SERIAL] Session ended. Waiting for next N")

# =========================
# Serial Thread
# =========================

def serial_worker(port: str, baudrate: int = 9600):
    try:
        ser = serial.Serial(port, baudrate, timeout=1)
        print(f"[SERIAL] Connected to {port} @ {baudrate}")
    except Exception as e:
        print(f"[SERIAL] Failed to open serial: {e}")
        return

    while True:
        try:
            if ser.in_waiting:
                cmd = ser.read().decode(errors="ignore").strip().upper()

                if cmd == "N":
                    create_new_session()
                elif cmd == "C":
                    capture_to_session()
                elif cmd == "S":
                    end_session()
                else:
                    print(f"[SERIAL] Unknown command: {cmd}")
        except Exception as e:
            print(f"[SERIAL] Error: {e}")
            time.sleep(0.5)

# =========================
# FastAPI Thread
# =========================

def start_fastapi():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

# =========================
# Main
# =========================

if __name__ == "__main__":
    print("[MAIN] Opening camera...")
    camera_manager.open_camera()

    serial_thread = threading.Thread(
        target=serial_worker,
        args=("COM3", 9600),  # CHANGE PORT IF NEEDED
        daemon=True
    )

    api_thread = threading.Thread(
        target=start_fastapi,
        daemon=True
    )

    serial_thread.start()
    api_thread.start()

    print("[MAIN] System running. Ctrl+C to exit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down...")
        camera_manager.close_camera()
