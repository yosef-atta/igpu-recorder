"""Unit and integration tests for Phase 8 — Main UI (Tkinter MainWindow)."""

from __future__ import annotations

import contextlib
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from igpu_recorder.ffmpeg import FrameRate, HardwareBackend, Resolution
from igpu_recorder.preview import PreviewConfig, PreviewController, PreviewFrame, PreviewMode
from igpu_recorder.state_machine import (
    ApplicationState,
    ApplicationStateMachine,
    SettingsState,
    UIControlsState,
)
from igpu_recorder.ui import MainWindow, bgra_to_ppm


@pytest.fixture(scope="module")
def shared_tk_root():
    """Create a module-scoped Tk root instance to prevent Tcl reinitialization issues."""
    root = tk.Tk()
    root.withdraw()
    yield root
    with contextlib.suppress(Exception):
        root.destroy()


@pytest.fixture
def tk_root(shared_tk_root: tk.Tk):
    """Provide a cleaned Tk root for each test."""
    for child in list(shared_tk_root.winfo_children()):
        with contextlib.suppress(Exception):
            child.destroy()
    yield shared_tk_root


@pytest.fixture
def mock_state_machine(tmp_path: Path):
    """Create an ApplicationStateMachine with a dummy output dir."""
    out_dir = tmp_path / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = SettingsState(output_dir=out_dir)
    return ApplicationStateMachine(
        ffmpeg_path=Path("dummy_ffmpeg.exe"),
        ffprobe_path=Path("dummy_ffprobe.exe"),
        backend=HardwareBackend.QSV,
        initial_settings=settings,
    )


@pytest.fixture
def mock_preview_controller():
    """Create a mock PreviewController with fake capture."""
    fake_capture = MagicMock()
    fake_capture.dimensions = (480, 270)
    fake_capture.grab.return_value = b"\x00\x00\x00\xff" * (480 * 270)

    config = PreviewConfig(target_width=480, target_height=270)
    return PreviewController(config=config, capture_factory=lambda: fake_capture)


class TestBgraToPpmConverter:
    """Test BGRA raw buffer conversion to PPM P6 format."""

    def test_bgra_to_ppm_conversion(self) -> None:
        w, h = 2, 2
        bgra_data = bytes([
            10, 20, 30, 255,
            40, 50, 60, 255,
            70, 80, 90, 255,
            100, 110, 120, 255,
        ])
        ppm = bgra_to_ppm(bgra_data, w, h)
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        assert ppm.startswith(header)

        rgb_payload = ppm[len(header):]
        expected_rgb = bytes([
            30, 20, 10,
            60, 50, 40,
            90, 80, 70,
            120, 110, 100,
        ])
        assert rgb_payload == expected_rgb


