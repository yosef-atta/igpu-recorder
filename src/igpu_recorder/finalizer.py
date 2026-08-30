"""MP4 finalization and stream-copy concatenation layer.

Provides:
- VideoStreamMetadata: Dataclass holding verified container/stream properties from ffprobe.
- probe_video_file: ffprobe validation and metadata extraction.
- generate_safe_destination_path: Collision resolution avoiding silent overwrites.
- write_concat_manifest: Deterministic concat demuxer manifest file generator with safe escaping.
- Finalizer: Orchestrator that concatenates segments via FFmpeg stream copy, applies MP4 faststart,
  validates the output against expected properties (resolution, codec, FPS, duration),
  handles cleanup only on success, and preserves recovery files on failure.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from igpu_recorder.exceptions import FinalizationError
from igpu_recorder.logging import get_logger
from igpu_recorder.session import SegmentInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from igpu_recorder.ffmpeg import FrameRate, Resolution
    from igpu_recorder.session import RecordingSession

logger = get_logger("finalizer")

# Default command timeout for finalizer operations
DEFAULT_FINALIZER_TIMEOUT = 30.0


class FinalizerCommandRunnerProtocol(Protocol):
    """Structural protocol for running subprocess commands in finalizer."""

    def __call__(
        self,
        args: list[str],
        timeout: float = ...,
    ) -> subprocess.CompletedProcess[str]: ...


def default_command_runner(
    args: list[str],
    timeout: float = DEFAULT_FINALIZER_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Execute subprocess safely without shell and with captured output."""
    logger.debug("Executing finalizer command: %s", args)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@dataclass(frozen=True)
class VideoStreamMetadata:
    """Metadata for a video stream extracted by ffprobe."""

    codec_name: str
    width: int
    height: int
    fps: float
    duration: float
    num_frames: int | None = None


def parse_fps(r_frame_rate_str: str | None, avg_frame_rate_str: str | None) -> float:
    """Safely parse FFmpeg rational frame rate string (e.g. '60/1', '30000/1001') to float."""
    for rate_str in (r_frame_rate_str, avg_frame_rate_str):
        if not rate_str or rate_str in ("0/0", "0"):
            continue
        if "/" in rate_str:
            try:
                num, den = rate_str.split("/", 1)
                den_f = float(den)
                if den_f > 0:
                    return float(num) / den_f
            except (ValueError, ZeroDivisionError):
                continue
        else:
            try:
                val = float(rate_str)
                if val > 0:
                    return val
            except ValueError:
                continue
    return 0.0


def probe_video_file(
    ffprobe_path: Path | str,
    target_file: Path | str,
    runner: FinalizerCommandRunnerProtocol = default_command_runner,
    timeout: float = DEFAULT_FINALIZER_TIMEOUT,
) -> VideoStreamMetadata:
    """Run ffprobe on target file and parse stream/container metadata.

    Args:
        ffprobe_path: Path to ffprobe binary.
        target_file: Path to media file to probe.
        runner: Command runner callable.
        timeout: Timeout in seconds.

    Returns:
        VideoStreamMetadata dataclass.

    Raises:
        FinalizationError: If probing fails or stream data is missing/invalid.
    """
    file_path = Path(target_file).resolve()
    if not file_path.exists():
        raise FinalizationError(f"Target video file does not exist: {file_path}")

    cmd = [
        str(ffprobe_path),
        "-hide_banner",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-select_streams",
        "v:0",
        str(file_path),
    ]

    try:
        res = runner(cmd, timeout=timeout)
    except Exception as exc:
        raise FinalizationError(f"ffprobe execution failed for {file_path}: {exc}") from exc

    if res.returncode != 0:
        err_msg = res.stderr.strip()
        raise FinalizationError(
            f"ffprobe returned exit code {res.returncode} for {file_path}. Stderr: {err_msg}"
        )

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise FinalizationError(
            f"Failed to parse ffprobe JSON output for {file_path}: {exc}"
        ) from exc

    streams = data.get("streams", [])
    if not streams:
        raise FinalizationError(f"No video streams found in {file_path}")

    v_stream = streams[0]
    codec_name = v_stream.get("codec_name", "")
    width = int(v_stream.get("width", 0))
    height = int(v_stream.get("height", 0))

    fps = parse_fps(v_stream.get("r_frame_rate"), v_stream.get("avg_frame_rate"))

    # Duration can be in stream or format
    duration_str = v_stream.get("duration") or data.get("format", {}).get("duration", "0")
    try:
        duration = float(duration_str)
    except (ValueError, TypeError):
        duration = 0.0

    nb_frames_str = v_stream.get("nb_frames")
    nb_frames = int(nb_frames_str) if nb_frames_str and nb_frames_str.isdigit() else None

    return VideoStreamMetadata(
        codec_name=codec_name,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        num_frames=nb_frames,
    )


