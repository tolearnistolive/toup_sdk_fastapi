"""
ToupCamera Manager Module
Thread-safe camera management for FastAPI web streaming
Supports dual resolution: fast streaming + high-resolution still capture
"""
import threading
import io
import time
import toupcam
from PIL import Image
from typing import Optional, Dict, Any, Callable
from datetime import datetime


class ToupCameraManager:
    """Thread-safe manager for ToupCamera devices with dual resolution support"""
    
    def __init__(self):
        self.hcam = None
        self.cur = None
        self.img_width = 0
        self.img_height = 0
        self.pData = None
        self.res = 0  # Current streaming resolution index
        self.snap_res = 0  # Capture resolution index (highest by default)
        self.temp = toupcam.TOUPCAM_TEMP_DEF
        self.tint = toupcam.TOUPCAM_TINT_DEF
        self.frame_count = 0
        self.fps = 0.0
        self.capture_count = 0
        
        # Thread safety
        self._frame_lock = threading.Lock()
        self._current_frame: Optional[bytes] = None  # JPEG bytes for streaming
        self._frame_available = threading.Event()
        
        # Polling thread
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        
        # Still image capture
        self._still_lock = threading.Lock()
        self._still_image: Optional[bytes] = None  # High-res JPEG
        self._still_requested = False
        self._still_complete = threading.Event()
        self._still_filename: Optional[str] = None
        
        # Callbacks for external notifications
        self._on_error: Optional[Callable[[str], None]] = None
    
    @property
    def is_open(self) -> bool:
        """Check if camera is currently open"""
        return self.hcam is not None and self._running
    
    def enumerate_cameras(self) -> list:
        """Get list of available cameras"""
        return toupcam.Toupcam.EnumV2()
    
    def get_still_resolutions(self) -> list:
        """Get available still/capture resolutions (usually higher than preview)"""
        if not self.cur:
            return []
        
        still_count = self.cur.model.still
        resolutions = []
        
        if still_count == 0:
            # No dedicated still resolutions, use preview resolutions
            for i in range(self.cur.model.preview):
                resolutions.append({
                    "index": i,
                    "width": self.cur.model.res[i].width,
                    "height": self.cur.model.res[i].height,
                    "current": i == self.snap_res
                })
        else:
            # Dedicated still resolutions available
            for i in range(still_count):
                resolutions.append({
                    "index": i,
                    "width": self.cur.model.res[i].width,
                    "height": self.cur.model.res[i].height,
                    "current": i == self.snap_res
                })
        
        return resolutions
    
    def open_camera(self, camera_id: Optional[str] = None) -> bool:
        """
        Open and start the camera
        
        Args:
            camera_id: Specific camera ID, or None for first available
            
        Returns:
            True if successful, False otherwise
        """
        if self.hcam:
            self.close_camera()
        
        # Enable GigE support
        toupcam.Toupcam.GigeEnable(None, None)
        
        # Find cameras
        arr = toupcam.Toupcam.EnumV2()
        if len(arr) == 0:
            print("[Camera] No cameras found")
            return False
        
        # Select camera
        if camera_id:
            for cam in arr:
                if cam.id == camera_id:
                    self.cur = cam
                    break
            else:
                return False
        else:
            self.cur = arr[0]
        
        print(f"[Camera] Opening: {self.cur.displayname}")
        
        # Open camera
        self.hcam = toupcam.Toupcam.Open(self.cur.id)
        if not self.hcam:
            print("[Camera] Failed to open camera")
            return False
        
        # Get current resolution (use lowest for fast streaming by default)
        self.res = self.cur.model.preview - 1  # Start with lowest resolution for speed
        self.img_width = self.cur.model.res[self.res].width
        self.img_height = self.cur.model.res[self.res].height
        
        # Set capture resolution to highest available
        still_count = self.cur.model.still
        if still_count > 0:
            self.snap_res = 0  # Highest still resolution (index 0 is typically highest)
        else:
            self.snap_res = 0  # Use highest preview resolution
        
        print(f"[Camera] Stream resolution: {self.img_width}x{self.img_height} (index {self.res})")
        print(f"[Camera] Still resolutions available: {still_count}")
        
        # Allocate buffer
        self.pData = bytes(toupcam.TDIBWIDTHBYTES(self.img_width * 24) * self.img_height)
        
        # Configure camera
        self.hcam.put_Option(toupcam.TOUPCAM_OPTION_BYTEORDER, 0)  # RGB byte order
        self.hcam.put_eSize(self.res)  # Set streaming resolution
        
        # Start pull mode WITHOUT callback (we'll poll manually)
        try:
            self.hcam.StartPullModeWithCallback(None, None)
            self.hcam.put_AutoExpoEnable(1)
            
            # Start polling thread
            self._running = True
            self._poll_thread = threading.Thread(target=self._poll_frames, daemon=True)
            self._poll_thread.start()
            
            print("[Camera] Started successfully with polling thread")
            return True
        except toupcam.HRESULTException as e:
            print(f"[Camera] Failed to start: {e}")
            self.close_camera()
            return False
    
    def close_camera(self):
        """Close the camera and clean up resources"""
        self._running = False
        
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        
        if self.hcam:
            self.hcam.Close()
        self.hcam = None
        self.cur = None
        self.pData = None
        with self._frame_lock:
            self._current_frame = None
        with self._still_lock:
            self._still_image = None
    
    def _poll_frames(self):
        """Background thread that polls for frames"""
        print("[Camera] Polling thread started")
        last_fps_time = time.time()
        frame_count_for_fps = 0
        
        while self._running and self.hcam:
            try:
                # Wait for a frame (50ms timeout)
                self.hcam.WaitImageV4(50, self.pData, 0, 24, 0, None)
                
                # Process the frame for streaming
                self._process_frame()
                
                # Calculate FPS
                frame_count_for_fps += 1
                now = time.time()
                if now - last_fps_time >= 1.0:
                    self.fps = frame_count_for_fps / (now - last_fps_time)
                    self.frame_count += frame_count_for_fps
                    frame_count_for_fps = 0
                    last_fps_time = now
                
                # Check for still image capture (if Snap was called)
                if self._still_requested:
                    self._try_pull_still_image()
                    
            except toupcam.HRESULTException as e:
                # Timeout or error - check for still image anyway
                if self._still_requested:
                    self._try_pull_still_image()
                time.sleep(0.01)
            except Exception as e:
                print(f"[Camera] Polling error: {e}")
                time.sleep(0.1)
        
        print("[Camera] Polling thread stopped")
    
    def _try_pull_still_image(self):
        """Try to pull a still image if one is ready"""
        try:
            info = toupcam.ToupcamFrameInfoV3()
            self.hcam.PullImageV3(None, 1, 24, 0, info)  # Peek
            
            if info.width > 0 and info.height > 0:
                # Still image is ready
                buf = bytes(toupcam.TDIBWIDTHBYTES(info.width * 24) * info.height)
                self.hcam.PullImageV3(buf, 1, 24, 0, info)
                
                print(f"[Camera] Still image captured: {info.width}x{info.height}")
                
                # Convert to JPEG and save
                self._save_still_image(buf, info.width, info.height)
                
                self._still_requested = False
                self._still_complete.set()
        except toupcam.HRESULTException:
            # No still image ready yet
            pass
    
    def _save_still_image(self, buf: bytes, width: int, height: int):
        """Convert raw buffer to JPEG and save"""
        try:
            # Handle row stride
            row_stride = toupcam.TDIBWIDTHBYTES(width * 24)
            bytes_per_pixel = 3
            
            if row_stride != width * bytes_per_pixel:
                packed_data = bytearray()
                for y in range(height):
                    row_start = y * row_stride
                    row_end = row_start + (width * bytes_per_pixel)
                    packed_data.extend(buf[row_start:row_end])
                image = Image.frombytes('RGB', (width, height), bytes(packed_data))
            else:
                image = Image.frombytes('RGB', (width, height), buf)
            
            # Save the image
            if self._still_filename:
                image.save(self._still_filename, quality=95)
                print(f"[Camera] Saved: {self._still_filename}")
            
            # Also store as JPEG bytes
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=95)
            with self._still_lock:
                self._still_image = buffer.getvalue()
                
        except Exception as e:
            print(f"[Camera] Still image save error: {e}")
    
    def _process_frame(self):
        """Process a captured frame - ULTRA OPTIMIZED for real-time streaming"""
        try:
            # Fast path: minimal processing
            row_stride = toupcam.TDIBWIDTHBYTES(self.img_width * 24)
            bytes_per_pixel = 3
            
            if row_stride != self.img_width * bytes_per_pixel:
                # Has padding - fast removal
                packed_data = bytearray(self.img_width * self.img_height * 3)
                src = 0
                dst = 0
                row_bytes = self.img_width * bytes_per_pixel
                for _ in range(self.img_height):
                    packed_data[dst:dst + row_bytes] = self.pData[src:src + row_bytes]
                    src += row_stride
                    dst += row_bytes
                image = Image.frombytes('RGB', (self.img_width, self.img_height), bytes(packed_data))
            else:
                image = Image.frombytes('RGB', (self.img_width, self.img_height), self.pData)
            
            # ULTRA LOW QUALITY for maximum speed streaming
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=35, optimize=False, subsampling=2)
            jpeg_bytes = buffer.getvalue()
            
            with self._frame_lock:
                self._current_frame = jpeg_bytes
            self._frame_available.set()
            
        except Exception as e:
            pass  # Silent fail for speed
    
    def get_current_frame(self) -> Optional[bytes]:
        """Get the current frame as JPEG bytes (thread-safe)"""
        with self._frame_lock:
            return self._current_frame
    
    def wait_for_frame(self, timeout: float = 1.0) -> Optional[bytes]:
        """Wait for a new frame and return it"""
        self._frame_available.wait(timeout=timeout)
        self._frame_available.clear()
        return self.get_current_frame()
    
    def capture_still_image(self, filename: Optional[str] = None, resolution_index: Optional[int] = None) -> str:
        """
        Capture a high-resolution still image using hardware Snap
        
        Args:
            filename: Output filename, auto-generated if None
            resolution_index: Still resolution index, or None for highest
            
        Returns:
            Path to saved image
        """
        if not self.hcam:
            raise RuntimeError("Camera not open")
        
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"capture_{timestamp}.jpg"
        
        still_count = self.cur.model.still
        
        if still_count == 0:
            # No hardware still support - capture current frame at full quality
            print("[Camera] No still capture support, using current frame")
            frame = self.get_current_frame()
            if frame:
                with open(filename, 'wb') as f:
                    f.write(frame)
                self.capture_count += 1
                return filename
            raise RuntimeError("No frame available")
        
        # Use hardware Snap for high-res capture
        if resolution_index is None:
            resolution_index = 0  # Highest resolution (index 0)
        
        # Validate resolution index
        if resolution_index < 0 or resolution_index >= still_count:
            resolution_index = 0
        
        self._still_filename = filename
        self._still_requested = True
        self._still_complete.clear()
        
        print(f"[Camera] Requesting still image at index {resolution_index}")
        
        try:
            self.hcam.Snap(resolution_index)
        except toupcam.HRESULTException as e:
            self._still_requested = False
            raise RuntimeError(f"Snap failed: {e}")
        
        # Wait for still image to be captured
        if self._still_complete.wait(timeout=10.0):
            self.capture_count += 1
            return filename
        else:
            self._still_requested = False
            raise RuntimeError("Still capture timeout")
    
    def get_resolutions(self) -> list:
        """Get available streaming/preview resolutions"""
        if not self.cur:
            return []
        
        resolutions = []
        for i in range(self.cur.model.preview):
            resolutions.append({
                "index": i,
                "width": self.cur.model.res[i].width,
                "height": self.cur.model.res[i].height,
                "current": i == self.res
            })
        return resolutions
    
    def set_resolution(self, index: int) -> bool:
        """Set camera streaming resolution by index (lower = faster streaming)"""
        if not self.hcam or index < 0 or index >= self.cur.model.preview:
            return False
        
        # Stop polling
        was_running = self._running
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
        
        self.hcam.Stop()
        self.res = index
        self.img_width = self.cur.model.res[index].width
        self.img_height = self.cur.model.res[index].height
        self.pData = bytes(toupcam.TDIBWIDTHBYTES(self.img_width * 24) * self.img_height)
        
        print(f"[Camera] Resolution changed to: {self.img_width}x{self.img_height}")
        
        self.hcam.put_eSize(self.res)
        try:
            self.hcam.StartPullModeWithCallback(None, None)
            
            if was_running:
                self._running = True
                self._poll_thread = threading.Thread(target=self._poll_frames, daemon=True)
                self._poll_thread.start()
            
            return True
        except toupcam.HRESULTException:
            return False
    
    def set_capture_resolution(self, index: int) -> bool:
        """Set the resolution to use for still image capture"""
        if not self.cur:
            return False
        
        still_count = self.cur.model.still
        if still_count == 0:
            # No dedicated still resolutions
            if index >= 0 and index < self.cur.model.preview:
                self.snap_res = index
                return True
        else:
            if index >= 0 and index < still_count:
                self.snap_res = index
                return True
        
        return False
    
    def get_exposure_range(self) -> Dict[str, int]:
        """Get exposure time range in microseconds"""
        if not self.hcam:
            return {"min": 0, "max": 0, "current": 0}
        
        uimin, uimax, uidef = self.hcam.get_ExpTimeRange()
        current = self.hcam.get_ExpoTime()
        return {"min": uimin, "max": uimax, "default": uidef, "current": current}
    
    def set_exposure(self, time_us: int) -> bool:
        """Set exposure time in microseconds"""
        if not self.hcam:
            return False
        try:
            self.hcam.put_AutoExpoEnable(0)
            self.hcam.put_ExpoTime(time_us)
            return True
        except toupcam.HRESULTException:
            return False
    
    def get_gain_range(self) -> Dict[str, int]:
        """Get gain range in percent"""
        if not self.hcam:
            return {"min": 0, "max": 0, "current": 0}
        
        try:
            usmin, usmax, usdef = self.hcam.get_ExpoAGainRange()
            current = self.hcam.get_ExpoAGain()
            return {"min": usmin, "max": usmax, "default": usdef, "current": current}
        except toupcam.HRESULTException:
            return {"min": 0, "max": 500, "current": 100}
    
    def set_gain(self, percent: int) -> bool:
        """Set gain in percent"""
        if not self.hcam:
            return False
        try:
            self.hcam.put_AutoExpoEnable(0)
            self.hcam.put_ExpoAGain(percent)
            return True
        except toupcam.HRESULTException:
            return False
    
    def get_auto_exposure(self) -> bool:
        """Check if auto exposure is enabled"""
        if not self.hcam:
            return False
        return self.hcam.get_AutoExpoEnable() == 1
    
    def set_auto_exposure(self, enabled: bool) -> bool:
        """Enable or disable auto exposure"""
        if not self.hcam:
            return False
        try:
            self.hcam.put_AutoExpoEnable(1 if enabled else 0)
            return True
        except toupcam.HRESULTException:
            return False
    
    def get_white_balance(self) -> Dict[str, int]:
        """Get white balance settings"""
        if not self.hcam:
            return {"temp": self.temp, "tint": self.tint}
        try:
            self.temp, self.tint = self.hcam.get_TempTint()
        except:
            pass
        return {"temp": self.temp, "tint": self.tint}
    
    def set_white_balance(self, temp: Optional[int] = None, tint: Optional[int] = None) -> bool:
        """Set white balance temperature and tint"""
        if not self.hcam:
            return False
        
        if temp is not None:
            self.temp = temp
        if tint is not None:
            self.tint = tint
        
        try:
            self.hcam.put_TempTint(self.temp, self.tint)
            return True
        except toupcam.HRESULTException:
            return False
    
    def auto_white_balance(self) -> bool:
        """Perform one-shot auto white balance"""
        if not self.hcam:
            return False
        try:
            self.hcam.AwbOnce()
            return True
        except toupcam.HRESULTException:
            return False
    
    def get_camera_info(self) -> Dict[str, Any]:
        """Get comprehensive camera information"""
        if not self.cur:
            return {"connected": False}
        
        return {
            "connected": self.is_open,
            "name": self.cur.displayname,
            "id": self.cur.id,
            "resolution": {
                "width": self.img_width,
                "height": self.img_height,
                "index": self.res
            },
            "capture_resolution": {
                "index": self.snap_res,
                "still_count": self.cur.model.still
            },
            "frame_count": self.frame_count,
            "fps": round(self.fps, 1),
            "capture_count": self.capture_count,
            "exposure": self.get_exposure_range(),
            "gain": self.get_gain_range(),
            "auto_exposure": self.get_auto_exposure(),
            "white_balance": self.get_white_balance(),
            "resolutions": self.get_resolutions(),
            "still_resolutions": self.get_still_resolutions(),
            "is_mono": bool(self.cur.model.flag & toupcam.TOUPCAM_FLAG_MONO)
        }


# Global camera manager instance
camera_manager = ToupCameraManager()
