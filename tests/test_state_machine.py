"""Unit tests for Phase 7 Application State Machine.

Verifies:
- Explicit state enum (IDLE, RECORDING, PAUSED, FINALIZING, ERROR).
- Valid transitions (Start, CUT, Resume, Stop, finalize success, reset error).
- Invalid transition rejections.
- Centralized state mutations & listeners.
- Settings lock outside IDLE.
- Derived UI controls in every state.
- Error recovery and error state transitions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from igpu_recorder.exceptions import (
    FinalizationError,
    InvalidConfigurationError,
    InvalidStateTransitionError,
    RecordingProcessError,
)
from igpu_recorder.ffmpeg import FrameRate, HardwareBackend, Resolution
from igpu_recorder.finalizer import FinalizationResult
from igpu_recorder.session import SegmentInfo
from igpu_recorder.state_machine import (
    VALID_TRANSITIONS,
    ApplicationState,
    ApplicationStateMachine,
    SettingsState,
)


@pytest.fixture
def dummy_settings(tmp_path: Path) -> SettingsState:
    return SettingsState(
        resolution=Resolution.R1080P,
        fps=FrameRate.FPS60,
        output_dir=tmp_path / "output",
        display_index=0,
        draw_mouse=True,
        global_quality=23,
    )


@pytest.fixture
def mock_session_factory(tmp_path: Path):
    def factory(*_: Any, **__: Any) -> MagicMock:
        mock = MagicMock()
        mock.session_id = "session_20260830_123456_deadbeef"
        mock.temp_dir = tmp_path / "session_temp"
        mock.output_target = tmp_path / "output" / "recording.mp4"
        mock.start.return_value = tmp_path / "session_temp" / "segment_000.mp4"
        mock.cut.return_value = SegmentInfo(
            index=0,
            path=tmp_path / "session_temp" / "segment_000.mp4",
            start_time=100.0,
            end_time=110.0,
            size_bytes=50000,
            is_valid=True,
        )
        mock.resume.return_value = tmp_path / "session_temp" / "segment_001.mp4"
        mock.stop.return_value = (
            SegmentInfo(
                index=0,
                path=tmp_path / "session_temp" / "segment_000.mp4",
                start_time=100.0,
                end_time=110.0,
                size_bytes=50000,
                is_valid=True,
            ),
        )
        return mock

    return factory


@pytest.fixture
def mock_finalizer_factory(tmp_path: Path):
    def factory(*_: Any, **__: Any) -> MagicMock:
        mock = MagicMock()
        mock.finalize_session.return_value = FinalizationResult(
            output_path=tmp_path / "output" / "iGPU-Recorder_test.mp4",
            duration=10.0,
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            codec="h264",
            num_segments=1,
            size_bytes=100000,
        )
        return mock

    return factory


class TestApplicationStateEnum:
    """Validate explicit 5 states exist and transition table is complete."""

    def test_state_enum_members(self) -> None:
        assert ApplicationState.IDLE.value == "IDLE"
        assert ApplicationState.RECORDING.value == "RECORDING"
        assert ApplicationState.PAUSED.value == "PAUSED"
        assert ApplicationState.FINALIZING.value == "FINALIZING"
        assert ApplicationState.ERROR.value == "ERROR"
        assert len(ApplicationState) == 5

    def test_valid_transition_table_keys(self) -> None:
        for state in ApplicationState:
            assert state in VALID_TRANSITIONS


class TestUIControlsStateDerivation:
    """Test derived UI control buttons and properties across states."""

    def test_idle_ui_state(self, dummy_settings: SettingsState) -> None:
        sm = ApplicationStateMachine(initial_settings=dummy_settings)
        controls = sm.derive_ui_controls()
        assert controls.state == ApplicationState.IDLE
        assert controls.primary_action_label == "Start Recording"
        assert controls.primary_action_enabled is True
        assert controls.stop_button_enabled is False
        assert controls.settings_locked is False
        assert controls.status_text == "Ready"

    def test_recording_ui_state(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
        mock_finalizer_factory: Any,
    ) -> None:
        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=mock_finalizer_factory,
        )
        sm.start()
        controls = sm.derive_ui_controls()
        assert controls.state == ApplicationState.RECORDING
        assert controls.primary_action_label == "CUT"
        assert controls.primary_action_enabled is True
        assert controls.stop_button_enabled is True
        assert controls.settings_locked is True
        assert controls.status_text == "Recording..."

    def test_paused_ui_state(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
        mock_finalizer_factory: Any,
    ) -> None:
        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=mock_finalizer_factory,
        )
        sm.start()
        sm.cut()
        controls = sm.derive_ui_controls()
        assert controls.state == ApplicationState.PAUSED
        assert controls.primary_action_label == "Resume"
        assert controls.primary_action_enabled is True
        assert controls.stop_button_enabled is True
        assert controls.settings_locked is True
        assert controls.status_text == "Paused"

    def test_finalizing_ui_state(self) -> None:
        sm = ApplicationStateMachine()
        # Manually force internal state for unit check of derive_ui_controls
        sm._state = ApplicationState.FINALIZING
        controls = sm.derive_ui_controls()
        assert controls.state == ApplicationState.FINALIZING
        assert controls.primary_action_enabled is False
        assert controls.stop_button_enabled is False
        assert controls.settings_locked is True
        assert controls.status_text == "Finalizing MP4..."

    def test_error_ui_state(self) -> None:
        sm = ApplicationStateMachine()
        sm.trigger_error("Disk full")
        controls = sm.derive_ui_controls()
        assert controls.state == ApplicationState.ERROR
        assert controls.primary_action_label == "Reset"
        assert controls.primary_action_enabled is True
        assert controls.stop_button_enabled is False
        assert controls.settings_locked is True
        assert "Error: Disk full" in controls.status_text


class TestSettingsLocking:
    """Ensure settings can only be mutated in IDLE state."""

    def test_settings_mutation_allowed_in_idle(self, dummy_settings: SettingsState) -> None:
        sm = ApplicationStateMachine(initial_settings=dummy_settings)
        new_settings = sm.update_settings(
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
        )
        assert new_settings.resolution == Resolution.R720P
        assert new_settings.fps == FrameRate.FPS30
        assert sm.settings.resolution == Resolution.R720P

    def test_settings_mutation_rejected_outside_idle(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
        mock_finalizer_factory: Any,
    ) -> None:
        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=mock_finalizer_factory,
        )
        sm.start()
        assert sm.state == ApplicationState.RECORDING

        with pytest.raises(InvalidConfigurationError, match="locked outside IDLE"):
            sm.update_settings(resolution=Resolution.R720P)

        sm.cut()
        assert sm.state == ApplicationState.PAUSED
        with pytest.raises(InvalidConfigurationError, match="locked outside IDLE"):
            sm.update_settings(fps=FrameRate.FPS30)


class TestStateTransitionsAndLifecycle:
    """Test full recording workflows and transition validations."""

    def test_start_cut_resume_stop_happy_path(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
        mock_finalizer_factory: Any,
    ) -> None:
        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=mock_finalizer_factory,
        )

        state_history: list[ApplicationState] = []
        sm.add_state_listener(lambda state, _: state_history.append(state))

        assert sm.state == ApplicationState.IDLE

        # 1. Start from IDLE
        seg0 = sm.start()
        assert seg0.name == "segment_000.mp4"
        assert sm.state == ApplicationState.RECORDING

        # 2. CUT from RECORDING
        info = sm.cut()
        assert info.index == 0
        assert sm.state == ApplicationState.PAUSED

        # 3. Resume from PAUSED
        seg1 = sm.resume()
        assert seg1.name == "segment_001.mp4"
        assert sm.state == ApplicationState.RECORDING

        # 4. Stop from RECORDING -> FINALIZING -> IDLE
        res = sm.stop()
        assert res.duration == 10.0
        assert sm.state == ApplicationState.IDLE
        assert sm.last_finalization_result is not None
        assert sm.active_session is None

        assert state_history == [
            ApplicationState.RECORDING,
            ApplicationState.PAUSED,
            ApplicationState.RECORDING,
            ApplicationState.FINALIZING,
            ApplicationState.IDLE,
        ]

    def test_stop_from_paused(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
        mock_finalizer_factory: Any,
    ) -> None:
        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=mock_finalizer_factory,
        )
        sm.start()
        sm.cut()
        assert sm.state == ApplicationState.PAUSED

        res = sm.stop()
        assert res.size_bytes == 100000
        assert sm.state == ApplicationState.IDLE

    def test_invalid_transitions_rejected(self, dummy_settings: SettingsState) -> None:
        sm = ApplicationStateMachine(initial_settings=dummy_settings)
        assert sm.state == ApplicationState.IDLE

        # Cannot CUT, Resume, Stop, or Reset Error from IDLE
        with pytest.raises(InvalidStateTransitionError):
            sm.cut()
        with pytest.raises(InvalidStateTransitionError):
            sm.resume()
        with pytest.raises(InvalidStateTransitionError):
            sm.stop()
        with pytest.raises(InvalidStateTransitionError):
            sm.reset_error()


class TestErrorHandlingAndRecovery:
    """Test recoverable errors and transition to IDLE."""

    def test_start_failure_transitions_to_error(
        self,
        dummy_settings: SettingsState,
    ) -> None:
        def failing_session_factory(*_: Any, **__: Any) -> MagicMock:
            mock = MagicMock()
            mock.start.side_effect = RecordingProcessError("Failed to launch FFmpeg")
            return mock

        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=failing_session_factory,
        )

        with pytest.raises(RecordingProcessError):
            sm.start()

        assert sm.state == ApplicationState.ERROR
        assert "Failed to launch FFmpeg" in str(sm.last_error)

        # Recover to IDLE
        sm.reset_error()
        assert sm.state == ApplicationState.IDLE
        assert sm.last_error is None

    def test_cut_failure_transitions_to_error(
        self,
        dummy_settings: SettingsState,
    ) -> None:
        def failing_cut_session_factory(*_: Any, **__: Any) -> MagicMock:
            mock = MagicMock()
            mock.start.return_value = Path("segment_000.mp4")
            mock.cut.side_effect = RecordingProcessError("Segment corrupted")
            mock.temp_dir = Path("F:/temp/recovery")
            return mock

        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=failing_cut_session_factory,
        )
        sm.start()

        with pytest.raises(RecordingProcessError):
            sm.cut()

        assert sm.state == ApplicationState.ERROR
        assert "Segment corrupted" in str(sm.last_error)
        assert sm.last_error_recovery_path == Path("F:/temp/recovery")

        sm.reset_error()
        assert sm.state == ApplicationState.IDLE

    def test_finalizer_failure_preserves_recovery_and_transitions_to_error(
        self,
        dummy_settings: SettingsState,
        mock_session_factory: Any,
    ) -> None:
        def failing_finalizer_factory(*_: Any, **__: Any) -> MagicMock:
            mock = MagicMock()
            mock.finalize_session.side_effect = FinalizationError("Stream copy error")
            return mock

        sm = ApplicationStateMachine(
            ffmpeg_path="ffmpeg.exe",
            ffprobe_path="ffprobe.exe",
            backend=HardwareBackend.QSV,
            initial_settings=dummy_settings,
            session_factory=mock_session_factory,
            finalizer_factory=failing_finalizer_factory,
        )
        sm.start()

        with pytest.raises(FinalizationError):
            sm.stop()

        assert sm.state == ApplicationState.ERROR
        assert "Stream copy error" in str(sm.last_error)
        assert sm.last_error_recovery_path is not None

        # Reset from error
        sm.reset_error()
        assert sm.state == ApplicationState.IDLE