class TestMainWindowLayoutAndWidgets:
    """Test widget creation and initial layout hierarchy."""

    def test_widgets_created_and_configured(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        assert app.root.title() == "iGPU Recorder"

        # Preview region
        assert hasattr(app, "_preview_canvas")
        assert int(app._preview_canvas.cget("width")) == 480
        assert int(app._preview_canvas.cget("height")) == 270

        # Selectors
        assert hasattr(app, "_res_720_radio")
        assert hasattr(app, "_res_1080_radio")
        assert hasattr(app, "_fps_30_radio")
        assert hasattr(app, "_fps_60_radio")
        assert hasattr(app, "_output_entry")
        assert hasattr(app, "_browse_button")

        # Action Buttons
        assert hasattr(app, "_primary_button")
        assert hasattr(app, "_stop_button")
        assert hasattr(app, "_status_label")

        # Initial Values
        assert app._res_var.get() == Resolution.R1080P.value
        assert app._fps_var.get() == FrameRate.FPS60.value
        assert app._primary_button.cget("text") == "Start Recording"
        assert str(app._primary_button.cget("state")) == "normal"
        assert str(app._stop_button.cget("state")) == "disabled"
        assert app._status_label.cget("text") == "Ready"


class TestStateWiringAndTransitions:
    """Test that application state changes drive UI controls deterministically."""

    def test_state_transitions_update_ui_elements(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        # 1. State: RECORDING
        rec_controls = UIControlsState(
            state=ApplicationState.RECORDING,
            primary_action_label="CUT",
            primary_action_enabled=True,
            stop_button_enabled=True,
            settings_locked=True,
            status_text="Recording...",
        )
        app._apply_ui_controls(ApplicationState.RECORDING, rec_controls)

        assert app._primary_button.cget("text") == "CUT"
        assert str(app._primary_button.cget("state")) == "normal"
        assert str(app._stop_button.cget("state")) == "normal"
        assert str(app._res_720_radio.cget("state")) == "disabled"
        assert str(app._res_1080_radio.cget("state")) == "disabled"
        assert str(app._fps_30_radio.cget("state")) == "disabled"
        assert str(app._fps_60_radio.cget("state")) == "disabled"
        assert str(app._output_entry.cget("state")) == "disabled"
        assert str(app._browse_button.cget("state")) == "disabled"
        assert app._status_label.cget("text") == "Recording..."
        assert mock_preview_controller.mode == PreviewMode.RECORDING

        # 2. State: PAUSED
        paused_controls = UIControlsState(
            state=ApplicationState.PAUSED,
            primary_action_label="Resume",
            primary_action_enabled=True,
            stop_button_enabled=True,
            settings_locked=True,
            status_text="Paused",
        )
        app._apply_ui_controls(ApplicationState.PAUSED, paused_controls)

        assert app._primary_button.cget("text") == "Resume"
        assert str(app._primary_button.cget("state")) == "normal"
        assert str(app._stop_button.cget("state")) == "normal"
        assert str(app._res_720_radio.cget("state")) == "disabled"
        assert app._status_label.cget("text") == "Paused"
        assert mock_preview_controller.mode == PreviewMode.IDLE

        # 3. State: FINALIZING
        fin_controls = UIControlsState(
            state=ApplicationState.FINALIZING,
            primary_action_label="Finalizing...",
            primary_action_enabled=False,
            stop_button_enabled=False,
            settings_locked=True,
            status_text="Finalizing MP4...",
        )
        app._apply_ui_controls(ApplicationState.FINALIZING, fin_controls)

        assert app._primary_button.cget("text") == "Finalizing..."
        assert str(app._primary_button.cget("state")) == "disabled"
        assert str(app._stop_button.cget("state")) == "disabled"
        assert str(app._res_720_radio.cget("state")) == "disabled"
        assert app._status_label.cget("text") == "Finalizing MP4..."

        # 4. State: ERROR
        err_controls = UIControlsState(
            state=ApplicationState.ERROR,
            primary_action_label="Reset",
            primary_action_enabled=True,
            stop_button_enabled=False,
            settings_locked=True,
            status_text="Error: Something failed",
        )
        app._apply_ui_controls(ApplicationState.ERROR, err_controls)

        assert app._primary_button.cget("text") == "Reset"
        assert str(app._primary_button.cget("state")) == "normal"
        assert str(app._stop_button.cget("state")) == "disabled"
        assert str(app._res_720_radio.cget("state")) == "disabled"
        assert app._status_label.cget("text") == "Error: Something failed"


class TestSettingsControlsInteraction:
    """Test user interaction with Resolution, FPS, and Output Directory settings."""

    def test_resolution_change_propagates_to_state_machine(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        app._res_var.set(Resolution.R720P.value)
        app._on_resolution_changed()
        assert mock_state_machine.settings.resolution == Resolution.R720P

        app._res_var.set(Resolution.R1080P.value)
        app._on_resolution_changed()
        assert mock_state_machine.settings.resolution == Resolution.R1080P

    def test_fps_change_propagates_to_state_machine(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        app._fps_var.set(FrameRate.FPS30.value)
        app._on_fps_changed()
        assert mock_state_machine.settings.fps == FrameRate.FPS30

        app._fps_var.set(FrameRate.FPS60.value)
        app._on_fps_changed()
        assert mock_state_machine.settings.fps == FrameRate.FPS60

    def test_output_folder_browse_dialog(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
        tmp_path: Path,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        target_dir = tmp_path / "custom_recordings"
        target_dir.mkdir()

        with patch("tkinter.filedialog.askdirectory", return_value=str(target_dir)):
            app._on_browse_output_dir()

        assert mock_state_machine.settings.output_dir == target_dir
        assert app._output_dir_var.get() == str(target_dir)

    def test_output_folder_entry_manual_change(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
        tmp_path: Path,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        target_dir = tmp_path / "manual_dir"
        target_dir.mkdir()

        app._output_dir_var.set(str(target_dir))
        app._on_output_entry_changed()

        assert mock_state_machine.settings.output_dir == target_dir


class TestMinimizationDetection:
    """Test preview suspension when window is minimized and resumed when restored."""

    def test_window_minimization_and_restore(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        event = MagicMock(spec=tk.Event)
        event.widget = tk_root

        # Mock wm_state to simulate minimize
        with patch.object(tk_root, "wm_state", return_value="iconic"):
            app._on_window_unmap(event)
            assert app._is_minimized is True
            assert mock_preview_controller.mode == PreviewMode.SUSPENDED

        # Mock wm_state to simulate restore
        with patch.object(tk_root, "wm_state", return_value="normal"):
            app._on_window_map(event)
            assert app._is_minimized is False
            assert mock_preview_controller.mode == PreviewMode.IDLE


class TestPreviewRendering:
    """Test desktop preview frame reception and canvas drawing."""

    def test_preview_frame_canvas_drawing(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        w, h = 480, 270
        frame_data = b"\x10\x20\x30\xff" * (w * h)
        frame = PreviewFrame(
            data=frame_data,
            width=w,
            height=h,
            channels=4,
            timestamp=1.0,
            frame_index=0,
            fps=10.0,
        )

        app._on_preview_frame_received(frame)
        assert app._pending_frame == frame

        # Force execution of idle draw callback
        app._draw_pending_preview_frame()

        assert app._last_frame_photo is not None
        assert app._last_frame_photo.width() == w
        assert app._last_frame_photo.height() == h
        assert app._preview_canvas_img_id is not None
        assert app._preview_placeholder_text_id is None


class TestAsynchronousOperationTriggers:
    """Test button clicks dispatching non-blocking background tasks."""

    def test_primary_button_start_dispatches_async(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        with patch.object(app, "_run_async") as mock_run_async:
            app._on_primary_action()
            mock_run_async.assert_called_once_with(app._execute_start)

    def test_primary_button_cut_dispatches_async(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        # Force state to RECORDING
        mock_state_machine._state = ApplicationState.RECORDING

        with patch.object(app, "_run_async") as mock_run_async:
            app._on_primary_action()
            mock_run_async.assert_called_once_with(app._execute_cut)

    def test_primary_button_resume_dispatches_async(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        # Force state to PAUSED
        mock_state_machine._state = ApplicationState.PAUSED

        with patch.object(app, "_run_async") as mock_run_async:
            app._on_primary_action()
            mock_run_async.assert_called_once_with(app._execute_resume)

    def test_stop_button_dispatches_async(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        # Force state to RECORDING
        mock_state_machine._state = ApplicationState.RECORDING

        with patch.object(app, "_run_async") as mock_run_async:
            app._on_stop_action()
            mock_run_async.assert_called_once_with(app._execute_stop)

    def test_window_close_cleans_up_resources(
        self,
        tk_root: tk.Tk,
        mock_state_machine: ApplicationStateMachine,
        mock_preview_controller: PreviewController,
    ) -> None:
        app = MainWindow(
            root=tk_root,
            state_machine=mock_state_machine,
            preview_controller=mock_preview_controller,
        )

        with patch.object(mock_preview_controller, "stop") as mock_stop_preview:
            app._on_window_close()
            assert app._is_closing is True
            mock_stop_preview.assert_called_once()
