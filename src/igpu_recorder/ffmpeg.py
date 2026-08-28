"""FFmpeg capability probing, hardware backend modeling, and command builder.

Provides deterministic hardware discovery and command generation for iGPU screen recording.
"""

from __future__ import annotations

import enum
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from igpu_recorder.exceptions import HardwareProbeError
from igpu_recorder.logging import get_logger

logger = get_logger("ffmpeg")

# Timeout in seconds for capability probe subprocess commands
PROBE_TIMEOUT_SECONDS = 5.0


class HardwareBackend(enum.Enum):
    """Supported hardware acceleration backends."""

    QSV = "qsv"
    AMF = "amf"

    @property
    def encoder_name(self) -> str:
        """FFmpeg encoder codec name."""
        match self:
            case HardwareBackend.QSV:
                return "h264_qsv"
            case HardwareBackend.AMF:
                return "h264_amf"


class Resolution(enum.Enum):
    """Supported video resolutions."""

    R720P = "720p"
    R1080P = "1080p"

    @property
    def dimensions(self) -> tuple[int, int]:
        """Width and height in pixels."""
        match self:
            case Resolution.R720P:
                return (1280, 720)
            case Resolution.R1080P:
                return (1920, 1080)

    @property
    def width(self) -> int:
        return self.dimensions[0]

    @property
    def height(self) -> int:
        return self.dimensions[1]


class FrameRate(enum.Enum):
    """Supported recording frame rates."""

    FPS30 = 30
    FPS60 = 60


@dataclass(frozen=True)
class RecordingProfile:
    """Immutable recording configuration profile."""

    resolution: Resolution
    fps: FrameRate
    backend: HardwareBackend
    output_path: Path
    display_index: int = 0
    draw_mouse: bool = True
    global_quality: int = 23

    def __post_init__(self) -> None:
        """Validate profile parameters."""
        if not isinstance(self.resolution, Resolution):
            raise TypeError(f"Invalid resolution: {self.resolution}")
        if not isinstance(self.fps, FrameRate):
            raise TypeError(f"Invalid frame rate: {self.fps}")
        if not isinstance(self.backend, HardwareBackend):
            raise TypeError(f"Invalid backend: {self.backend}")
        if not isinstance(self.output_path, Path):
            raise TypeError(f"Invalid output path: {self.output_path}")


@dataclass(frozen=True)
class FFmpegCapabilities:
    """Result of FFmpeg executable and hardware capability probing."""

    ffmpeg_path: Path
    ffprobe_path: Path
    ffmpeg_version: str
    has_ddagrab: bool
    available_encoders: tuple[str, ...]
    verified_backends: tuple[HardwareBackend, ...]

    @property
    def is_recording_supported(self) -> bool:
        """Whether a usable hardware backend and ddagrab are present."""
        return self.has_ddagrab and len(self.verified_backends) > 0

    @property
    def primary_backend(self) -> HardwareBackend | None:
        """The primary/preferred verified hardware backend."""
        return self.verified_backends[0] if self.verified_backends else None


