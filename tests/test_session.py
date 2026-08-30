"""Tests for Phase 4 Recording Session and CUT/Resume workflows."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from igpu_recorder.exceptions import RecordingProcessError
from igpu_recorder.ffmpeg import (
    FrameRate,
    HardwareBackend,
    RecordingProfile,
    Resolution,
    find_executable,
)
from igpu_recorder.session import (
    RecordingSession,
    SessionSnapshot,
    SessionState,
    format_segment_filename,
    generate_session_id,
)

if TYPE_CHECKING:
    from pathlib import Path


class FakeProcessController:
    """Mock ProcessController that writes dummy valid video data on stop."""

    def __init__(
        self,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
        exit_code: int = 0,
        bytes_to_write: int = 2048,
    ) -> None:
        self.fail_on_start = fail_on_start
        self.fail_on_stop = fail_on_stop
        self.exit_code = exit_code
        self.bytes_to_write = bytes_to_write
        self.started_profile: RecordingProfile | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self.kill_calls = 0

    def start(self, ffmpeg_path: Path | str, profile: RecordingProfile) -> None:
        _ = ffmpeg_path
        self.start_calls += 1
        if self.fail_on_start:
            raise RecordingProcessError("Simulated controller startup failure")
        self.started_profile = profile
        # Create output file immediately so it exists
        profile.output_path.parent.mkdir(parents=True, exist_ok=True)
        profile.output_path.touch()

    def stop(self) -> int:
        self.stop_calls += 1
        if self.fail_on_stop:
            raise RecordingProcessError("Simulated controller stop failure")
        if self.started_profile and self.started_profile.output_path.exists():
            self.started_profile.output_path.write_bytes(b"0" * self.bytes_to_write)
        return self.exit_code

    def kill(self) -> int:
        self.kill_calls += 1
        return -9


@pytest.fixture
def fake_controller_factory() -> list[FakeProcessController]:
    created: list[FakeProcessController] = []

    def _factory() -> FakeProcessController:
        ctrl = FakeProcessController()
        created.append(ctrl)
        return ctrl

    _factory.created = created  # type: ignore[attr-defined]
    return _factory  # type: ignore[return-value]


class TestRecordingSessionUnit:
    """Unit tests for session state transitions, naming, snapshots, and error handling."""

    def test_session_id_generation(self) -> None:
        sid1 = generate_session_id()
        sid2 = generate_session_id()
        assert sid1.startswith("session_")
        assert sid2.startswith("session_")
        assert sid1 != sid2

    def test_segment_filename_formatting(self) -> None:
        assert format_segment_filename(0) == "segment_000.mp4"
        assert format_segment_filename(1) == "segment_001.mp4"
        assert format_segment_filename(99) == "segment_099.mp4"
        assert format_segment_filename(100) == "segment_100.mp4"

    def test_session_initialization_and_snapshot(self, tmp_path: Path) -> None:
        out_target = tmp_path / "final_recording.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_target,
            temp_base_dir=tmp_path / "temp_sessions",
        )

        assert session.state == SessionState.IDLE
        assert session.temp_dir.exists()
        assert session.temp_dir.is_dir()
        assert session.current_segment_index == 0
        assert len(session.completed_segments) == 0

        snapshot = session.get_snapshot()
        assert isinstance(snapshot, SessionSnapshot)
        assert snapshot.session_id == session.session_id
        assert snapshot.state == SessionState.IDLE
        assert snapshot.resolution == Resolution.R1080P
        assert snapshot.fps == FrameRate.FPS60
        assert snapshot.backend == HardwareBackend.QSV
        assert snapshot.output_target == out_target.resolve()
        assert snapshot.temp_dir == session.temp_dir
        assert snapshot.is_active is False

    def test_start_segment_000(self, tmp_path: Path) -> None:
        controller_instances: list[FakeProcessController] = []

        def factory() -> FakeProcessController:
            c = FakeProcessController()
            controller_instances.append(c)
            return c

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=factory,
        )

        seg_path = session.start()
        assert session.state == SessionState.RECORDING
        assert seg_path.name == "segment_000.mp4"
        assert seg_path.parent == session.temp_dir
        assert len(controller_instances) == 1
        assert controller_instances[0].start_calls == 1
        assert controller_instances[0].started_profile is not None
        assert controller_instances[0].started_profile.resolution == Resolution.R720P
        assert controller_instances[0].started_profile.fps == FrameRate.FPS30

        # Cannot start again while recording
        with pytest.raises(
            RecordingProcessError, match="Cannot start session from state 'recording'"
        ):
            session.start()

    def test_cut_and_resume_cycle(self, tmp_path: Path) -> None:
        controller_instances: list[FakeProcessController] = []

        def factory() -> FakeProcessController:
            c = FakeProcessController()
            controller_instances.append(c)
            return c

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=factory,
        )

        # 1. Start -> segment_000.mp4
        seg0 = session.start()
        assert seg0.name == "segment_000.mp4"
        assert session.state == SessionState.RECORDING

        # 2. CUT -> PAUSED
        info0 = session.cut()
        assert session.state == SessionState.PAUSED
        assert info0.index == 0
        assert info0.filename == "segment_000.mp4"
        assert info0.is_valid is True
        assert info0.size_bytes >= 1024
        assert len(session.completed_segments) == 1

        # Cannot CUT again while PAUSED
        with pytest.raises(RecordingProcessError, match="Cannot execute CUT from state 'paused'"):
            session.cut()

        # 3. Resume -> segment_001.mp4
        seg1 = session.resume()
        assert session.state == SessionState.RECORDING
        assert seg1.name == "segment_001.mp4"
        assert session.current_segment_index == 1
        assert len(controller_instances) == 2

        # Verify resumed segment uses identical encoding settings
        assert controller_instances[1].started_profile is not None
        assert controller_instances[1].started_profile.resolution == Resolution.R1080P
        assert controller_instances[1].started_profile.fps == FrameRate.FPS60
        assert controller_instances[1].started_profile.backend == HardwareBackend.QSV

        # 4. Stop from RECORDING
        segments = session.stop()
        assert session.state == SessionState.STOPPED
        assert len(segments) == 2
        assert segments[0].filename == "segment_000.mp4"
        assert segments[1].filename == "segment_001.mp4"

    def test_multi_cut_resume_cycles(self, tmp_path: Path) -> None:
        controller_instances: list[FakeProcessController] = []

        def factory() -> FakeProcessController:
            c = FakeProcessController()
            controller_instances.append(c)
            return c

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.AMF,
            output_target=tmp_path / "out.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=factory,
        )

        session.start()  # seg 0
        for i in range(3):
            session.cut()  # pause
            assert session.state == SessionState.PAUSED
            session.resume()  # resume next
            assert session.state == SessionState.RECORDING
            assert session.current_segment_index == i + 1

        # Stop from recording
        final_segs = session.stop()
        assert len(final_segs) == 4
        for idx, seg in enumerate(final_segs):
            assert seg.index == idx
            assert seg.filename == f"segment_{idx:03d}.mp4"
            assert seg.is_valid is True

    def test_stop_from_paused(self, tmp_path: Path) -> None:
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=FakeProcessController,
        )

        session.start()
        session.cut()
        assert session.state == SessionState.PAUSED

        # Stop directly from PAUSED
        completed = session.stop()
        assert session.state == SessionState.STOPPED
        assert len(completed) == 1
        assert completed[0].filename == "segment_000.mp4"

    def test_reject_empty_or_invalid_segment_on_cut(self, tmp_path: Path) -> None:
        def invalid_controller_factory() -> FakeProcessController:
            # Writes 0 bytes (invalid/empty segment)
            return FakeProcessController(bytes_to_write=0)

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=invalid_controller_factory,
        )

        session.start()
        with pytest.raises(RecordingProcessError, match="invalid or empty"):
            session.cut()

        assert session.state == SessionState.FAILED

    def test_reject_non_zero_exit_code_on_cut(self, tmp_path: Path) -> None:
        def err_controller_factory() -> FakeProcessController:
            return FakeProcessController(exit_code=1, bytes_to_write=2048)

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=err_controller_factory,
        )

        session.start()
        with pytest.raises(RecordingProcessError, match="exit code: 1"):
            session.cut()

        assert session.state == SessionState.FAILED

    def test_preserve_completed_segments_on_abort_or_failure(self, tmp_path: Path) -> None:
        c1 = FakeProcessController(bytes_to_write=2048)
        c2 = FakeProcessController(fail_on_start=True)
        controllers = [c1, c2]

        def factory() -> FakeProcessController:
            return controllers.pop(0)

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=factory,
        )

        session.start()
        session.cut()
        assert len(session.completed_segments) == 1
        seg0_path = session.completed_segments[0].path
        assert seg0_path.exists()

        # Resuming fails
        with pytest.raises(RecordingProcessError, match="Simulated controller startup failure"):
            session.resume()

        assert session.state == SessionState.FAILED
        # Verify segment 0 is still preserved
        assert seg0_path.exists()
        assert len(session.completed_segments) == 1

        # Calling abort cleans up running processes but preserves files
        session.abort()
        assert seg0_path.exists()

    def test_cleanup_temp_dir_safety(self, tmp_path: Path) -> None:
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=tmp_path / "final.mp4",
            temp_base_dir=tmp_path,
            process_controller_factory=FakeProcessController,
        )
        temp_dir = session.temp_dir
        assert temp_dir.exists()

        session.start()
        # Should not cleanup while recording without force
        session.cleanup_temp_dir(force=False)
        assert temp_dir.exists()

        session.stop()
        assert session.state == SessionState.STOPPED
        # Should cleanup when STOPPED
        session.cleanup_temp_dir()
        assert not temp_dir.exists()


class TestRealRecordingSessionIntegration:
    """End-to-end integration tests using real FFmpeg on Windows reference hardware."""

    def test_real_start_cut_resume_stop_exit_criteria(self, tmp_path: Path) -> None:
        """Exit criteria test:

        Start -> record 2s -> CUT -> wait 1s -> Resume -> record 2s -> Stop.
        Produces segment_000.mp4 and segment_001.mp4 with roughly 4s combined media duration.
        """
        ffmpeg_bin = find_executable("ffmpeg")
        ffprobe_bin = find_executable("ffprobe")
        if not ffmpeg_bin or not ffprobe_bin:
            pytest.skip("FFmpeg/ffprobe not found on host machine.")

        out_final = tmp_path / "iGPU-Recorder_output.mp4"
        session = RecordingSession(
            ffmpeg_path=ffmpeg_bin,
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_target=out_final,
            temp_base_dir=tmp_path / "sessions",
            draw_mouse=False,
            global_quality=23,
        )

        try:
            # 1. Start
            session.start()
        except RecordingProcessError as exc:
            err_str = str(exc)
            if (
                "Desktop duplication access denied" in err_str
                or "Operation not permitted" in err_str
            ):
                pytest.skip("Desktop duplication not permitted in current Windows session.")
            raise

        assert session.state == SessionState.RECORDING

        # Record ~2.0s
        time.sleep(2.0)

        # 2. CUT
        info0 = session.cut()
        assert session.state == SessionState.PAUSED
        assert info0.index == 0
        assert info0.path.exists()
        assert info0.size_bytes > 0

        # Wait during CUT/pause
        time.sleep(1.0)

        # 3. Resume
        session.resume()
        assert session.state == SessionState.RECORDING
        assert session.current_segment_index == 1

        # Record another ~2.0s
        time.sleep(2.0)

        # 4. Stop
        completed = session.stop()
        assert session.state == SessionState.STOPPED
        assert len(completed) == 2

        seg0 = completed[0]
        seg1 = completed[1]
        assert seg0.filename == "segment_000.mp4"
        assert seg1.filename == "segment_001.mp4"
        assert seg0.path.exists()
        assert seg1.path.exists()
        assert seg0.size_bytes > 0
        assert seg1.size_bytes > 0

        session.cleanup_temp_dir()
