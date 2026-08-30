"""Tests for Phase 3 Recording Process Controller."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from igpu_recorder.exceptions import RecordingProcessError
from igpu_recorder.ffmpeg import (
    FrameRate,
    HardwareBackend,
    RecordingProfile,
    Resolution,
    find_executable,
)
from igpu_recorder.process_controller import (
    ProcessController,
    ProcessState,
)


@pytest.fixture
def dummy_profile(tmp_path: Path) -> RecordingProfile:
    """Fixture providing a standard test recording profile."""
    return RecordingProfile(
        resolution=Resolution.R720P,
        fps=FrameRate.FPS30,
        backend=HardwareBackend.QSV,
        output_path=tmp_path / "test_segment.mp4",
    )


class TestProcessControllerUnit:
    """Unit tests using fake/mocked subprocesses."""

    def test_initial_status(self) -> None:
        controller = ProcessController()
        status = controller.get_status()
        assert status.state == ProcessState.STOPPED
        assert status.is_alive is False
        assert status.pid is None
        assert status.exit_code is None
        assert status.unexpected_exit is False
        assert status.stderr_tail == ""

    def test_start_success_mock(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # Process is alive
        mock_proc.stderr = ["ffmpeg version 7.1\n", "Stream mapping: ...\n"]

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)

            assert controller.state == ProcessState.RUNNING
            status = controller.get_status()
            assert status.state == ProcessState.RUNNING
            assert status.is_alive is True
            assert status.pid == 12345
            assert status.exit_code is None
            assert status.unexpected_exit is False

    def test_detect_immediate_startup_failure(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01)

        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.poll.return_value = 1  # Exited immediately with error
        mock_proc.stderr = ["Device creation failed: Error opening D3D11\n"]

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RecordingProcessError, match="FFmpeg failed immediately on startup"):
                controller.start("fake_ffmpeg.exe", dummy_profile)

            assert controller.state == ProcessState.EXITED
            status = controller.get_status()
            assert status.state == ProcessState.EXITED
            assert status.is_alive is False
            assert status.exit_code == 1
            assert status.unexpected_exit is True
            assert "Error opening D3D11" in status.stderr_tail

    def test_prevent_multiple_simultaneous_processes(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01)

        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)
            assert controller.state == ProcessState.RUNNING

            # Second start must be rejected
            with pytest.raises(RecordingProcessError, match="already active"):
                controller.start("fake_ffmpeg.exe", dummy_profile)

    def test_graceful_stop(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01, stop_timeout=1.0)

        mock_proc = MagicMock()
        mock_proc.pid = 22222
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        mock_stdin = MagicMock()
        mock_stdin.closed = False
        mock_proc.stdin = mock_stdin
        mock_proc.stderr = []

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)
            code = controller.stop()

            assert code == 0
            mock_stdin.write.assert_called_with("q\n")
            mock_stdin.flush.assert_called()
            mock_stdin.close.assert_called()
            assert controller.state == ProcessState.STOPPED
            status = controller.get_status()
            assert status.is_alive is False
            assert status.exit_code == 0
            assert status.unexpected_exit is False

    def test_stop_fallback_to_terminate_and_kill(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(
            startup_timeout=0.01,
            stop_timeout=0.05,
            kill_timeout=0.05,
        )

        mock_proc = MagicMock()
        mock_proc.pid = 33333
        mock_proc.poll.return_value = None
        # First wait (after q) times out, terminate wait times out, kill wait returns -9
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=0.05),
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=0.05),
            -9,
        ]
        mock_stdin = MagicMock()
        mock_stdin.closed = False
        mock_proc.stdin = mock_stdin
        mock_proc.stderr = []

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)
            code = controller.stop()

            assert code == -9
            mock_proc.terminate.assert_called_once()
            mock_proc.kill.assert_called_once()
            assert controller.state == ProcessState.STOPPED

    def test_kill_method(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01, kill_timeout=0.5)

        mock_proc = MagicMock()
        mock_proc.pid = 44444
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = -9
        mock_proc.stderr = []

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)
            code = controller.kill()

            assert code == -9
            mock_proc.kill.assert_called_once()
            assert controller.state == ProcessState.STOPPED

    def test_detect_unexpected_process_death(self, dummy_profile: RecordingProfile) -> None:
        controller = ProcessController(startup_timeout=0.01)

        mock_proc = MagicMock()
        mock_proc.pid = 55555
        # Alive during startup check and state refresh, then crash code
        mock_proc.poll.side_effect = [None, None, 3221225477, 3221225477, 3221225477]
        mock_proc.stderr = ["Fatal error occurred in encoder\n"]

        with patch("subprocess.Popen", return_value=mock_proc):
            controller.start("fake_ffmpeg.exe", dummy_profile)
            assert controller.state == ProcessState.RUNNING

            # When polling status later, unexpected death is detected
            status = controller.get_status()
            assert status.state == ProcessState.EXITED
            assert status.is_alive is False
            assert status.exit_code == 3221225477
            assert status.unexpected_exit is True

    def test_stop_when_already_stopped_is_idempotent(self) -> None:
        controller = ProcessController()
        code = controller.stop()
        assert code == 0
        assert controller.state == ProcessState.STOPPED


class TestProcessControllerIntegrationWithPythonSubprocess:
    """Integration test using a real Python subprocess simulating FFmpeg stdin 'q' listener."""

    def test_real_python_subprocess_graceful_stop(self, tmp_path: Path) -> None:
        # Create a tiny script that listens for 'q' on stdin
        fake_ffmpeg_py = tmp_path / "fake_ffmpeg.py"
        fake_ffmpeg_py.write_text(
            "import sys, time\n"
            "sys.stderr.write('Fake FFmpeg starting\\n')\n"
            "sys.stderr.flush()\n"
            "line = sys.stdin.readline()\n"
            "if 'q' in line:\n"
            "    sys.stderr.write('Cleanly finalized\\n')\n"
            "    sys.stderr.flush()\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )

        profile = RecordingProfile(
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_path=tmp_path / "out.mp4",
        )

        controller = ProcessController(startup_timeout=0.1, stop_timeout=2.0)

        # Patch build_recording_args to invoke our fake python script
        with patch(
            "igpu_recorder.process_controller.build_recording_args",
            return_value=[sys.executable, str(fake_ffmpeg_py)],
        ):
            controller.start("ffmpeg", profile)
            status = controller.get_status()
            assert status.state == ProcessState.RUNNING
            assert status.is_alive is True

            time.sleep(0.1)
            exit_code = controller.stop()
            assert exit_code == 0
            assert controller.state == ProcessState.STOPPED
            final_status = controller.get_status()
            assert final_status.is_alive is False
            assert final_status.exit_code == 0
            assert final_status.unexpected_exit is False
            assert "Fake FFmpeg starting" in final_status.stderr_tail
            assert "Cleanly finalized" in final_status.stderr_tail


class TestRealFFmpegProcessControllerLifecycle:
    """Real FFmpeg lifecycle tests on Windows reference hardware."""

    def test_real_ffmpeg_start_stop_mp4_metadata(self, tmp_path: Path) -> None:
        ffmpeg_bin = find_executable("ffmpeg")
        ffprobe_bin = find_executable("ffprobe")
        if not ffmpeg_bin or not ffprobe_bin:
            pytest.skip("FFmpeg/ffprobe not found on host machine.")

        out_file = tmp_path / "segment_000.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_path=out_file,
            display_index=0,
            draw_mouse=False,
            global_quality=23,
        )

        controller = ProcessController(startup_timeout=0.5, stop_timeout=5.0)

        # 1. Start recording
        try:
            controller.start(ffmpeg_bin, profile)
        except RecordingProcessError as exc:
            err_str = str(exc)
            if (
                "Desktop duplication access denied" in err_str
                or "Operation not permitted" in err_str
            ):
                pytest.skip("Desktop duplication not permitted in current Windows session.")
            raise

        assert controller.state == ProcessState.RUNNING
        status = controller.get_status()
        assert status.is_alive is True
        assert status.pid is not None

        # Let it record for 1.5 seconds
        time.sleep(1.5)

        # 2. Stop recording
        exit_code = controller.stop()
        assert exit_code == 0
        assert controller.state == ProcessState.STOPPED
        assert out_file.exists()
        assert out_file.stat().st_size > 0

        # 3. Verify MP4 metadata with ffprobe
        probe_res = subprocess.run(
            [
                str(ffprobe_bin),
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_name,width,height,r_frame_rate",
                "-of",
                "default=noprint_wrappers=1",
                str(out_file),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        probe_out = probe_res.stdout
        assert "format_name=mov,mp4,m4a,3gp,3g2,mj2" in probe_out or "mp4" in probe_out
        assert "codec_name=h264" in probe_out
        assert "width=1280" in probe_out
        assert "height=720" in probe_out

    def test_repeated_start_stop_cycles(self, tmp_path: Path) -> None:
        ffmpeg_bin = find_executable("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("FFmpeg not found on host machine.")

        controller = ProcessController(startup_timeout=0.4, stop_timeout=4.0)

        # Run 2 consecutive recording cycles through the same controller
        for i in range(2):
            out_file = tmp_path / f"cycle_{i}.mp4"
            profile = RecordingProfile(
                resolution=Resolution.R720P,
                fps=FrameRate.FPS30,
                backend=HardwareBackend.QSV,
                output_path=out_file,
            )
            try:
                controller.start(ffmpeg_bin, profile)
            except RecordingProcessError as exc:
                err_str = str(exc)
                if (
                    "Desktop duplication access denied" in err_str
                    or "Operation not permitted" in err_str
                ):
                    pytest.skip("Desktop duplication not permitted in current Windows session.")
                raise

            assert controller.state == ProcessState.RUNNING
            time.sleep(1.0)
            code = controller.stop()
            assert code == 0
            assert controller.state == ProcessState.STOPPED
            assert out_file.exists()
            assert out_file.stat().st_size > 0