def find_executable(name: str, custom_path: Path | str | None = None) -> Path | None:
    """Locate an executable on PATH or at a specified custom path.

    Args:
        name: Executable name (e.g. 'ffmpeg', 'ffprobe').
        custom_path: Optional explicit path or directory to check first.

    Returns:
        Path to resolved executable or None if not found.
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()
        if p.is_dir():
            candidate = p / (f"{name}.exe" if os.name == "nt" else name)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()

    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    return None


def run_probe_command(
    args: list[str],
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Execute a probe subprocess safely with bounded timeout and captured stderr/stdout.

    Args:
        args: List of command arguments.
        timeout: Maximum seconds to wait.

    Returns:
        CompletedProcess instance containing stdout, stderr, and returncode.

    Raises:
        HardwareProbeError: If timeout or process execution error occurs.
    """
    logger.debug("Executing probe command: %s", args)
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Prevent console window flashing on Windows
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Probe command timed out after %s seconds: %s", timeout, args)
        raise HardwareProbeError(f"Command timed out after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        logger.error("Failed to execute probe command: %s", exc)
        raise HardwareProbeError(f"Execution error for {' '.join(args)}: {exc}") from exc


def parse_ffmpeg_version(version_output: str) -> str:
    """Extract version string from `ffmpeg -version` output."""
    match = re.search(r"ffmpeg\s+version\s+([^\s]+)", version_output, re.IGNORECASE)
    if match:
        return match.group(1)
    # Fallback to the first line if regex fails
    first_line = version_output.strip().splitlines()[0] if version_output.strip() else "unknown"
    return first_line


def check_ddagrab_filter(ffmpeg_path: Path) -> bool:
    """Check if the FFmpeg build includes the `ddagrab` filter."""
    result = run_probe_command([str(ffmpeg_path), "-hide_banner", "-filters"])
    if result.returncode != 0:
        logger.warning("ffmpeg -filters returned exit code %d", result.returncode)
        return False
    return "ddagrab" in result.stdout


def detect_available_encoders(ffmpeg_path: Path) -> tuple[str, ...]:
    """Query available encoders from FFmpeg."""
    result = run_probe_command([str(ffmpeg_path), "-hide_banner", "-encoders"])
    if result.returncode != 0:
        logger.warning("ffmpeg -encoders returned exit code %d", result.returncode)
        return ()

    encoders: list[str] = []
    # Output lines typically match: " V..... h264_qsv  Intel Quick Sync Video acceleration"
    for line in result.stdout.splitlines():
        line_strip = line.strip()
        if not line_strip or line_strip.startswith("Encoders:") or line_strip.startswith("---"):
            continue
        parts = line_strip.split(maxsplit=2)
        if len(parts) >= 2:
            codec_flags, codec_name = parts[0], parts[1]
            if "V" in codec_flags:  # Video encoder
                encoders.append(codec_name)

    return tuple(encoders)


def verify_qsv_initialization(ffmpeg_path: Path) -> bool:
    """Probe whether Intel QSV (h264_qsv) can actually initialize on this hardware.

    We test synthetic frame encoding through D3D11VA + QSV device derive.
    """
    cmd = [
        str(ffmpeg_path),
        "-hide_banner",
        "-init_hw_device",
        "d3d11va=d3d11",
        "-init_hw_device",
        "qsv=qsv@d3d11",
        "-filter_hw_device",
        "qsv",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=0.1:size=1920x1080:rate=30",
        "-vf",
        "format=nv12,hwupload=extra_hw_frames=64",
        "-c:v",
        "h264_qsv",
        "-f",
        "null",
        "-",
    ]
    try:
        res = run_probe_command(cmd, timeout=PROBE_TIMEOUT_SECONDS)
        if res.returncode == 0:
            logger.info("Intel QSV hardware initialization verified.")
            return True
        logger.debug(
            "QSV initialization failed with code %d. stderr:\n%s",
            res.returncode,
            res.stderr,
        )
        return False
    except HardwareProbeError as exc:
        logger.debug("QSV probe exception: %s", exc)
        return False


def verify_amf_initialization(ffmpeg_path: Path) -> bool:
    """Probe whether AMD AMF (h264_amf) can actually initialize on this hardware."""
    cmd = [
        str(ffmpeg_path),
        "-hide_banner",
        "-init_hw_device",
        "d3d11va=d3d11",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=0.1:size=1920x1080:rate=30",
        "-c:v",
        "h264_amf",
        "-f",
        "null",
        "-",
    ]
    try:
        res = run_probe_command(cmd, timeout=PROBE_TIMEOUT_SECONDS)
        if res.returncode == 0:
            logger.info("AMD AMF hardware initialization verified.")
            return True
        logger.debug(
            "AMF initialization failed with code %d. stderr:\n%s",
            res.returncode,
            res.stderr,
        )
        return False
    except HardwareProbeError as exc:
        logger.debug("AMF probe exception: %s", exc)
        return False


def probe_capabilities(
    ffmpeg_custom_path: Path | str | None = None,
    ffprobe_custom_path: Path | str | None = None,
) -> FFmpegCapabilities:
    """Perform a full discovery and capability verification probe.

    Args:
        ffmpeg_custom_path: Optional explicit path for ffmpeg executable.
        ffprobe_custom_path: Optional explicit path for ffprobe executable.

    Returns:
        FFmpegCapabilities dataclass.

    Raises:
        HardwareProbeError: If required executables are missing or non-functional.
    """
    logger.info("Probing FFmpeg capabilities...")

    ffmpeg_path = find_executable("ffmpeg", ffmpeg_custom_path)
    if not ffmpeg_path:
        raise HardwareProbeError("FFmpeg executable not found on PATH or custom path.")

    ffprobe_path = find_executable("ffprobe", ffprobe_custom_path)
    if not ffprobe_path:
        raise HardwareProbeError("ffprobe executable not found on PATH or custom path.")

    # Read FFmpeg version
    version_res = run_probe_command([str(ffmpeg_path), "-version"])
    if version_res.returncode != 0:
        raise HardwareProbeError(f"FFmpeg failed to report version (code {version_res.returncode})")
    version_str = parse_ffmpeg_version(version_res.stdout)
    logger.info("Discovered FFmpeg version: %s at %s", version_str, ffmpeg_path)

    # Detect ddagrab
    has_ddagrab = check_ddagrab_filter(ffmpeg_path)
    logger.info("ddagrab capture filter available: %s", has_ddagrab)

    # Detect listed encoders
    encoders = detect_available_encoders(ffmpeg_path)
    logger.debug("Found %d video encoders in FFmpeg", len(encoders))

    # Verify backends
    verified: list[HardwareBackend] = []

    if "h264_qsv" in encoders:
        if verify_qsv_initialization(ffmpeg_path):
            verified.append(HardwareBackend.QSV)
        else:
            logger.info("h264_qsv is listed in FFmpeg build but failed hardware initialization.")

    if "h264_amf" in encoders:
        if verify_amf_initialization(ffmpeg_path):
            verified.append(HardwareBackend.AMF)
        else:
            logger.info("h264_amf is listed in FFmpeg build but failed hardware initialization.")

    caps = FFmpegCapabilities(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        ffmpeg_version=version_str,
        has_ddagrab=has_ddagrab,
        available_encoders=encoders,
        verified_backends=tuple(verified),
    )

    if not caps.is_recording_supported:
        logger.warning(
            "Recording capability check failed: ddagrab=%s, verified_backends=%s",
            caps.has_ddagrab,
            [b.value for b in caps.verified_backends],
        )

    return caps


def build_recording_args(
    ffmpeg_path: Path | str,
    profile: RecordingProfile,
) -> list[str]:
    """Construct deterministic FFmpeg argument list for recording without shell concatenation.

    Args:
        ffmpeg_path: Path to FFmpeg binary.
        profile: Recording configuration profile.

    Returns:
        List of command-line argument strings.

    Raises:
        HardwareProbeError: If backend configuration is unsupported.
    """
    fps_val = profile.fps.value
    draw_mouse_val = 1 if profile.draw_mouse else 0
    display_idx = profile.display_index
    output_file = str(Path(profile.output_path).resolve())

    args: list[str] = [str(ffmpeg_path), "-hide_banner", "-y"]

    match profile.backend:
        case HardwareBackend.QSV:
            # Winning Phase 0 graph for Intel QSV
            args.extend(
                [
                    "-init_hw_device",
                    "d3d11va=d3d11",
                    "-init_hw_device",
                    "qsv=qsv@d3d11",
                    "-filter_hw_device",
                    "qsv",
                    "-f",
                    "lavfi",
                    "-i",
                    f"ddagrab=output_idx={display_idx}:draw_mouse={draw_mouse_val}:framerate={fps_val}",
                ]
            )

            # Filter graph
            if profile.resolution == Resolution.R1080P:
                # Direct format conversion in QSV without downscale
                vf = "hwmap=derive_device=qsv,scale_qsv=format=nv12"
            else:
                # 720p scaling
                vf = (
                    f"hwmap=derive_device=qsv,"
                    f"scale_qsv=w={profile.resolution.width}:h={profile.resolution.height}:format=nv12"
                )

            args.extend(
                [
                    "-vf",
                    vf,
                    "-c:v",
                    "h264_qsv",
                    "-global_quality",
                    str(profile.global_quality),
                    "-fps_mode",
                    "cfr",
                    output_file,
                ]
            )

        case HardwareBackend.AMF:
            # AMD AMF recording graph
            args.extend(
                [
                    "-init_hw_device",
                    "d3d11va=d3d11",
                    "-f",
                    "lavfi",
                    "-i",
                    f"ddagrab=output_idx={display_idx}:draw_mouse={draw_mouse_val}:framerate={fps_val}",
                ]
            )

            if profile.resolution == Resolution.R1080P:
                vf = "format=nv12"
            else:
                vf = f"scale={profile.resolution.width}:{profile.resolution.height},format=nv12"

            args.extend(
                [
                    "-vf",
                    vf,
                    "-c:v",
                    "h264_amf",
                    "-quality",
                    "speed",
                    "-fps_mode",
                    "cfr",
                    output_file,
                ]
            )

        case _:
            raise HardwareProbeError(f"Unsupported hardware backend: {profile.backend}")

    return args