def escape_ffmpeg_concat_path(path: Path | str) -> str:
    r"""Safely escape path for FFmpeg concat demuxer manifest.

    Rules per FFmpeg documentation:
    - Backslashes and single quotes must be escaped with a backslash.
    - Forward slashes are preferred for paths.
    - Paths are enclosed in single quotes.
    Example: F:\temp\seg'1.mp4 -> 'F:/temp/seg\'1.mp4'
    """
    raw_str = str(Path(path).resolve()).replace("\\", "/")
    # Escape single quotes: ' -> \'
    escaped = raw_str.replace("'", r"\'")
    return f"file '{escaped}'"


def write_concat_manifest(
    segments: Sequence[Path | str | SegmentInfo],
    manifest_path: Path,
) -> Path:
    """Build deterministic concat input manifest file.

    Args:
        segments: Ordered list of segment paths or SegmentInfo objects.
        manifest_path: Path where the concat list text file should be created.

    Returns:
        Path to the created manifest file.

    Raises:
        FinalizationError: If segments list is empty.
    """
    if not segments:
        raise FinalizationError("Cannot create concat manifest: no segments provided.")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# FFmpeg concat demuxer manifest (deterministic order)"]

    for seg in segments:
        seg_path = seg.path if isinstance(seg, SegmentInfo) else Path(seg)
        line = escape_ffmpeg_concat_path(seg_path)
        lines.append(line)

    content = "\n".join(lines) + "\n"
    manifest_path.write_text(content, encoding="utf-8")
    logger.debug("Wrote concat manifest to %s with %d entries", manifest_path, len(segments))
    return manifest_path


def generate_safe_destination_path(
    destination: Path | str,
    output_dir: Path | str | None = None,
) -> Path:
    """Generate a destination path that never collides with or overwrites an existing file.

    If destination does not exist, returns destination.
    If destination exists, appends numeric suffix: (1), (2), etc. before suffix.

    Args:
        destination: Desired destination file path or filename.
        output_dir: Optional base directory if destination is relative.

    Returns:
        Resolved non-colliding destination Path.
    """
    dest = Path(destination)
    if not dest.is_absolute():
        dest = Path(output_dir) / dest if output_dir else dest.resolve()

    dest = dest.resolve()
    if not dest.exists():
        return dest

    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent

    # Check if there is an existing collision suffix pattern like " (1)"
    match = re.search(r"^(.*?)\s*\((\d+)\)$", stem)
    if match:
        base_name = match.group(1).rstrip()
        counter = int(match.group(2)) + 1
    else:
        base_name = stem
        counter = 1

    while True:
        candidate = parent / f"{base_name} ({counter}){suffix}"
        if not candidate.exists():
            logger.info("Destination collided. Generated safe filename: %s", candidate.name)
            return candidate
        counter += 1


@dataclass(frozen=True)
class FinalizationResult:
    """Result returned upon successful finalization."""

    output_path: Path
    duration: float
    resolution: Resolution
    fps: FrameRate
    codec: str
    num_segments: int
    size_bytes: int


