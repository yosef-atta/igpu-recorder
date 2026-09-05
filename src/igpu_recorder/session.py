"""Recording session management and segment orchestration.

Provides:
- RecordingSession: Manages the lifecycle of a logical recording comprising
  multiple sequential hardware-encoded segments.
- SessionState: Typed session state enum.
- SegmentInfo: Metadata representing an individual recorded segment.
- Support for Start, CUT, Resume, and Stop from RECORDING or PAUSED.
- Snapshot and enforcement of immutable settings throughout session lifetime.
- Deterministic session ID and segment naming.
- Robust segment finalization, validation, and error recovery.
"""

from __future__ import annotations

import enum
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from igpu_recorder.exceptions import RecordingProcessError
from igpu_recorder.ffmpeg import RecordingProfile
from igpu_recorder.logging import get_logger
from igpu_recorder.process_controller import ProcessController

if TYPE_CHECKING:
    from collections.abc import Callable

    from igpu_recorder.ffmpeg import FrameRate, HardwareBackend, Resolution

logger = get_logger("session")

MIN_VALID_SEGMENT_BYTES = 1024  # Minimum non-empty MP4 threshold


class ProcessControllerProtocol(Protocol):
    """Structural protocol for process controller lifecycle."""

    def start(self, ffmpeg_path: Path | str, profile: RecordingProfile) -> None: ...
    def stop(self) -> int: ...
    def kill(self) -> int: ...



