"""Tests for Phase 2 FFmpeg capability layer and command builder."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from igpu_recorder.exceptions import HardwareProbeError
from igpu_recorder.ffmpeg import (
    FrameRate,
    HardwareBackend,
    RecordingProfile,
    Resolution,
    build_recording_args,
    check_ddagrab_filter,
    detect_available_encoders,
    find_executable,
    parse_ffmpeg_version,
    probe_capabilities,
    run_probe_command,
)


class TestFFmpegModels:
    """Test data models and enumerations."""

    def test_resolution_dimensions(self) -> None:
        assert Resolution.R720P.dimensions == (1280, 720)
        assert Resolution.R720P.width == 1280
        assert Resolution.R720P.height == 720

        assert Resolution.R1080P.dimensions == (1920, 1080)
        assert Resolution.R1080P.width == 1920
        assert Resolution.R1080P.height == 1080

    def test_framerate_values(self) -> None:
        assert FrameRate.FPS30.value == 30
        assert FrameRate.FPS60.value == 60

    def test_backend_encoder_names(self) -> None:
        assert HardwareBackend.QSV.encoder_name == "h264_qsv"
        assert HardwareBackend.AMF.encoder_name == "h264_amf"

    def test_recording_profile_validation(self, tmp_path: Path) -> None:
        out_file = tmp_path / "test.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_path=out_file,
        )
        assert profile.resolution == Resolution.R1080P
        assert profile.fps == FrameRate.FPS60
        assert profile.backend == HardwareBackend.QSV
        assert profile.output_path == out_file
        assert profile.display_index == 0
        assert profile.draw_mouse is True
        assert profile.global_quality == 23

    def test_recording_profile_invalid_types(self, tmp_path: Path) -> None:
        out_file = tmp_path / "test.mp4"
        with pytest.raises(TypeError):
            RecordingProfile(
                resolution="1080p",  # type: ignore[arg-type]
                fps=FrameRate.FPS60,
                backend=HardwareBackend.QSV,
                output_path=out_file,
            )

        with pytest.raises(TypeError):
            RecordingProfile(
                resolution=Resolution.R1080P,
                fps=60,  # type: ignore[arg-type]
                backend=HardwareBackend.QSV,
                output_path=out_file,
            )

        with pytest.raises(TypeError):
            RecordingProfile(
                resolution=Resolution.R1080P,
                fps=FrameRate.FPS60,
                backend="qsv",  # type: ignore[arg-type]
                output_path=out_file,
            )

        with pytest.raises(TypeError):
            RecordingProfile(
                resolution=Resolution.R1080P,
                fps=FrameRate.FPS60,
                backend=HardwareBackend.QSV,
                output_path="test.mp4",  # type: ignore[arg-type]
            )


class TestCommandBuilder:
    """Test deterministic FFmpeg argument construction."""

    def test_build_qsv_1080p60_args(self, tmp_path: Path) -> None:
        out_file = tmp_path / "record.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_path=out_file,
            display_index=0,
            draw_mouse=True,
            global_quality=23,
        )
        args = build_recording_args("ffmpeg.exe", profile)

        # Expected structured argument list
        assert args[0] == "ffmpeg.exe"
        assert "-hide_banner" in args
        assert "-y" in args
        assert "-init_hw_device" in args
        assert "d3d11va=d3d11" in args
        assert "qsv=qsv@d3d11" in args
        assert "ddagrab=output_idx=0:draw_mouse=1:framerate=60" in args
        assert "hwmap=derive_device=qsv,scale_qsv=format=nv12" in args
        assert "h264_qsv" in args
        assert "-global_quality" in args
        assert "23" in args
        assert "-fps_mode" in args
        assert "cfr" in args
        assert str(out_file.resolve()) in args

        # Ensure no accidental shell strings
        for arg in args:
            assert not arg.startswith("ffmpeg ")

    def test_build_qsv_720p30_args(self, tmp_path: Path) -> None:
        out_file = tmp_path / "record_720p.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R720P,
            fps=FrameRate.FPS30,
            backend=HardwareBackend.QSV,
            output_path=out_file,
            display_index=1,
            draw_mouse=False,
            global_quality=20,
        )
        args = build_recording_args("C:/ffmpeg/ffmpeg.exe", profile)

        assert "ddagrab=output_idx=1:draw_mouse=0:framerate=30" in args
        assert "hwmap=derive_device=qsv,scale_qsv=w=1280:h=720:format=nv12" in args
        assert "20" in args
        assert str(out_file.resolve()) in args

    def test_build_amf_1080p60_args(self, tmp_path: Path) -> None:
        out_file = tmp_path / "amf_1080p.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.AMF,
            output_path=out_file,
        )
        args = build_recording_args("ffmpeg.exe", profile)

        assert "h264_amf" in args
        assert "ddagrab=output_idx=0:draw_mouse=1:framerate=60" in args
        assert "format=nv12" in args
        assert "-quality" in args
        assert "speed" in args
        assert str(out_file.resolve()) in args

    def test_build_amf_720p60_args(self, tmp_path: Path) -> None:
        out_file = tmp_path / "amf_720p.mp4"
        profile = RecordingProfile(
            resolution=Resolution.R720P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.AMF,
            output_path=out_file,
        )
        args = build_recording_args("ffmpeg.exe", profile)

        assert "h264_amf" in args
        assert "scale=1280:720,format=nv12" in args
        assert str(out_file.resolve()) in args

    def test_prevent_command_injection_via_paths(self, tmp_path: Path) -> None:
        # A malicious path containing quotes and command separators
        evil_path = tmp_path / 'out"; rm -rf /; echo "pwned.mp4'
        profile = RecordingProfile(
            resolution=Resolution.R1080P,
            fps=FrameRate.FPS60,
            backend=HardwareBackend.QSV,
            output_path=evil_path,
        )
        args = build_recording_args("ffmpeg.exe", profile)
        # Verify argument array safety
        assert str(evil_path.resolve()) in args
        assert args[-1] == str(evil_path.resolve())


class TestProbeParsingAndDiscovery:
    """Test version parsing, encoder filtering, and probe execution."""

    def test_parse_ffmpeg_version(self) -> None:
        sample_output = (
            "ffmpeg version N-120037-g36c8eef42c-20250625 Copyright (c) 2000-2025 FFmpeg\n"
            "built with gcc 15.1.0\n"
        )
        version = parse_ffmpeg_version(sample_output)
        assert version == "N-120037-g36c8eef42c-20250625"

        sample_release = "ffmpeg version 7.1-full_build-www.gyan.dev Copyright (c) 2000-2024"
        assert parse_ffmpeg_version(sample_release) == "7.1-full_build-www.gyan.dev"

    def test_detect_available_encoders_parsing(self, tmp_path: Path) -> None:
        dummy_ffmpeg = tmp_path / "ffmpeg.exe"
        sample_encoders_stdout = (
            "Encoders:\n"
            " V..... = Video\n"
            " A..... = Audio\n"
            " S..... = Subtitle\n"
            " ------\n"
            " V..... h264_qsv             Intel Quick Sync Video acceleration\n"
            " V..... h264_amf             AMD AMF H.264 Encoder\n"
            " V..... h264_nvenc           NVIDIA NVENC H.264 encoder\n"
            " A..... aac                  AAC (Advanced Audio Coding)\n"
            " V..... libx264              libx264 H.264\n"
        )
        with patch("igpu_recorder.ffmpeg.run_probe_command") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg", "-encoders"],
                returncode=0,
                stdout=sample_encoders_stdout,
                stderr="",
            )
            encoders = detect_available_encoders(dummy_ffmpeg)
            assert "h264_qsv" in encoders
            assert "h264_amf" in encoders
            assert "h264_nvenc" in encoders
            assert "libx264" in encoders
            assert "aac" not in encoders  # Audio codec excluded

    def test_check_ddagrab_filter_true(self, tmp_path: Path) -> None:
        dummy_ffmpeg = tmp_path / "ffmpeg.exe"
        sample_filters = """Filters:
  .. ddagrab           |->V       Grab Windows Desktop images using Desktop Duplication API
  .. nullsink          V->|       Do absolutely nothing with the input video.
