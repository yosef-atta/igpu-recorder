"""Tests for Phase 5 MP4 Finalizer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from igpu_recorder.exceptions import FinalizationError
from igpu_recorder.ffmpeg import (
    FrameRate,
    HardwareBackend,
    RecordingProfile,
    Resolution,
    find_executable,
)
from igpu_recorder.finalizer import (
    Finalizer,
    VideoStreamMetadata,
    escape_ffmpeg_concat_path,
    generate_safe_destination_path,
    parse_fps,
    probe_video_file,
    write_concat_manifest,
)
from igpu_recorder.session import (
    RecordingSession,
)


class FakeCommandRunner:
    """Mock runner to record command invocations and return programmed responses."""

    def __init__(self) -> None:
        self.invocations: list[list[str]] = []
        self.ffprobe_data: dict[str, str] = {}
        self.fail_ffmpeg: bool = False
        self.fail_ffprobe: bool = False

    def __call__(self, args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        _ = timeout
        self.invocations.append(args)

        if "ffprobe" in args[0]:
            if self.fail_ffprobe:
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout="", stderr="Simulated ffprobe error"
                )
            target = args[-1]
            stdout_content = self.ffprobe_data.get(
                target,
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_name": "h264",
                                "width": 1920,
                                "height": 1080,
                                "r_frame_rate": "60/1",
                                "avg_frame_rate": "60/1",
                                "duration": "10.5",
                                "nb_frames": "630",
                            }
                        ],
                        "format": {
                            "duration": "10.5",
                            "size": "5000000",
                        },
                    }
                ),
            )
            return subprocess.CompletedProcess(
                args, returncode=0, stdout=stdout_content, stderr=""
            )

        if "ffmpeg" in args[0]:
            if self.fail_ffmpeg:
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout="", stderr="Simulated ffmpeg concat error"
                )
            # Output file is last argument in ffmpeg command
            out_file = Path(args[-1])
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b"dummy_mp4_stream_copied_data_with_faststart" * 50)
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")


class FakeProcessController:
    """Mock ProcessController for session setup."""

    def __init__(self, exit_code: int = 0, bytes_to_write: int = 4096) -> None:
        self.exit_code = exit_code
        self.bytes_to_write = bytes_to_write
        self.started_profile: RecordingProfile | None = None

    def start(self, ffmpeg_path: Path | str, profile: RecordingProfile) -> None:
        _ = ffmpeg_path
        self.started_profile = profile
        profile.output_path.parent.mkdir(parents=True, exist_ok=True)
        profile.output_path.touch()

    def stop(self) -> int:
        if self.started_profile and self.started_profile.output_path.exists():
            self.started_profile.output_path.write_bytes(b"A" * self.bytes_to_write)
        return self.exit_code

    def kill(self) -> int:
        return 0


class TestFinalizerUnit:
    """Unit tests for concat manifest, escaping, collisions, and orchestration."""

    def test_parse_fps(self) -> None:
        assert parse_fps("60/1", "60/1") == 60.0
        assert parse_fps("30/1", None) == 30.0
        assert parse_fps("30000/1001", "30000/1001") == pytest.approx(29.970029, 1e-4)
        assert parse_fps("60", None) == 60.0
        assert parse_fps("0/0", "30/1") == 30.0
        assert parse_fps("invalid", None) == 0.0
        assert parse_fps(None, None) == 0.0

    def test_escape_ffmpeg_concat_path(self) -> None:
        # Standard path with forward slash conversion
        p1 = Path(r"C:\videos\session 1\segment_000.mp4")
        escaped1 = escape_ffmpeg_concat_path(p1)
        assert escaped1.startswith("file '")
        assert escaped1.endswith("segment_000.mp4'")
        assert "\\" not in escaped1

        # Path with single quote
        p2 = Path(r"C:\videos\user's test\segment_001.mp4")
        escaped2 = escape_ffmpeg_concat_path(p2)
        assert r"user\'s test" in escaped2

    def test_write_concat_manifest(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "concat.txt"
        seg1 = tmp_path / "seg_000.mp4"
        seg2 = tmp_path / "seg_001.mp4"
        seg1.touch()
        seg2.touch()

        write_concat_manifest([seg1, seg2], manifest_file)
        assert manifest_file.exists()
        content = manifest_file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert len(lines) == 3  # 1 header comment + 2 files
        assert "seg_000.mp4" in lines[1]
        assert "seg_001.mp4" in lines[2]

    def test_write_concat_manifest_empty_raises(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "concat.txt"
        with pytest.raises(FinalizationError, match="no segments provided"):
            write_concat_manifest([], manifest_file)

    def test_generate_safe_destination_path_no_collision(self, tmp_path: Path) -> None:
        dest = tmp_path / "iGPU-Recorder_2026-08-30_12-00-00.mp4"
        safe = generate_safe_destination_path(dest)
        assert safe == dest.resolve()

    def test_generate_safe_destination_path_with_collisions(self, tmp_path: Path) -> None:
        base = tmp_path / "output.mp4"
        base.touch()

        safe1 = generate_safe_destination_path(base)
        assert safe1.name == "output (1).mp4"
        safe1.touch()

        safe2 = generate_safe_destination_path(base)
        assert safe2.name == "output (2).mp4"
        safe2.touch()

        # If base is already "output (2).mp4", next is (3)
        safe3 = generate_safe_destination_path(safe2)
        assert safe3.name == "output (3).mp4"

    def test_probe_video_file_success(self, tmp_path: Path) -> None:
        target = tmp_path / "test.mp4"
        target.write_bytes(b"dummy content")
        runner = FakeCommandRunner()
        meta = probe_video_file("ffprobe.exe", target, runner=runner)

        assert isinstance(meta, VideoStreamMetadata)
        assert meta.codec_name == "h264"
        assert meta.width == 1920
        assert meta.height == 1080
        assert meta.fps == 60.0
        assert meta.duration == 10.5
        assert meta.num_frames == 630

    def test_probe_video_file_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent.mp4"
        runner = FakeCommandRunner()
        with pytest.raises(FinalizationError, match="Target video file does not exist"):
            probe_video_file("ffprobe.exe", target, runner=runner)

    def test_probe_video_file_ffprobe_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "test.mp4"
        target.write_bytes(b"dummy")
        runner = FakeCommandRunner()
        runner.fail_ffprobe = True
        with pytest.raises(FinalizationError, match="ffprobe returned exit code 1"):
            probe_video_file("ffprobe.exe", target, runner=runner)

    def test_single_segment_finalization_stream_copy(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner,
        )

        out_path = tmp_path / "final_recording.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )

        session.start()
        session.stop()
        temp_dir = session.temp_dir
        assert temp_dir.exists()

        res = finalizer.finalize_session(session)

        assert res.output_path == out_path.resolve()
        assert res.duration == 10.5
        assert res.resolution == Resolution.R1080P
        assert res.fps == FrameRate.FPS60
        assert res.codec == "h264"
        assert res.num_segments == 1
        assert res.size_bytes > 0

        # Verify ffmpeg arguments: stream copy + faststart used, NO re-encode
        ffmpeg_cmds = [cmd for cmd in runner.invocations if "ffmpeg" in cmd[0]]
        assert len(ffmpeg_cmds) == 1
        cmd = ffmpeg_cmds[0]
        assert "-c" in cmd and "copy" in cmd
        assert "-movflags" in cmd and "+faststart" in cmd
        # Concat flag should NOT be present for single segment optimization
        assert "-f" not in cmd or "concat" not in cmd

        # Verify temporary directory cleaned up on success
        assert not temp_dir.exists()

    def test_multi_segment_concat_stream_copy(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner,
        )

        out_path = tmp_path / "multi_segment.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )

        session.start()
        session.cut()  # seg 000
        session.resume()
        session.cut()  # seg 001
        session.resume()
        session.stop()  # seg 002

        assert len(session.completed_segments) == 3
        temp_dir = session.temp_dir
        assert temp_dir.exists()

        res = finalizer.finalize_session(session)

        assert res.output_path == out_path.resolve()
        assert res.num_segments == 3
        assert res.duration == 10.5

        # Verify concat demuxer arguments with stream copy
        ffmpeg_cmds = [cmd for cmd in runner.invocations if "ffmpeg" in cmd[0]]
        assert len(ffmpeg_cmds) == 1
        cmd = ffmpeg_cmds[0]
        assert "-f" in cmd and "concat" in cmd
        assert "-safe" in cmd and "0" in cmd
        assert "-c" in cmd and "copy" in cmd
        assert "-movflags" in cmd and "+faststart" in cmd

        # Verify temp directory cleaned up
        assert not temp_dir.exists()

    def test_finalization_validation_failure_resolution_mismatch(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        # Set probe output to 720p when session expected 1080p
        runner.ffprobe_data["default"] = json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                        "r_frame_rate": "60/1",
                        "duration": "5.0",
                    }
                ],
                "format": {"duration": "5.0"},
            }
        )

        def runner_with_custom_probe(
            args: list[str], timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            if "ffprobe" in args[0]:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=runner.ffprobe_data["default"], stderr=""
                )
            return runner(args, timeout)

        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner_with_custom_probe,
        )

        out_path = tmp_path / "res_mismatch.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )
        session.start()
        session.stop()

        temp_dir = session.temp_dir
        seg_file = session.completed_segments[0].path
        assert seg_file.exists()

        with pytest.raises(FinalizationError) as exc_info:
            finalizer.finalize_session(session)

        assert "Resolution mismatch" in str(exc_info.value)
        assert f"Source segments preserved at: {temp_dir}" in str(exc_info.value)

        # Segments must be preserved
        assert temp_dir.exists()
        assert seg_file.exists()

    def test_finalization_validation_failure_codec_mismatch(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.ffprobe_data["default"] = json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                        "r_frame_rate": "60/1",
                        "duration": "5.0",
                    }
                ],
                "format": {"duration": "5.0"},
            }
        )

        def runner_with_custom_probe(
            args: list[str], timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            if "ffprobe" in args[0]:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=runner.ffprobe_data["default"], stderr=""
                )
            return runner(args, timeout)

        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner_with_custom_probe,
        )

        out_path = tmp_path / "codec_mismatch.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )
        session.start()
        session.stop()

        with pytest.raises(FinalizationError, match="Codec mismatch"):
            finalizer.finalize_session(session)

        # Source segments must be preserved
        assert session.temp_dir.exists()

    def test_finalization_validation_failure_zero_duration(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.ffprobe_data["default"] = json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "r_frame_rate": "60/1",
                        "duration": "0.0",
                    }
                ],
                "format": {"duration": "0.0"},
            }
        )

        def runner_with_custom_probe(
            args: list[str], timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            if "ffprobe" in args[0]:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=runner.ffprobe_data["default"], stderr=""
                )
            return runner(args, timeout)

        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner_with_custom_probe,
        )

        out_path = tmp_path / "zero_dur.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )
        session.start()
        session.stop()

        with pytest.raises(FinalizationError, match="Final video duration is zero"):
            finalizer.finalize_session(session)

        assert session.temp_dir.exists()

    def test_failed_ffmpeg_concat_preserves_source_segments_and_returns_path(
        self, tmp_path: Path
    ) -> None:
        runner = FakeCommandRunner()
        runner.fail_ffmpeg = True

        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner,
        )

        out_path = tmp_path / "failed_concat.mp4"
        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=out_path,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )
        session.start()
        session.cut()
        session.resume()
        session.stop()

        temp_dir = session.temp_dir
        assert temp_dir.exists()

        with pytest.raises(FinalizationError) as exc_info:
            finalizer.finalize_session(session)

        assert "FFmpeg concat failed" in str(exc_info.value)
        assert f"Source segments preserved at: {temp_dir}" in str(exc_info.value)
        assert temp_dir.exists()
        assert len(list(temp_dir.glob("segment_*.mp4"))) == 2

    def test_destination_collision_resolution_during_finalization(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        finalizer = Finalizer(
            ffmpeg_path="fake_ffmpeg.exe",
            ffprobe_path="fake_ffprobe.exe",
            command_runner=runner,
        )

        existing_out = tmp_path / "recording.mp4"
        existing_out.write_bytes(b"original preexisting file that must not be overwritten")

        session = RecordingSession(
            ffmpeg_path="fake_ffmpeg.exe",
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_target=existing_out,
            temp_base_dir=tmp_path / "session_temp",
            process_controller_factory=FakeProcessController,
        )
        session.start()
        session.stop()

        res = finalizer.finalize_session(session)

        # Ensure existing file was never touched
        assert (
            existing_out.read_bytes()
            == b"original preexisting file that must not be overwritten"
        )
        # Ensure collision resolved to "recording (1).mp4"
        assert res.output_path.name == "recording (1).mp4"
        assert res.output_path.exists()


class TestRealFinalizerIntegration:
    """Integration test with real FFmpeg and ffprobe binaries."""

    def test_real_single_and_multi_segment_finalization(self, tmp_path: Path) -> None:
        """Create real synthetic MP4 segments, verify stream copy concat + faststart."""
        ffmpeg_bin = find_executable("ffmpeg")
        ffprobe_bin = find_executable("ffprobe")
        if not ffmpeg_bin or not ffprobe_bin:
            pytest.skip("FFmpeg/ffprobe not found on host.")

        # Create two real test MP4 segments using lavfi testsrc
        seg0 = tmp_path / "seg_000.mp4"
        seg1 = tmp_path / "seg_001.mp4"

        cmd0 = [
            str(ffmpeg_bin),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1.0:size=1280x720:rate=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(seg0),
        ]
        cmd1 = [
            str(ffmpeg_bin),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1.5:size=1280x720:rate=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(seg1),
        ]
        subprocess.run(cmd0, check=True, capture_output=True)
        subprocess.run(cmd1, check=True, capture_output=True)

        finalizer = Finalizer(ffmpeg_path=ffmpeg_bin, ffprobe_path=ffprobe_bin)

        # 1. Test single segment remux + faststart
        single_out = tmp_path / "single_final.mp4"
        finalizer._remux_single_segment(seg0, single_out, apply_faststart=True)
        meta_single = finalizer.validate_output(
            single_out,
            expected_resolution=Resolution.R720P,
            expected_fps=FrameRate.FPS30,
            expected_codec="h264",
        )
        assert meta_single.width == 1280
        assert meta_single.height == 720
        assert meta_single.duration >= 0.9

        # 2. Test multi-segment stream copy concat
        manifest = tmp_path / "manifest.txt"
        write_concat_manifest([seg0, seg1], manifest)
        multi_out = tmp_path / "multi_final.mp4"
        finalizer._concat_segments(manifest, multi_out, apply_faststart=True)

        meta_multi = finalizer.validate_output(
            multi_out,
            expected_resolution=Resolution.R720P,
            expected_fps=FrameRate.FPS30,
            expected_codec="h264",
        )
        assert meta_multi.width == 1280
        assert meta_multi.height == 720
        assert meta_multi.duration >= 2.3  # combined 1.0s + 1.5s
