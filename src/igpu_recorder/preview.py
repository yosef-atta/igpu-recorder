"""Desktop preview subsystem for low-overhead visual confirmation.

Implements lightweight primary display capture with:
- Direct Win32 GDI downscaled blit (StretchBlt) with zero external C dependencies
- Dynamic framerate capping (10 FPS idle, 5 FPS recording)
- Zero-workload suspension when minimized / hidden
- Thread-safe frame broadcasting to UI listeners
- Complete isolation from recording pipeline
"""

from __future__ import annotations

import abc
import ctypes
import enum
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from igpu_recorder.logging import get_logger

logger = get_logger("preview")

# Win32 Constants
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SRCCOPY = 0x00CC0020
COLORONCOLOR = 3
DIB_RGB_COLORS = 0
BI_RGB = 0
DESKTOP_ALL = 0x01FF


def ensure_thread_desktop_access() -> bool:
    """Ensure the current thread is attached to the interactive Default desktop.

    Returns:
        bool: True if attached or already attached, False otherwise.
    """
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, DESKTOP_ALL)
        if hdesk:
            return bool(user32.SetThreadDesktop(hdesk))
    except Exception as exc:
        logger.debug("Failed to set thread desktop to Default: %s", exc)
    return False


class PreviewMode(enum.Enum):
    """Operating modes for the preview engine."""

    OFF = "off"
    IDLE = "idle"  # ~10 FPS target
    RECORDING = "recording"  # ~5 FPS target
    SUSPENDED = "suspended"  # 0 FPS (worker thread asleep/paused)


@dataclass(frozen=True)
class PreviewFrame:
    """Immutable preview frame snapshot."""

    data: bytes
    width: int
    height: int
    channels: int
    timestamp: float
    frame_index: int
    fps: float


@dataclass(frozen=True)
class PreviewConfig:
    """Configuration for preview dimensions and framerate caps."""

    target_width: int = 480
    target_height: int = 270
    idle_fps: float = 10.0
    recording_fps: float = 5.0
    display_index: int = 0

    def __post_init__(self) -> None:
        if self.target_width <= 0 or self.target_height <= 0:
            msg = f"Invalid preview dimensions: {self.target_width}x{self.target_height}"
            raise ValueError(msg)
        if self.idle_fps <= 0 or self.recording_fps <= 0:
            msg = f"Invalid preview FPS: idle={self.idle_fps}, recording={self.recording_fps}"
            raise ValueError(msg)


