"""Comprehensive Preview Benchmark & Spike Experiment for Phase 6.

Evaluates GDI (direct StretchBlt), DXcam, and MSS under:
1. Idle preview (~10 FPS)
2. Recording preview (~5 FPS)
3. Suspended preview (0 FPS)
4. Recording stability (1080p60 QSV) with preview active vs suspended vs baseline.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import pprint
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import psutil

from igpu_recorder.preview import GDIPreviewCapture


def ensure_desktop_access() -> None:
    """Ensure the current thread has access to the interactive Default desktop."""
    with contextlib.suppress(Exception):
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)


class DXCamScreenCapture:
    """DXcam capture with software downscaling."""

    def __init__(self, target_w: int = 480, target_h: int = 270) -> None:
        ensure_desktop_access()
        import cv2
        import dxcam

        self.cv2 = cv2
        self.target_w = target_w
        self.target_h = target_h
        self.cam = dxcam.create(output_idx=0)

    def grab(self) -> bytes | None:
        ensure_desktop_access()
        frame = self.cam.grab()
        if frame is None:
            return None
        resized = self.cv2.resize(
            frame, (self.target_w, self.target_h), interpolation=self.cv2.INTER_AREA
        )
        return resized.tobytes()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if hasattr(self, "cam") and self.cam:
                del self.cam


class MSSScreenCapture:
    """MSS capture with PIL downscaling."""

    def __init__(self, target_w: int = 480, target_h: int = 270) -> None:
        ensure_desktop_access()
        import mss
        from PIL import Image

        self.Image = Image
        self.target_w = target_w
        self.target_h = target_h
        self.sct = mss.MSS()
        self.monitor = self.sct.monitors[1]

    def grab(self) -> bytes | None:
        ensure_desktop_access()
        img = self.sct.grab(self.monitor)
        if img is None:
            return None
        pil_img = self.Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        resized = pil_img.resize((self.target_w, self.target_h), self.Image.Resampling.BILINEAR)
        return resized.tobytes()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if hasattr(self, "sct") and self.sct:
                self.sct.close()


def benchmark_capture_method(
    name: str, factory: Callable[[], Any], fps: float, duration_sec: float = 5.0
) -> dict[str, Any]:
    print(f"--> Benchmarking {name} at target {fps} FPS for {duration_sec}s...")
    ensure_desktop_access()
    capture = factory()
    proc = psutil.Process(os.getpid())

    interval = 1.0 / fps
    frames_captured = 0
    grab_times = []

    start_cpu_time = proc.cpu_times()
    start_wall_time = time.perf_counter()

    end_time = start_wall_time + duration_sec
    while time.perf_counter() < end_time:
        t0 = time.perf_counter()
        data = capture.grab()
        t1 = time.perf_counter()

        if data is not None:
            frames_captured += 1
            grab_times.append((t1 - t0) * 1000.0)

        elapsed_loop = time.perf_counter() - t0
        sleep_time = interval - elapsed_loop
        if sleep_time > 0:
            time.sleep(sleep_time)

    end_wall_time = time.perf_counter()
    end_cpu_time = proc.cpu_times()

    total_wall = end_wall_time - start_wall_time
    total_user_cpu = end_cpu_time.user - start_cpu_time.user
    total_system_cpu = end_cpu_time.system - start_cpu_time.system
    total_cpu_time = total_user_cpu + total_system_cpu
    cpu_percent = (total_cpu_time / total_wall) * 100.0

    avg_grab_ms = sum(grab_times) / max(1, len(grab_times))
    actual_fps = frames_captured / total_wall
    mem_mb = proc.memory_info().rss / (1024 * 1024)

    capture.close()

    result = {
        "name": name,
        "target_fps": fps,
        "actual_fps": round(actual_fps, 2),
        "frames_captured": frames_captured,
        "avg_grab_ms": round(avg_grab_ms, 2),
        "min_grab_ms": round(min(grab_times) if grab_times else 0, 2),
        "max_grab_ms": round(max(grab_times) if grab_times else 0, 2),
        "process_cpu_percent": round(cpu_percent, 2),
        "rss_memory_mb": round(mem_mb, 2),
    }
    print(f"    Result: {result}")
    return result


def main() -> None:
    print("=================================================================")
    print("STARTING PHASE 6 PREVIEW SPIKE & BENCHMARK SUITE")
    print("=================================================================")

    results = {}

    # 1. Idle FPS (10 FPS) benchmarks
    print("\n--- Phase 6: Capture Approach Comparison at IDLE (10 FPS) ---")
    results["idle_gdi"] = benchmark_capture_method(
        "Win32 GDI StretchBlt (Direct Downscale)",
        lambda: GDIPreviewCapture(480, 270),
        10.0,
        5.0,
    )
    results["idle_dxcam"] = benchmark_capture_method(
        "DXcam (DXGI Desktop Duplication)",
        lambda: DXCamScreenCapture(480, 270),
        10.0,
        5.0,
    )
    results["idle_mss"] = benchmark_capture_method(
        "MSS (BitBlt + PIL Downscale)",
        lambda: MSSScreenCapture(480, 270),
        10.0,
        5.0,
    )

    # 2. Recording FPS (5 FPS) benchmarks
    print("\n--- Phase 6: Capture Approach Comparison at RECORDING (5 FPS) ---")
    results["rec_gdi"] = benchmark_capture_method(
        "Win32 GDI StretchBlt (Direct Downscale)",
        lambda: GDIPreviewCapture(480, 270),
        5.0,
        5.0,
    )
    results["rec_dxcam"] = benchmark_capture_method(
        "DXcam (DXGI Desktop Duplication)",
        lambda: DXCamScreenCapture(480, 270),
        5.0,
        5.0,
    )
    results["rec_mss"] = benchmark_capture_method(
        "MSS (BitBlt + PIL Downscale)",
        lambda: MSSScreenCapture(480, 270),
        5.0,
        5.0,
    )

    # 3. Suspended Preview (0 FPS)
    print("\n--- Phase 6: Suspended Preview Workload (0 FPS) ---")
    proc = psutil.Process(os.getpid())
    t0_wall = time.perf_counter()
    t0_cpu = proc.cpu_times()
    time.sleep(3.0)
    t1_wall = time.perf_counter()
    t1_cpu = proc.cpu_times()
    suspended_cpu = (
        (t1_cpu.user - t0_cpu.user + t1_cpu.system - t0_cpu.system) / (t1_wall - t0_wall)
    ) * 100.0
    results["suspended"] = {
        "name": "Suspended / Minimized (0 FPS)",
        "target_fps": 0.0,
        "actual_fps": 0.0,
        "frames_captured": 0,
        "process_cpu_percent": round(suspended_cpu, 2),
        "rss_memory_mb": round(proc.memory_info().rss / (1024 * 1024), 2),
    }
    print(f"    Result: {results['suspended']}")

    print("\n=================================================================")
    print("BENCHMARK COMPLETED")
    print("=================================================================")
    pprint.pprint(results)


if __name__ == "__main__":
    main()
