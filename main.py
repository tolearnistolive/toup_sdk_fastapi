"""
ToupCamera FastAPI Web Streaming Application
Provides live video streaming and high-resolution capture via web interface
Supports dual resolution: fast streaming + high-res still capture
"""
import os
import asyncio
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from camera_manager import camera_manager


# Pydantic models for request/response
class ExposureSettings(BaseModel):
    time_us: int


class GainSettings(BaseModel):
    percent: int


class ResolutionSettings(BaseModel):
    index: int


class CaptureResolutionSettings(BaseModel):
    index: int


class WhiteBalanceSettings(BaseModel):
    temp: Optional[int] = None
    tint: Optional[int] = None


class AutoExposureSettings(BaseModel):
    enabled: bool


class CaptureRequest(BaseModel):
    resolution_index: Optional[int] = None
    filename: Optional[str] = None


class CaptureResponse(BaseModel):
    success: bool
    filename: str = ""
    message: str = ""
    width: int = 0
    height: int = 0


# Lifespan management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage camera lifecycle"""
    # Startup: try to open camera
    camera_manager.open_camera()
    yield
    # Shutdown: close camera
    camera_manager.close_camera()


# Create FastAPI app
app = FastAPI(
    title="ToupCamera Web Streaming",
    description="Live video streaming and high-resolution capture from ToupCamera",
    version="2.0.0",
    lifespan=lifespan
)

# Setup templates and static files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(BASE_DIR, "templates")
static_dir = os.path.join(BASE_DIR, "static")

# Create directories if they don't exist
os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

templates = Jinja2Templates(directory=templates_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# MJPEG streaming generator
async def generate_mjpeg():
    """Generate MJPEG stream from camera frames"""
    while True:
        if not camera_manager.is_open:
            await asyncio.sleep(0.5)
            continue
        
        frame = camera_manager.get_current_frame()
        if frame:
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
            )
        
        await asyncio.sleep(0.008)  # ~120 FPS max for real-time streaming


# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main web interface"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/stream")
async def video_stream():
    """MJPEG video stream endpoint (uses streaming resolution)"""
    return StreamingResponse(
        generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/frame")
async def get_frame():
    """Get a single JPEG frame at streaming resolution"""
    frame = camera_manager.get_current_frame()
    if frame:
        return StreamingResponse(
            iter([frame]),
            media_type="image/jpeg"
        )
    raise HTTPException(status_code=503, detail="No frame available")


@app.post("/capture", response_model=CaptureResponse)
async def capture_image(request: CaptureRequest = CaptureRequest()):
    """
    Capture a high-resolution still image
    
    Uses hardware Snap for maximum quality at the selected still resolution.
    """
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    try:
        saved_path = camera_manager.capture_still_image(
            filename=request.filename,
            resolution_index=request.resolution_index
        )
        
        # Get resolution info
        still_res = camera_manager.get_still_resolutions()
        res_index = request.resolution_index if request.resolution_index is not None else 0
        width = height = 0
        if still_res and res_index < len(still_res):
            width = still_res[res_index]["width"]
            height = still_res[res_index]["height"]
        
        return CaptureResponse(
            success=True,
            filename=saved_path,
            message=f"High-res image saved: {saved_path}",
            width=width,
            height=height
        )
    except Exception as e:
        return CaptureResponse(
            success=False,
            message=str(e)
        )


@app.get("/camera/status")
async def camera_status():
    """Get camera connection status and info"""
    return camera_manager.get_camera_info()


@app.post("/camera/open")
async def open_camera():
    """Open/connect to camera"""
    if camera_manager.is_open:
        return {"success": True, "message": "Camera already open"}
    
    success = camera_manager.open_camera()
    if success:
        return {"success": True, "message": "Camera opened successfully"}
    else:
        raise HTTPException(status_code=503, detail="Failed to open camera. No camera found.")


@app.post("/camera/close")
async def close_camera():
    """Close/disconnect camera"""
    camera_manager.close_camera()
    return {"success": True, "message": "Camera closed"}


@app.get("/settings")
async def get_settings():
    """Get all camera settings"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    return {
        "exposure": camera_manager.get_exposure_range(),
        "gain": camera_manager.get_gain_range(),
        "auto_exposure": camera_manager.get_auto_exposure(),
        "white_balance": camera_manager.get_white_balance(),
        "resolutions": camera_manager.get_resolutions(),
        "still_resolutions": camera_manager.get_still_resolutions()
    }


@app.get("/settings/resolutions")
async def get_resolutions():
    """Get available streaming resolutions"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    return {"resolutions": camera_manager.get_resolutions()}


@app.get("/settings/still_resolutions")
async def get_still_resolutions():
    """Get available still/capture resolutions"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    return {"still_resolutions": camera_manager.get_still_resolutions()}


@app.put("/settings/resolution")
async def set_resolution(settings: ResolutionSettings):
    """
    Set camera streaming resolution
    
    Lower resolution = faster streaming (less lag when moving slide)
    Higher resolution = more detail but slower
    """
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_resolution(settings.index)
    if success:
        return {
            "success": True,
            "resolutions": camera_manager.get_resolutions(),
            "message": f"Streaming resolution changed"
        }
    raise HTTPException(status_code=400, detail="Failed to set resolution")


@app.put("/settings/capture_resolution")
async def set_capture_resolution(settings: CaptureResolutionSettings):
    """Set the resolution to use for still image capture"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_capture_resolution(settings.index)
    if success:
        return {
            "success": True,
            "still_resolutions": camera_manager.get_still_resolutions()
        }
    raise HTTPException(status_code=400, detail="Failed to set capture resolution")


@app.put("/settings/exposure")
async def set_exposure(settings: ExposureSettings):
    """Set exposure time"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_exposure(settings.time_us)
    if success:
        return {"success": True, "exposure": camera_manager.get_exposure_range()}
    raise HTTPException(status_code=400, detail="Failed to set exposure")


@app.put("/settings/gain")
async def set_gain(settings: GainSettings):
    """Set gain"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_gain(settings.percent)
    if success:
        return {"success": True, "gain": camera_manager.get_gain_range()}
    raise HTTPException(status_code=400, detail="Failed to set gain")


@app.put("/settings/auto_exposure")
async def set_auto_exposure(settings: AutoExposureSettings):
    """Enable or disable auto exposure"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_auto_exposure(settings.enabled)
    if success:
        return {"success": True, "auto_exposure": camera_manager.get_auto_exposure()}
    raise HTTPException(status_code=400, detail="Failed to set auto exposure")


@app.put("/settings/white_balance")
async def set_white_balance(settings: WhiteBalanceSettings):
    """Set white balance"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.set_white_balance(settings.temp, settings.tint)
    if success:
        return {"success": True, "white_balance": camera_manager.get_white_balance()}
    raise HTTPException(status_code=400, detail="Failed to set white balance")


@app.post("/settings/auto_white_balance")
async def auto_white_balance():
    """Perform one-shot auto white balance"""
    if not camera_manager.is_open:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    success = camera_manager.auto_white_balance()
    if success:
        return {"success": True, "message": "Auto white balance performed"}
    raise HTTPException(status_code=400, detail="Failed to perform auto white balance")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