class BITMAPINFOHEADER(ctypes.Structure):
    """Win32 BITMAPINFOHEADER structure."""

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BasePreviewCapture(abc.ABC):
    """Abstract screen capture interface for preview."""

    @abc.abstractmethod
    def grab(self) -> bytes | None:
        """Capture and return raw downscaled frame bytes (BGRA 32-bit)."""
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        """Release all allocated graphics and OS handles."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def dimensions(self) -> tuple[int, int]:
        """Return (width, height) of captured preview frame."""
        raise NotImplementedError


class GDIPreviewCapture(BasePreviewCapture):
    """High-performance Win32 GDI StretchBlt capture.

    Downscales the primary display directly during memory blit in DWM/GDI,
    avoiding massive full-resolution bitmap memory allocations and Python-side
    downscaling CPU costs.
    """

    def __init__(self, target_width: int = 480, target_height: int = 270) -> None:
        self._target_width = target_width
        self._target_height = target_height
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32

        ensure_thread_desktop_access()

        self._screen_w = self._user32.GetSystemMetrics(SM_CXSCREEN)
        self._screen_h = self._user32.GetSystemMetrics(SM_CYSCREEN)
        if self._screen_w <= 0 or self._screen_h <= 0:
            # Fallback to standard 1080p if metrics unavailable
            self._screen_w = 1920
            self._screen_h = 1080

        # Preserve aspect ratio within target bounding box
        aspect = self._screen_w / max(1, self._screen_h)
        target_aspect = self._target_width / max(1, self._target_height)
        if target_aspect > aspect:
            self._render_w = max(2, int(self._target_height * aspect))
            self._render_h = self._target_height
        else:
            self._render_w = self._target_width
            self._render_h = max(2, int(self._target_width / aspect))

        # Make dimensions even
        self._render_w &= ~1
        self._render_h &= ~1

        self._hdesktop: int | None = None
        self._hdc_screen: int | None = None
        self._hdc_mem: int | None = None
        self._hbitmap: int | None = None
        self._hold_bitmap: int | None = None
        self._buffer: ctypes.Array[ctypes.c_char] | None = None
        self._bmi: BITMAPINFOHEADER | None = None

        self._init_handles()

    def _init_handles(self) -> None:
        """Initialize GDI device contexts and compatible bitmap."""
        ensure_thread_desktop_access()
        self._hdesktop = self._user32.GetDesktopWindow()
        self._hdc_screen = self._user32.GetDC(self._hdesktop)
        self._hdc_mem = self._gdi32.CreateCompatibleDC(self._hdc_screen)
        self._hbitmap = self._gdi32.CreateCompatibleBitmap(
            self._hdc_screen, self._render_w, self._render_h
        )
        self._hold_bitmap = self._gdi32.SelectObject(self._hdc_mem, self._hbitmap)
        self._gdi32.SetStretchBltMode(self._hdc_mem, COLORONCOLOR)

        self._bmi = BITMAPINFOHEADER()
        self._bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self._bmi.biWidth = self._render_w
        self._bmi.biHeight = -self._render_h  # Negative for top-down DIB
        self._bmi.biPlanes = 1
        self._bmi.biBitCount = 32  # 32-bit BGRA
        self._bmi.biCompression = BI_RGB
        self._bmi.biSizeImage = self._render_w * self._render_h * 4
        self._buffer = ctypes.create_string_buffer(self._bmi.biSizeImage)

    @property
    def dimensions(self) -> tuple[int, int]:
        return (self._render_w, self._render_h)

    def grab(self) -> bytes | None:
        """Capture one downscaled frame using hardware-accelerated StretchBlt."""
        if not self._hdc_mem or not self._hdc_screen or not self._buffer or not self._bmi:
            return None

        ensure_thread_desktop_access()

        # StretchBlt downscales the screen directly into our small memory DC
        res = self._gdi32.StretchBlt(
            self._hdc_mem,
            0,
            0,
            self._render_w,
            self._render_h,
            self._hdc_screen,
            0,
            0,
            self._screen_w,
            self._screen_h,
            SRCCOPY,
        )
        if not res:
            logger.debug("StretchBlt returned 0 (LastError: %s)", ctypes.GetLastError())
            return None

        # Copy downscaled pixels to Python buffer
        scanlines = self._gdi32.GetDIBits(
            self._hdc_mem,
            self._hbitmap,
            0,
            self._render_h,
            self._buffer,
            ctypes.byref(self._bmi),
            DIB_RGB_COLORS,
        )
        if scanlines == 0:
            return None

        return self._buffer.raw

    def close(self) -> None:
        """Clean up Win32 GDI resources cleanly."""
        try:
            if self._hdc_mem and self._hold_bitmap:
                self._gdi32.SelectObject(self._hdc_mem, self._hold_bitmap)
            if self._hbitmap:
                self._gdi32.DeleteObject(self._hbitmap)
                self._hbitmap = None
            if self._hdc_mem:
                self._gdi32.DeleteDC(self._hdc_mem)
                self._hdc_mem = None
            if self._hdc_screen and self._hdesktop:
                self._user32.ReleaseDC(self._hdesktop, self._hdc_screen)
                self._hdc_screen = None
        except Exception as exc:
            logger.debug("Error during GDI cleanup: %s", exc)


PreviewListener = Callable[[PreviewFrame], None]


class PreviewController:
    """Thread-safe controller managing background desktop preview capture and rate policy."""

    def __init__(
        self,
        config: PreviewConfig | None = None,
        capture_factory: Callable[[], BasePreviewCapture] | None = None,
    ) -> None:
        self._config = config or PreviewConfig()
        self._capture_factory = capture_factory or (
            lambda: GDIPreviewCapture(self._config.target_width, self._config.target_height)
        )

        self._lock = threading.Lock()
        self._mode = PreviewMode.IDLE
        self._listeners: list[PreviewListener] = []

        self._stop_event = threading.Event()
        self._mode_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._frame_count = 0
        self._last_frame_time = 0.0
        self._current_fps = 0.0
        self._last_frame: PreviewFrame | None = None

    @property
    def mode(self) -> PreviewMode:
        """Current operating mode."""
        with self._lock:
            return self._mode

    @property
    def current_fps(self) -> float:
        """Instantaneous measured frame rate."""
        with self._lock:
            return self._current_fps

    @property
    def last_frame(self) -> PreviewFrame | None:
        """Most recent captured frame snapshot."""
        with self._lock:
            return self._last_frame

    def add_listener(self, callback: PreviewListener) -> None:
        """Register a frame subscriber callback."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: PreviewListener) -> None:
        """Unregister a frame subscriber callback."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def set_mode(self, mode: PreviewMode) -> None:
        """Change preview operating mode (IDLE, RECORDING, SUSPENDED, OFF)."""
        with self._lock:
            if self._mode == mode:
                return
            logger.info("Preview mode changed: %s -> %s", self._mode.value, mode.value)
            self._mode = mode
            self._mode_event.set()

    def suspend(self) -> None:
        """Suspend preview capture when the application window is minimized."""
        self.set_mode(PreviewMode.SUSPENDED)

    def resume(self, is_recording: bool = False) -> None:
        """Resume preview capture when the application window is restored."""
        target = PreviewMode.RECORDING if is_recording else PreviewMode.IDLE
        self.set_mode(target)

    def start(self) -> None:
        """Start the background preview capture worker thread."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._mode_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="PreviewCaptureWorker",
                daemon=True,
            )
            self._thread.start()
            logger.info("Preview capture worker thread started.")

    def stop(self, timeout_sec: float = 2.0) -> None:
        """Gracefully stop the preview capture worker thread."""
        self._stop_event.set()
        self._mode_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
        self._thread = None
        logger.info("Preview capture worker thread stopped.")

    def _worker_loop(self) -> None:
        """Background capture loop running at adaptive target frame rates."""
        capture: BasePreviewCapture | None = None
        try:
            capture = self._capture_factory()
        except Exception as exc:
            logger.error("Failed to initialize screen capture backend: %s", exc)
            return

        fps_calc_times: list[float] = []

        try:
            while not self._stop_event.is_set():
                with self._lock:
                    current_mode = self._mode

                if current_mode in (PreviewMode.OFF, PreviewMode.SUSPENDED):
                    # Zero workload: sleep on event until mode changes or stop requested
                    self._mode_event.wait(timeout=0.2)
                    self._mode_event.clear()
                    continue

                target_fps = (
                    self._config.recording_fps
                    if current_mode == PreviewMode.RECORDING
                    else self._config.idle_fps
                )
                interval = 1.0 / max(0.1, target_fps)

                t0 = time.perf_counter()
                frame_bytes: bytes | None = None
                try:
                    frame_bytes = capture.grab()
                except Exception as exc:
                    logger.debug("Frame grab exception: %s", exc)

                t1 = time.perf_counter()

                if frame_bytes:
                    fps_calc_times.append(t1)
                    # Keep rolling 1-second window for FPS calculation
                    cutoff = t1 - 1.0
                    fps_calc_times = [t for t in fps_calc_times if t >= cutoff]
                    measured_fps = float(len(fps_calc_times))

                    w, h = capture.dimensions
                    frame = PreviewFrame(
                        data=frame_bytes,
                        width=w,
                        height=h,
                        channels=4,
                        timestamp=t1,
                        frame_index=self._frame_count,
                        fps=measured_fps,
                    )

                    with self._lock:
                        self._frame_count += 1
                        self._last_frame = frame
                        self._current_fps = measured_fps
                        listeners = list(self._listeners)

                    # Notify subscribers outside the lock
                    for cb in listeners:
                        try:
                            cb(frame)
                        except Exception as exc:
                            logger.error("Error in preview frame listener: %s", exc)

                elapsed = time.perf_counter() - t0
                sleep_dur = interval - elapsed
                if sleep_dur > 0:
                    time.sleep(sleep_dur)

        finally:
            if capture:
                capture.close()
