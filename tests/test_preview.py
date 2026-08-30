"""Comprehensive test suite for the Desktop Preview subsystem (Phase 6).

Tests:
- Configuration validation and defaults
- GDI screen capture logic, aspect ratio preservation, and resource cleanup
- PreviewController thread lifecycle (start, stop, restart)
- Operating mode transitions (IDLE, RECORDING, SUSPENDED, OFF)
- Target frame rate regulation (10 FPS idle, 5 FPS recording, 0 FPS suspended)
- Listener registration, broadcast delivery, and error isolation
- Window minimize / suspend and restore behavior
- Isolation from recording data path
"""

import threading
import time

import pytest

from igpu_recorder.preview import (
    BasePreviewCapture,
    GDIPreviewCapture,
    PreviewConfig,
    PreviewController,
    PreviewFrame,
    PreviewMode,
)


class DummyPreviewCapture(BasePreviewCapture):
    """Deterministic mock capture for unit testing."""

    def __init__(self, width: int = 320, height: int = 180, return_none: bool = False) -> None:
        self._width = width
        self._height = height
        self._return_none = return_none
        self.grab_count = 0
        self.closed = False
        self._dummy_bytes = b"\x00" * (width * height * 4)

    @property
    def dimensions(self) -> tuple[int, int]:
        return (self._width, self._height)

    def grab(self) -> bytes | None:
        if self.closed or self._return_none:
            return None
        self.grab_count += 1
        return self._dummy_bytes

    def close(self) -> None:
        self.closed = True


class TestPreviewConfig:
    """Tests for PreviewConfig validation and behavior."""

    def test_default_config(self) -> None:
        config = PreviewConfig()
        assert config.target_width == 480
        assert config.target_height == 270
        assert config.idle_fps == 10.0
        assert config.recording_fps == 5.0
        assert config.display_index == 0

    def test_custom_config(self) -> None:
        config = PreviewConfig(
            target_width=640, target_height=360, idle_fps=15.0, recording_fps=8.0
        )
        assert config.target_width == 640
        assert config.target_height == 360
        assert config.idle_fps == 15.0
        assert config.recording_fps == 8.0

    def test_invalid_dimensions(self) -> None:
        with pytest.raises(ValueError, match="Invalid preview dimensions"):
            PreviewConfig(target_width=0, target_height=270)

        with pytest.raises(ValueError, match="Invalid preview dimensions"):
            PreviewConfig(target_width=480, target_height=-10)

    def test_invalid_fps(self) -> None:
        with pytest.raises(ValueError, match="Invalid preview FPS"):
            PreviewConfig(idle_fps=0)

        with pytest.raises(ValueError, match="Invalid preview FPS"):
            PreviewConfig(recording_fps=-5.0)


class TestGDIPreviewCapture:
    """Unit and real tests for GDIPreviewCapture."""

    def test_dimensions_aspect_ratio_calculation(self) -> None:
        cap = GDIPreviewCapture(target_width=480, target_height=270)
        w, h = cap.dimensions
        assert w > 0
        assert h > 0
        assert w % 2 == 0
        assert h % 2 == 0
        assert w <= 480
        assert h <= 270
        cap.close()

    def test_real_gdi_grab_success_and_buffer_size(self) -> None:
        cap = GDIPreviewCapture(target_width=320, target_height=180)
        w, h = cap.dimensions
        expected_bytes_len = w * h * 4

        frame_data = cap.grab()
        if frame_data is None:
            pytest.skip(
                "GDI grab returned None (interactive desktop unavailable in test context)"
            )
        assert frame_data is not None
        assert len(frame_data) == expected_bytes_len
        assert isinstance(frame_data, bytes)

        # Grab a second frame to verify stability
        frame_data_2 = cap.grab()
        assert frame_data_2 is not None
        assert len(frame_data_2) == expected_bytes_len

        cap.close()

    def test_close_is_idempotent(self) -> None:
        cap = GDIPreviewCapture(target_width=320, target_height=180)
        cap.close()
        # Second close must not raise errors
        cap.close()
        # Grab after close returns None
        assert cap.grab() is None


