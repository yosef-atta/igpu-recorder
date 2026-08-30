"""Application state machine orchestrating UI state, settings locks, and recording lifecycle.

Provides:
- ApplicationState: Explicit 5-state enum (IDLE, RECORDING, PAUSED, FINALIZING, ERROR).
- SettingsState: Immutable dataclass capturing resolution, fps, output folder, display index.
- UIControlsState: Derived UI control availability and labels.
- ApplicationStateMachine: Centralized state coordinator ensuring invalid transitions are rejected.
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from igpu_recorder.exceptions import (
    FinalizationError,
    InvalidConfigurationError,
    InvalidStateTransitionError,
    RecordingProcessError,
)
from igpu_recorder.ffmpeg import FrameRate, HardwareBackend, Resolution, probe_capabilities
from igpu_recorder.finalizer import FinalizationResult, Finalizer
from igpu_recorder.logging import get_logger
from igpu_recorder.session import RecordingSession

if TYPE_CHECKING:
    from igpu_recorder.session import SegmentInfo

logger = get_logger("state_machine")


class ApplicationState(enum.Enum):
    """Explicit application lifecycle states."""

    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PAUSED = "PAUSED"
    FINALIZING = "FINALIZING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SettingsState:
    """User-configurable settings state."""

    resolution: Resolution = Resolution.R1080P
    fps: FrameRate = FrameRate.FPS60
    output_dir: Path = Path.home() / "Videos"
    display_index: int = 0
    draw_mouse: bool = True
    global_quality: int = 23


@dataclass(frozen=True)
class UIControlsState:
    """Derived UI control properties for the current state."""

    state: ApplicationState
    primary_action_label: str
    primary_action_enabled: bool
    stop_button_enabled: bool
    settings_locked: bool
    status_text: str


# Valid transitions map: from_state -> set of allowed target_states
VALID_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.IDLE: frozenset({ApplicationState.RECORDING, ApplicationState.ERROR}),
    ApplicationState.RECORDING: frozenset(
        {ApplicationState.PAUSED, ApplicationState.FINALIZING, ApplicationState.ERROR}
    ),
    ApplicationState.PAUSED: frozenset(
        {ApplicationState.RECORDING, ApplicationState.FINALIZING, ApplicationState.ERROR}
    ),
    ApplicationState.FINALIZING: frozenset({ApplicationState.IDLE, ApplicationState.ERROR}),
    ApplicationState.ERROR: frozenset({ApplicationState.IDLE}),
}


class ApplicationStateMachine:
    """Centralized state machine coordinating recorder operations and UI constraints.

    Ensures invalid transitions are rejected, mutations are thread-safe and centralized,
    settings are locked outside IDLE, and button states are derived deterministically.
    """

    def __init__(
        self,
        ffmpeg_path: Path | str | None = None,
        ffprobe_path: Path | str | None = None,
        backend: HardwareBackend | None = None,
        initial_settings: SettingsState | None = None,
        session_factory: Callable[..., RecordingSession] | None = None,
        finalizer_factory: Callable[..., Finalizer] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._state = ApplicationState.IDLE
        self._settings = initial_settings or SettingsState()
        self._ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else None
        self._ffprobe_path = Path(ffprobe_path) if ffprobe_path else None
        self._backend = backend
        self._session_factory = session_factory or RecordingSession
        self._finalizer_factory = finalizer_factory or Finalizer

        self._active_session: RecordingSession | None = None
        self._last_finalization_result: FinalizationResult | None = None
        self._last_error: str | None = None
        self._last_error_recovery_path: Path | None = None
        self._state_listeners: list[Callable[[ApplicationState, UIControlsState], None]] = []

        logger.info("ApplicationStateMachine initialized in state IDLE.")

    @property
    def state(self) -> ApplicationState:
        with self._lock:
            return self._state

    @property
    def settings(self) -> SettingsState:
        with self._lock:
            return self._settings

    @property
    def active_session(self) -> RecordingSession | None:
        with self._lock:
            return self._active_session

    @property
    def last_finalization_result(self) -> FinalizationResult | None:
        with self._lock:
            return self._last_finalization_result

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def last_error_recovery_path(self) -> Path | None:
        with self._lock:
            return self._last_error_recovery_path

    def add_state_listener(
        self, listener: Callable[[ApplicationState, UIControlsState], None]
    ) -> None:
        """Register a callback invoked whenever application state transitions."""
        with self._lock:
            if listener not in self._state_listeners:
                self._state_listeners.append(listener)

    def remove_state_listener(
        self, listener: Callable[[ApplicationState, UIControlsState], None]
    ) -> None:
        """Unregister a state listener callback."""
        with self._lock:
            if listener in self._state_listeners:
                self._state_listeners.remove(listener)

    def _notify_listeners(self) -> None:
        """Notify listeners of state and UI controls change."""
        current_state = self._state
        controls = self.derive_ui_controls()
        for listener in list(self._state_listeners):
            try:
                listener(current_state, controls)
            except Exception as exc:
                logger.warning("Error in state listener: %s", exc)

    def _transition_to(self, new_state: ApplicationState) -> None:
        """Internal transition validator and updater under lock."""
        if new_state not in VALID_TRANSITIONS.get(self._state, frozenset()):
            msg = f"Invalid state transition from {self._state.value} to {new_state.value}."
            logger.error(msg)
            raise InvalidStateTransitionError(msg)

        old_state = self._state
        self._state = new_state
        logger.info("State transition: %s -> %s", old_state.value, new_state.value)
        self._notify_listeners()

    def update_settings(
        self,
        resolution: Resolution | None = None,
        fps: FrameRate | None = None,
        output_dir: Path | str | None = None,
        display_index: int | None = None,
        draw_mouse: bool | None = None,
        global_quality: int | None = None,
    ) -> SettingsState:
        """Update configurable settings. Rejected if not in IDLE state."""
        with self._lock:
            if self._state != ApplicationState.IDLE:
                raise InvalidConfigurationError(
                    f"Cannot modify settings while in {self._state.value} state. "
                    "Settings are locked outside IDLE."
                )

            new_res = resolution if resolution is not None else self._settings.resolution
            new_fps = fps if fps is not None else self._settings.fps
            new_out = Path(output_dir) if output_dir is not None else self._settings.output_dir
            new_disp = display_index if display_index is not None else self._settings.display_index
            new_mouse = draw_mouse if draw_mouse is not None else self._settings.draw_mouse
            new_qual = global_quality if global_quality is not None else self._settings.global_quality

            self._settings = SettingsState(
                resolution=new_res,
                fps=new_fps,
                output_dir=new_out,
                display_index=new_disp,
                draw_mouse=new_mouse,
                global_quality=new_qual,
            )
            logger.info("Updated settings: %s", self._settings)
            self._notify_listeners()
            return self._settings

    def derive_ui_controls(self) -> UIControlsState:
        """Derive all UI button and input control states from current application state."""
        with self._lock:
            match self._state:
                case ApplicationState.IDLE:
                    return UIControlsState(
                        state=ApplicationState.IDLE,
                        primary_action_label="Start Recording",
                        primary_action_enabled=True,
                        stop_button_enabled=False,
                        settings_locked=False,
                        status_text="Ready",
                    )
                case ApplicationState.RECORDING:
                    return UIControlsState(
                        state=ApplicationState.RECORDING,
                        primary_action_label="CUT",
                        primary_action_enabled=True,
                        stop_button_enabled=True,
                        settings_locked=True,
                        status_text="Recording...",
                    )
                case ApplicationState.PAUSED:
                    return UIControlsState(
                        state=ApplicationState.PAUSED,
                        primary_action_label="Resume",
                        primary_action_enabled=True,
                        stop_button_enabled=True,
                        settings_locked=True,
                        status_text="Paused",
                    )
                case ApplicationState.FINALIZING:
                    return UIControlsState(
                        state=ApplicationState.FINALIZING,
                        primary_action_label="Finalizing...",
                        primary_action_enabled=False,
                        stop_button_enabled=False,
                        settings_locked=True,
                        status_text="Finalizing MP4...",
                    )
                case ApplicationState.ERROR:
                    err_msg = self._last_error or "An error occurred"
                    return UIControlsState(
                        state=ApplicationState.ERROR,
                        primary_action_label="Reset",
                        primary_action_enabled=True,
                        stop_button_enabled=False,
                        settings_locked=True,
                        status_text=f"Error: {err_msg}",
                    )

    def _ensure_pipeline_ready(self) -> tuple[Path, Path, HardwareBackend]:
        """Validate output directory, binaries, and backend before starting."""
        out_dir = self._settings.output_dir.resolve()
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InvalidConfigurationError(f"Cannot create output directory {out_dir}: {exc}") from exc

        if not out_dir.is_dir():
            raise InvalidConfigurationError(f"Output path is not a directory: {out_dir}")

        if not self._ffmpeg_path or not self._ffprobe_path or not self._backend:
            caps = probe_capabilities()
            if not caps.is_recording_supported:
                raise RecordingProcessError(
                    "System is not ready for hardware recording: missing ddagrab or supported backend."
                )
            self._ffmpeg_path = caps.ffmpeg_path
            self._ffprobe_path = caps.ffprobe_path
            self._backend = caps.primary_backend

        if self._ffmpeg_path is None or self._ffprobe_path is None or self._backend is None:
            raise RecordingProcessError("FFmpeg or hardware encoder backend unavailable.")

        return self._ffmpeg_path, self._ffprobe_path, self._backend

    def start(self) -> Path:
        """Start a new recording session from IDLE.

        Transitions: IDLE -> RECORDING (or IDLE -> ERROR on startup failure).
        """
        with self._lock:
            if self._state != ApplicationState.IDLE:
                raise InvalidStateTransitionError(
                    f"Cannot start recording from state {self._state.value}. Expected IDLE."
                )

            try:
                ffmpeg_p, _, backend = self._ensure_pipeline_ready()
                # Target filename in output dir
                target_file = self._settings.output_dir / "recording.mp4"

                session = self._session_factory(
                    ffmpeg_path=ffmpeg_p,
                    resolution=self._settings.resolution,
                    fps=self._settings.fps,
                    backend=backend,
                    output_target=target_file,
                    display_index=self._settings.display_index,
                    draw_mouse=self._settings.draw_mouse,
                    global_quality=self._settings.global_quality,
                )

                first_segment_path = session.start()
                self._active_session = session
                self._last_error = None
                self._last_error_recovery_path = None
                self._transition_to(ApplicationState.RECORDING)
                return first_segment_path

            except Exception as exc:
                logger.error("Failed to start recording: %s", exc)
                self._last_error = str(exc)
                self._transition_to(ApplicationState.ERROR)
                raise

    def cut(self) -> SegmentInfo:
        """Perform CUT on the active recording.

        Transitions: RECORDING -> PAUSED (or RECORDING -> ERROR on failure).
        """
        with self._lock:
            if self._state != ApplicationState.RECORDING:
                raise InvalidStateTransitionError(
                    f"Cannot execute CUT from state {self._state.value}. Expected RECORDING."
                )

            if not self._active_session:
                self._last_error = "No active recording session."
                self._transition_to(ApplicationState.ERROR)
                raise RecordingProcessError("No active recording session.")

            try:
                segment_info = self._active_session.cut()
                self._transition_to(ApplicationState.PAUSED)
                return segment_info
            except Exception as exc:
                logger.error("CUT failed: %s", exc)
                self._last_error = str(exc)
                if self._active_session:
                    self._last_error_recovery_path = self._active_session.temp_dir
                self._transition_to(ApplicationState.ERROR)
                raise

    def resume(self) -> Path:
        """Resume recording after CUT.

        Transitions: PAUSED -> RECORDING (or PAUSED -> ERROR on failure).
        """
        with self._lock:
            if self._state != ApplicationState.PAUSED:
                raise InvalidStateTransitionError(
                    f"Cannot resume recording from state {self._state.value}. Expected PAUSED."
                )

            if not self._active_session:
                self._last_error = "No active recording session."
                self._transition_to(ApplicationState.ERROR)
                raise RecordingProcessError("No active recording session.")

            try:
                next_segment_path = self._active_session.resume()
                self._transition_to(ApplicationState.RECORDING)
                return next_segment_path
            except Exception as exc:
                logger.error("Resume failed: %s", exc)
                self._last_error = str(exc)
                if self._active_session:
                    self._last_error_recovery_path = self._active_session.temp_dir
                self._transition_to(ApplicationState.ERROR)
                raise

    def stop(self, custom_output_name: str | None = None) -> FinalizationResult:
        """Stop recording from RECORDING or PAUSED and finalize segments.

        Transitions: RECORDING/PAUSED -> FINALIZING -> IDLE (or FINALIZING -> ERROR on failure).
        """
        with self._lock:
            if self._state not in (ApplicationState.RECORDING, ApplicationState.PAUSED):
                raise InvalidStateTransitionError(
                    f"Cannot stop recording from state {self._state.value}. "
                    "Expected RECORDING or PAUSED."
                )

            session = self._active_session
            if not session:
                self._last_error = "No active recording session found to stop."
                self._transition_to(ApplicationState.ERROR)
                raise RecordingProcessError("No active recording session.")

            # Stop the session first
            try:
                session.stop()
            except Exception as exc:
                logger.error("Session stop failed: %s", exc)
                self._last_error = str(exc)
                self._last_error_recovery_path = session.temp_dir
                self._transition_to(ApplicationState.ERROR)
                raise

            # Transition to FINALIZING
            self._transition_to(ApplicationState.FINALIZING)

        # Finalize
        with self._lock:
            try:
                ffmpeg_p, ffprobe_p, _ = self._ensure_pipeline_ready()
                finalizer = self._finalizer_factory(
                    ffmpeg_path=ffmpeg_p,
                    ffprobe_path=ffprobe_p,
                )

                custom_dest: Path | None = None
                if custom_output_name:
                    custom_dest = self._settings.output_dir / custom_output_name
                elif session.output_target:
                    # Generate default timestamped filename
                    timestamp_str = session.session_id.replace("session_", "iGPU-Recorder_")
                    custom_dest = self._settings.output_dir / f"{timestamp_str}.mp4"

                result = finalizer.finalize_session(
                    session=session,
                    custom_output_path=custom_dest,
                )

                self._last_finalization_result = result
                self._active_session = None
                self._last_error = None
                self._last_error_recovery_path = None
                self._transition_to(ApplicationState.IDLE)
                return result

            except Exception as exc:
                logger.error("Finalization failed: %s", exc)
                self._last_error = str(exc)
                self._last_error_recovery_path = session.temp_dir
                self._transition_to(ApplicationState.ERROR)
                raise FinalizationError(str(exc)) from exc

    def reset_error(self) -> None:
        """Reset recoverable ERROR state back to IDLE.

        Transitions: ERROR -> IDLE.
        """
        with self._lock:
            if self._state != ApplicationState.ERROR:
                raise InvalidStateTransitionError(
                    f"Cannot reset error from state {self._state.value}. Expected ERROR."
                )

            if self._active_session:
                try:
                    self._active_session.abort()
                except Exception as exc:
                    logger.warning("Error aborting session during reset: %s", exc)
                self._active_session = None

            self._last_error = None
            self._transition_to(ApplicationState.IDLE)
            logger.info("Application state reset from ERROR to IDLE.")

    def trigger_error(self, message: str, recovery_path: Path | None = None) -> None:
        """Directly transition to ERROR state from an asynchronous failure (e.g. process crash)."""
        with self._lock:
            if self._state == ApplicationState.ERROR:
                return

            if self._active_session and not recovery_path:
                recovery_path = self._active_session.temp_dir

            self._last_error = message
            self._last_error_recovery_path = recovery_path
            self._transition_to(ApplicationState.ERROR)