"""
        with patch("igpu_recorder.ffmpeg.run_probe_command") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg", "-filters"],
                returncode=0,
                stdout=sample_filters,
                stderr="",
            )
            assert check_ddagrab_filter(dummy_ffmpeg) is True

    def test_check_ddagrab_filter_false(self, tmp_path: Path) -> None:
        dummy_ffmpeg = tmp_path / "ffmpeg.exe"
        sample_filters = """Filters:
  .. gdigrab           |->V       Grab Windows Desktop
"""
        with patch("igpu_recorder.ffmpeg.run_probe_command") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg", "-filters"],
                returncode=0,
                stdout=sample_filters,
                stderr="",
            )
            assert check_ddagrab_filter(dummy_ffmpeg) is False

    def test_run_probe_command_timeout(self) -> None:
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="test", timeout=1.0)),
            pytest.raises(HardwareProbeError, match="Command timed out"),
        ):
            run_probe_command(["ffmpeg", "-version"], timeout=1.0)

    def test_run_probe_command_os_error(self) -> None:
        with (
            patch("subprocess.run", side_effect=OSError("Access denied")),
            pytest.raises(HardwareProbeError, match="Execution error"),
        ):
            run_probe_command(["nonexistent_binary"])

    def test_find_executable_custom_path_file(self, tmp_path: Path) -> None:
        custom_exe = tmp_path / "my_ffmpeg.exe"
        custom_exe.write_text("fake binary")
        with patch("os.access", return_value=True):
            found = find_executable("ffmpeg", custom_exe)
            assert found == custom_exe.resolve()

    def test_find_executable_custom_path_dir(self, tmp_path: Path) -> None:
        custom_exe = tmp_path / "ffmpeg.exe"
        custom_exe.write_text("fake binary")
        with patch("os.access", return_value=True):
            found = find_executable("ffmpeg", tmp_path)
            assert found == custom_exe.resolve()

    def test_find_executable_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            assert find_executable("nonexistent_binary_xyz_123") is None


class TestCapabilityProbingPipeline:
    """Test full probe_capabilities workflow under various hardware mocking scenarios."""

    def test_probe_capabilities_success_qsv_only(self, tmp_path: Path) -> None:
        ffmpeg_exe = tmp_path / "ffmpeg.exe"
        ffprobe_exe = tmp_path / "ffprobe.exe"
        ffmpeg_exe.write_text("bin")
        ffprobe_exe.write_text("bin")

        with (
            patch("igpu_recorder.ffmpeg.find_executable", side_effect=[ffmpeg_exe, ffprobe_exe]),
            patch("igpu_recorder.ffmpeg.run_probe_command") as mock_run,
            patch("igpu_recorder.ffmpeg.check_ddagrab_filter", return_value=True),
            patch(
                "igpu_recorder.ffmpeg.detect_available_encoders",
                return_value=("h264_qsv", "h264_amf"),
            ),
            patch("igpu_recorder.ffmpeg.verify_qsv_initialization", return_value=True),
            patch("igpu_recorder.ffmpeg.verify_amf_initialization", return_value=False),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg", "-version"],
                returncode=0,
                stdout="ffmpeg version N-120037-g36c8eef42c-20250625",
                stderr="",
            )

            caps = probe_capabilities()
            assert caps.ffmpeg_version == "N-120037-g36c8eef42c-20250625"
            assert caps.has_ddagrab is True
            assert caps.verified_backends == (HardwareBackend.QSV,)
            assert caps.primary_backend == HardwareBackend.QSV
            assert caps.is_recording_supported is True

    def test_probe_capabilities_no_usable_hardware(self, tmp_path: Path) -> None:
        ffmpeg_exe = tmp_path / "ffmpeg.exe"
        ffprobe_exe = tmp_path / "ffprobe.exe"
        ffmpeg_exe.write_text("bin")
        ffprobe_exe.write_text("bin")

        with (
            patch("igpu_recorder.ffmpeg.find_executable", side_effect=[ffmpeg_exe, ffprobe_exe]),
            patch("igpu_recorder.ffmpeg.run_probe_command") as mock_run,
            patch("igpu_recorder.ffmpeg.check_ddagrab_filter", return_value=True),
            patch(
                "igpu_recorder.ffmpeg.detect_available_encoders",
                return_value=("h264_qsv", "h264_amf"),
            ),
            patch("igpu_recorder.ffmpeg.verify_qsv_initialization", return_value=False),
            patch("igpu_recorder.ffmpeg.verify_amf_initialization", return_value=False),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg", "-version"],
                returncode=0,
                stdout="ffmpeg version 7.1",
                stderr="",
            )

            caps = probe_capabilities()
            assert caps.verified_backends == ()
            assert caps.primary_backend is None
            assert caps.is_recording_supported is False

    def test_probe_capabilities_missing_ffmpeg(self) -> None:
        with (
            patch("igpu_recorder.ffmpeg.find_executable", return_value=None),
            pytest.raises(HardwareProbeError, match="FFmpeg executable not found"),
        ):
            probe_capabilities()

    def test_probe_capabilities_missing_ffprobe(self, tmp_path: Path) -> None:
        ffmpeg_exe = tmp_path / "ffmpeg.exe"
        ffmpeg_exe.write_text("bin")
        with (
            patch("igpu_recorder.ffmpeg.find_executable", side_effect=[ffmpeg_exe, None]),
            pytest.raises(HardwareProbeError, match="ffprobe executable not found"),
        ):
            probe_capabilities()


class TestRealEnvironmentProbing:
    """Integration probe tests on the actual Windows host environment."""

    def test_real_system_probing(self) -> None:
        # On this Windows machine with FFmpeg installed, probe_capabilities must succeed
        caps = probe_capabilities()
        assert caps.ffmpeg_path.is_file()
        assert caps.ffprobe_path.is_file()
        assert caps.ffmpeg_version != "unknown"
        assert caps.has_ddagrab is True
        # On this Intel machine, QSV should be verified
        assert HardwareBackend.QSV in caps.verified_backends
        assert caps.is_recording_supported is True