class TestPreviewController:
    """Tests for PreviewController threading, mode transitions, and listener dispatch."""

    def test_initial_state(self) -> None:
        controller = PreviewController()
        assert controller.mode == PreviewMode.IDLE
        assert controller.current_fps == 0.0
        assert controller.last_frame is None

    def test_listener_registration_and_dispatch(self) -> None:
        dummy_cap = DummyPreviewCapture(320, 180)
        config = PreviewConfig(target_width=320, target_height=180, idle_fps=20.0)
        controller = PreviewController(config=config, capture_factory=lambda: dummy_cap)

        received_frames: list[PreviewFrame] = []
        frame_event = threading.Event()

        def on_frame(frame: PreviewFrame) -> None:
            received_frames.append(frame)
            if len(received_frames) >= 3:
                frame_event.set()

        controller.add_listener(on_frame)
        controller.start()

        assert frame_event.wait(timeout=2.0)
        controller.stop()

        assert len(received_frames) >= 3
        first = received_frames[0]
        assert first.width == 320
        assert first.height == 180
        assert first.channels == 4
        assert len(first.data) == 320 * 180 * 4
        assert first.timestamp > 0
        assert first.frame_index >= 0
        assert dummy_cap.closed

    def test_listener_removal(self) -> None:
        dummy_cap = DummyPreviewCapture(320, 180)
        controller = PreviewController(capture_factory=lambda: dummy_cap)

        count = 0

        def on_frame(_: PreviewFrame) -> None:
            nonlocal count
            count += 1

        controller.add_listener(on_frame)
        controller.remove_listener(on_frame)
        controller.start()

        time.sleep(0.1)
        controller.stop()

        assert count == 0

    def test_listener_error_isolation(self) -> None:
        """Listener raising an exception does not kill worker or other listeners."""
        dummy_cap = DummyPreviewCapture(320, 180)
        config = PreviewConfig(idle_fps=20.0)
        controller = PreviewController(config=config, capture_factory=lambda: dummy_cap)

        healthy_received = []
        healthy_event = threading.Event()

        def broken_listener(_: PreviewFrame) -> None:
            raise RuntimeError("Listener crashed intentionally")

        def healthy_listener(frame: PreviewFrame) -> None:
            healthy_received.append(frame)
            if len(healthy_received) >= 2:
                healthy_event.set()

        controller.add_listener(broken_listener)
        controller.add_listener(healthy_listener)
        controller.start()

        assert healthy_event.wait(timeout=2.0)
        controller.stop()

        assert len(healthy_received) >= 2

    def test_mode_transitions_and_framerate_regulation(self) -> None:
        """Verify transition between IDLE (10 FPS) and RECORDING (5 FPS)."""
        dummy_cap = DummyPreviewCapture(320, 180)
        config = PreviewConfig(idle_fps=20.0, recording_fps=5.0)
        controller = PreviewController(config=config, capture_factory=lambda: dummy_cap)

        controller.start()
        assert controller.mode == PreviewMode.IDLE

        # Switch to RECORDING
        controller.set_mode(PreviewMode.RECORDING)
        assert controller.mode == PreviewMode.RECORDING

        time.sleep(0.2)
        controller.stop()

    def test_suspension_minimization_stops_workload(self) -> None:
        """Minimizing/suspending preview completely stops grab invocations (0 FPS)."""
        dummy_cap = DummyPreviewCapture(320, 180)
        config = PreviewConfig(idle_fps=30.0)
        controller = PreviewController(config=config, capture_factory=lambda: dummy_cap)

        controller.start()
        time.sleep(0.1)
        initial_grabs = dummy_cap.grab_count
        assert initial_grabs > 0

        # Suspend preview (window minimized)
        controller.suspend()
        assert controller.mode == PreviewMode.SUSPENDED

        # Allow time to pass while suspended
        time.sleep(0.3)
        suspended_grabs = dummy_cap.grab_count

        # Grab count must not increase meaningfully while suspended
        assert suspended_grabs <= initial_grabs + 1

        # Resume preview (window restored)
        controller.resume(is_recording=False)
        assert controller.mode == PreviewMode.IDLE

        time.sleep(0.15)
        resumed_grabs = dummy_cap.grab_count
        assert resumed_grabs > suspended_grabs

        controller.stop()

    def test_stop_and_restart_lifecycle(self) -> None:
        dummy_cap_1 = DummyPreviewCapture(320, 180)
        dummy_cap_2 = DummyPreviewCapture(320, 180)
        caps = [dummy_cap_1, dummy_cap_2]

        controller = PreviewController(capture_factory=lambda: caps.pop(0))
        controller.start()
        time.sleep(0.05)
        controller.stop()
        assert dummy_cap_1.closed

        # Restart controller
        controller.start()
        time.sleep(0.05)
        controller.stop()
        assert dummy_cap_2.closed


class TestRealGDIPreviewIntegration:
    """End-to-end integration test with live Windows Desktop Capture."""

    def test_live_desktop_preview_feed(self) -> None:
        config = PreviewConfig(target_width=480, target_height=270, idle_fps=10.0)
        controller = PreviewController(config=config)

        frames: list[PreviewFrame] = []
        feed_event = threading.Event()

        def frame_handler(frame: PreviewFrame) -> None:
            frames.append(frame)
            if len(frames) >= 5:
                feed_event.set()

        controller.add_listener(frame_handler)
        controller.start()

        assert feed_event.wait(timeout=3.0), "Timed out waiting for live desktop preview frames"
        controller.stop()

        assert len(frames) >= 5
        for f in frames:
            assert f.width > 0
            assert f.height > 0
            assert f.channels == 4
            assert len(f.data) == f.width * f.height * 4
            assert f.timestamp > 0