class SessionState(enum.Enum):
    """Lifecycle states of a logical recording session."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class SegmentInfo:
    """Metadata for a completed or active segment."""

    index: int
    path: Path
    start_time: float
    end_time: float | None = None
    size_bytes: int = 0
    is_valid: bool = False

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class SessionSnapshot:
    """Immutable snapshot of the session configuration and status."""

    session_id: str
    state: SessionState
    resolution: Resolution
    fps: FrameRate
    backend: HardwareBackend
    output_target: Path
    temp_dir: Path
    segments: tuple[SegmentInfo, ...]
    current_segment_index: int
    created_at: float
    is_active: bool


def generate_session_id() -> str:
    """Generate a unique deterministic-format session identifier."""
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    unique_hex = uuid.uuid4().hex[:8]
    return f"session_{timestamp_str}_{unique_hex}"


def format_segment_filename(segment_index: int) -> str:
    """Format segment index into zero-padded filename segment_XXX.mp4."""
    return f"segment_{segment_index:03d}.mp4"


class RecordingSession:
    """Manages a single logical recording session across multiple segments.

    Enforces immutable settings, manages temporary directories, controls
    the underlying FFmpeg process for segment transitions (Start, CUT, Resume, Stop),
    and validates completed segments.
    """

    def __init__(
        self,
        ffmpeg_path: Path | str,
        resolution: Resolution,
        fps: FrameRate,
        backend: HardwareBackend,
        output_target: Path | str,
        display_index: int = 0,
        draw_mouse: bool = True,
        global_quality: int = 23,
        temp_base_dir: Path | str | None = None,
        process_controller_factory: Callable[[], ProcessControllerProtocol] | None = None,
        min_segment_bytes: int = MIN_VALID_SEGMENT_BYTES,
    ) -> None:
        """Initialize session configuration and allocate private temporary directory.

        Settings become frozen from initialization.
        """
        self._session_id = generate_session_id()
        self._created_at = time.time()
        self._ffmpeg_path = Path(ffmpeg_path)

        # Snapshot immutable recording settings
        self._resolution = resolution
        self._fps = fps
        self._backend = backend
        self._output_target = Path(output_target).resolve()
        self._display_index = display_index
        self._draw_mouse = draw_mouse
        self._global_quality = global_quality
        self._min_segment_bytes = min_segment_bytes

        # Create private temporary session directory
        if temp_base_dir:
            base = Path(temp_base_dir).resolve()
            base.mkdir(parents=True, exist_ok=True)
            self._temp_dir = Path(tempfile.mkdtemp(prefix=f"igpu_{self._session_id}_", dir=base))
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix=f"igpu_{self._session_id}_"))

        self._controller_factory = process_controller_factory or ProcessController
        self._controller: ProcessControllerProtocol | None = None

        self._state = SessionState.IDLE
        self._lock = threading.Lock()
        self._segments: list[SegmentInfo] = []
        self._current_segment_index = 0
        self._current_segment_start_time: float = 0.0

        logger.info(
            "Initialized RecordingSession [%s] (dir: %s, res: %s, fps: %s, backend: %s)",
            self._session_id,
            self._temp_dir,
            self._resolution.value,
            self._fps.value,
            self._backend.value,
        )
        self._write_metadata()

    def _write_metadata(self) -> None:
        """Write session state to disk for headless recovery."""
        metadata = {
            "session_id": self._session_id,
            "resolution": self._resolution.value,
            "fps": self._fps.value,
            "backend": self._backend.value,
            "output_target": str(self._output_target),
        }
        try:
            with open(self._temp_dir / "session_metadata.json", "w", encoding="utf-8") as f:
                import json
                json.dump(metadata, f, indent=2)
        except Exception as exc:
            logger.warning("Failed to write session metadata: %s", exc)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def temp_dir(self) -> Path:
        return self._temp_dir

    @property
    def output_target(self) -> Path:
        return self._output_target

    @property
    def completed_segments(self) -> tuple[SegmentInfo, ...]:
        with self._lock:
            return tuple(self._segments)

    @property
    def current_segment_index(self) -> int:
        with self._lock:
            return self._current_segment_index

    def get_snapshot(self) -> SessionSnapshot:
        """Return an immutable snapshot of current session state."""
        with self._lock:
            return SessionSnapshot(
                session_id=self._session_id,
                state=self._state,
                resolution=self._resolution,
                fps=self._fps,
                backend=self._backend,
                output_target=self._output_target,
                temp_dir=self._temp_dir,
                segments=tuple(self._segments),
                current_segment_index=self._current_segment_index,
                created_at=self._created_at,
                is_active=self._state in (SessionState.RECORDING, SessionState.PAUSED),
            )

    def _build_profile_for_segment(self, segment_path: Path) -> RecordingProfile:
        """Create a RecordingProfile for a specific segment path using snapshotted settings."""
        return RecordingProfile(
            resolution=self._resolution,
            fps=self._fps,
            backend=self._backend,
            output_path=segment_path,
            display_index=self._display_index,
            draw_mouse=self._draw_mouse,
            global_quality=self._global_quality,
        )

    def start(self) -> Path:
        """Start the initial recording segment (segment_000.mp4).

        Transitions state from IDLE to RECORDING.

        Returns:
            Path to the segment file being recorded.

        Raises:
            RecordingProcessError: If session is not in IDLE state or startup fails.
        """
        with self._lock:
            if self._state != SessionState.IDLE:
                raise RecordingProcessError(
                    f"Cannot start session from state '{self._state.value}'. Expected IDLE."
                )

            self._current_segment_index = 0
            return self._start_segment_locked()

    def cut(self) -> SegmentInfo:
        """Execute CUT on the active recording segment.

        Gracefully finalizes the active segment, validates it, appends it to completed segments,
        and transitions session state to PAUSED.

        Returns:
            SegmentInfo for the finalized segment.

        Raises:
            RecordingProcessError: If not currently RECORDING or if finalizing fails.
        """
        with self._lock:
            if self._state != SessionState.RECORDING:
                raise RecordingProcessError(
                    f"Cannot execute CUT from state '{self._state.value}'. Expected RECORDING."
                )

            segment_info = self._finalize_active_segment_locked()
            self._state = SessionState.PAUSED
            logger.info(
                "Session [%s] paused via CUT. Completed segment: %s (%d bytes)",
                self._session_id,
                segment_info.filename,
                segment_info.size_bytes,
            )
            return segment_info

    def resume(self) -> Path:
        """Resume recording after a CUT.

        Increments the segment index, starts the new segment process with identical settings,
        and transitions session state to RECORDING.

        Returns:
            Path to the newly started segment file.

        Raises:
            RecordingProcessError: If not currently PAUSED or if startup fails.
        """
        with self._lock:
            if self._state != SessionState.PAUSED:
                raise RecordingProcessError(
                    f"Cannot resume session from state '{self._state.value}'. Expected PAUSED."
                )

            self._current_segment_index += 1
            return self._start_segment_locked()

    def stop(self) -> tuple[SegmentInfo, ...]:
        """Stop the session from either RECORDING or PAUSED state.

        If RECORDING: gracefully stops and validates the active segment.
        If PAUSED: no active process needs stopping; completes the session.
        Transitions state to STOPPED.

        Returns:
            Tuple of all completed valid SegmentInfo objects.

        Raises:
            RecordingProcessError: If session is already STOPPED, FAILED, or IDLE.
        """
        with self._lock:
            if self._state not in (SessionState.RECORDING, SessionState.PAUSED):
                raise RecordingProcessError(
                    f"Cannot stop session from state '{self._state.value}'. "
                    "Expected RECORDING or PAUSED."
                )

            if self._state == SessionState.RECORDING:
                logger.info("Stopping session [%s] from RECORDING state...", self._session_id)
                self._finalize_active_segment_locked()
            else:
                logger.info("Stopping session [%s] from PAUSED state...", self._session_id)

            self._state = SessionState.STOPPED
            logger.info(
                "Session [%s] successfully stopped with %d completed segments.",
                self._session_id,
                len(self._segments),
            )
            return tuple(self._segments)

    def abort(self) -> None:
        """Abort/kill any active segment without throwing, preserving completed segments."""
        with self._lock:
            if self._controller is not None:
                try:
                    self._controller.kill()
                except Exception as exc:
                    logger.warning("Error during abort kill: %s", exc)
                finally:
                    self._controller = None

            if self._state not in (SessionState.STOPPED, SessionState.IDLE):
                self._state = SessionState.FAILED
            logger.warning(
                "Session [%s] aborted. %d completed segments preserved in %s.",
                self._session_id,
                len(self._segments),
                self._temp_dir,
            )

    def cleanup_temp_dir(self, force: bool = False) -> None:
        """Remove the temporary directory.

        By default, will only delete if the session is STOPPED or force=True.
        """
        with self._lock:
            if not force and self._state != SessionState.STOPPED:
                logger.warning(
                    "Refusing to cleanup temporary directory while session is %s.",
                    self._state.value,
                )
                return

        if self._temp_dir.exists():
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                logger.info("Cleaned up temporary directory %s", self._temp_dir)
            except OSError as exc:
                logger.error("Failed to delete temp dir %s: %s", self._temp_dir, exc)

    def _start_segment_locked(self) -> Path:
        """Start segment process under lock."""
        segment_name = format_segment_filename(self._current_segment_index)
        segment_path = self._temp_dir / segment_name

        profile = self._build_profile_for_segment(segment_path)
        controller = self._controller_factory()

        try:
            controller.start(self._ffmpeg_path, profile)
        except Exception as exc:
            self._state = SessionState.FAILED
            logger.error(
                "Failed to start segment %s for session [%s]: %s",
                segment_name,
                self._session_id,
                exc,
            )
            raise RecordingProcessError(
                f"Failed to start segment {segment_name}: {exc}"
            ) from exc

        self._controller = controller
        self._current_segment_start_time = time.time()
        self._state = SessionState.RECORDING
        logger.info(
            "Session [%s] started segment %d -> %s",
            self._session_id,
            self._current_segment_index,
            segment_path,
        )
        return segment_path

    def _finalize_active_segment_locked(self) -> SegmentInfo:
        """Stop active controller, validate segment, and record metadata under lock."""
        if self._controller is None:
            raise RecordingProcessError("No active recorder process controller found.")

        segment_name = format_segment_filename(self._current_segment_index)
        segment_path = self._temp_dir / segment_name
        end_time = time.time()

        try:
            exit_code = self._controller.stop()
        except Exception as exc:
            self._state = SessionState.FAILED
            logger.error("Error stopping recorder process for segment %s: %s", segment_name, exc)
            raise RecordingProcessError(
                f"Failed to cleanly stop segment {segment_name}: {exc}"
            ) from exc
        finally:
            self._controller = None

        # Validate segment file
        if not segment_path.exists():
            self._state = SessionState.FAILED
            err = f"Completed segment file does not exist: {segment_path}"
            logger.error(err)
            raise RecordingProcessError(err)

        size_bytes = segment_path.stat().st_size
        if size_bytes < self._min_segment_bytes or exit_code != 0:
            self._state = SessionState.FAILED
            err = (
                f"Completed segment {segment_name} is invalid or empty "
                f"(size: {size_bytes} bytes, exit code: {exit_code})."
            )
            logger.error(err)
            raise RecordingProcessError(err)

        info = SegmentInfo(
            index=self._current_segment_index,
            path=segment_path,
            start_time=self._current_segment_start_time,
            end_time=end_time,
            size_bytes=size_bytes,
            is_valid=True,
        )
        self._segments.append(info)
        return info