class Finalizer:
    """Assembles and validates the finished recording from session segments."""

    def __init__(
        self,
        ffmpeg_path: Path | str,
        ffprobe_path: Path | str,
        command_runner: FinalizerCommandRunnerProtocol = default_command_runner,
    ) -> None:
        self._ffmpeg_path = Path(ffmpeg_path)
        self._ffprobe_path = Path(ffprobe_path)
        self._runner = command_runner

    def finalize_session(
        self,
        session: RecordingSession,
        custom_output_path: Path | str | None = None,
        apply_faststart: bool = True,
    ) -> FinalizationResult:
        """Finalize a RecordingSession into a validated MP4 file.

        Args:
            session: The RecordingSession to finalize.
            custom_output_path: Optional custom output path overriding session.output_target.
            apply_faststart: Whether to relocate MP4 moov atom to start (+faststart).

        Returns:
            FinalizationResult dataclass.

        Raises:
            FinalizationError: If finalization or validation fails, including recovery path.
        """
        snapshot = session.get_snapshot()
        segments = snapshot.segments
        temp_dir = snapshot.temp_dir

        if not segments:
            raise FinalizationError(
                f"Cannot finalize session {snapshot.session_id}: No completed segments found. "
                f"Recovery path: {temp_dir}"
            )

        # 1. Determine collision-safe target path
        target_candidate = (
            Path(custom_output_path).resolve()
            if custom_output_path
            else snapshot.output_target.resolve()
        )
        safe_output_path = generate_safe_destination_path(target_candidate)

        logger.info(
            "Finalizing session [%s] (%d segment(s)) -> %s",
            snapshot.session_id,
            len(segments),
            safe_output_path,
        )

        try:
            # 2. Perform stream-copy assembly
            if len(segments) == 1:
                # Single segment optimization: stream copy / remux directly with faststart
                single_segment_path = segments[0].path
                if not single_segment_path.exists():
                    raise FinalizationError(
                        f"Single source segment missing: {single_segment_path}"
                    )

                self._remux_single_segment(
                    input_path=single_segment_path,
                    output_path=safe_output_path,
                    apply_faststart=apply_faststart,
                )
            else:
                # Multi-segment concat using concat demuxer
                manifest_path = temp_dir / "concat_list.txt"
                write_concat_manifest(segments, manifest_path)

                self._concat_segments(
                    manifest_path=manifest_path,
                    output_path=safe_output_path,
                    apply_faststart=apply_faststart,
                )

            # 3. Validate generated output file using ffprobe
            meta = self.validate_output(
                output_path=safe_output_path,
                expected_resolution=snapshot.resolution,
                expected_fps=snapshot.fps,
                expected_codec="h264",
            )

            # 4. Clean up temporary session directory only after successful validation
            session.cleanup_temp_dir(force=True)

            result = FinalizationResult(
                output_path=safe_output_path,
                duration=meta.duration,
                resolution=snapshot.resolution,
                fps=snapshot.fps,
                codec=meta.codec_name,
                num_segments=len(segments),
                size_bytes=safe_output_path.stat().st_size,
            )
            logger.info(
                "Successfully finalized session [%s] to %s",
                snapshot.session_id,
                safe_output_path,
            )
            return result

        except Exception as exc:
            # On failure: Preserve temporary files, do not delete them.
            logger.error(
                "Finalization failed for session [%s]: %s. Temporary files preserved at %s",
                snapshot.session_id,
                exc,
                temp_dir,
            )
            # If a partially written output file was created, clean it up
            if safe_output_path.exists():
                with contextlib.suppress(OSError):
                    safe_output_path.unlink()

            raise FinalizationError(
                f"Finalization failed: {exc}. Source segments preserved at: {temp_dir}"
            ) from exc

    def _remux_single_segment(
        self,
        input_path: Path,
        output_path: Path,
        apply_faststart: bool = True,
    ) -> None:
        """Stream copy a single segment to output destination with optional faststart."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd: list[str] = [
            str(self._ffmpeg_path),
            "-hide_banner",
            "-y",
            "-i",
            str(input_path.resolve()),
            "-c",
            "copy",
        ]
        if apply_faststart:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path.resolve()))

        res = self._runner(cmd)
        if res.returncode != 0:
            err = res.stderr.strip()
            raise FinalizationError(
                f"FFmpeg single-segment remux failed (exit code {res.returncode}): {err}"
            )

    def _concat_segments(
        self,
        manifest_path: Path,
        output_path: Path,
        apply_faststart: bool = True,
    ) -> None:
        """Concatenate segments from manifest using stream copy (-c copy) and optional faststart."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd: list[str] = [
            str(self._ffmpeg_path),
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest_path.resolve()),
            "-c",
            "copy",
        ]
        if apply_faststart:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path.resolve()))

        res = self._runner(cmd)
        if res.returncode != 0:
            err = res.stderr.strip()
            raise FinalizationError(
                f"FFmpeg concat failed (exit code {res.returncode}): {err}"
            )

    def validate_output(
        self,
        output_path: Path,
        expected_resolution: Resolution | None = None,
        expected_fps: FrameRate | None = None,
        expected_codec: str = "h264",
    ) -> VideoStreamMetadata:
        """Validate final MP4 using ffprobe.

        Verifies:
        - Output file exists and is non-empty.
        - Non-zero duration.
        - Expected codec (e.g. h264).
        - Expected resolution (width and height).
        - Expected FPS within reasonable tolerance (e.g. ±5 FPS).

        Args:
            output_path: Path to MP4 file.
            expected_resolution: Expected Resolution enum.
            expected_fps: Expected FrameRate enum.
            expected_codec: Expected codec name substring (default 'h264').

        Returns:
            VideoStreamMetadata.

        Raises:
            FinalizationError: If any validation checks fail.
        """
        if not output_path.exists():
            raise FinalizationError(f"Finalized output file does not exist: {output_path}")

        size = output_path.stat().st_size
        if size < 1024:
            raise FinalizationError(
                f"Finalized output file is suspiciously small or empty: {size} bytes"
            )

        meta = probe_video_file(self._ffprobe_path, output_path, runner=self._runner)

        # 1. Non-zero duration check
        if meta.duration <= 0.0:
            raise FinalizationError(
                f"Validation failed: Final video duration is zero ({meta.duration}s)."
            )

        # 2. Codec check
        if expected_codec and expected_codec.lower() not in meta.codec_name.lower():
            raise FinalizationError(
                f"Validation failed: Codec mismatch. Expected '{expected_codec}', "
                f"got '{meta.codec_name}'."
            )

        # 3. Resolution check
        if expected_resolution is not None:
            expected_w, expected_h = expected_resolution.dimensions
            if meta.width != expected_w or meta.height != expected_h:
                raise FinalizationError(
                    f"Validation failed: Resolution mismatch. Expected {expected_w}x{expected_h}, "
                    f"got {meta.width}x{meta.height}."
                )

        # 4. FPS check (with tolerance, allowing fractional rates like 29.97 / 59.94 / 30 / 60)
        if expected_fps is not None:
            expected_fps_val = float(expected_fps.value)
            if abs(meta.fps - expected_fps_val) > 5.0 and meta.fps > 0:
                raise FinalizationError(
                    f"Validation failed: FPS mismatch. Expected ~{expected_fps_val}, "
                    f"got {meta.fps:.2f}."
                )

        return meta
