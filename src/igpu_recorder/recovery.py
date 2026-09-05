"""Recovery routines for unfinalized recording sessions."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from igpu_recorder.logging import get_logger

logger = get_logger("recovery")


def attempt_headless_recovery(
    ffmpeg_path: Path, ffprobe_path: Path, temp_base_dir: Path | None = None
) -> None:
    """Scan for unfinalized sessions and attempt to finalize them headlessly.

    Args:
        ffmpeg_path: Path to FFmpeg executable.
        ffprobe_path: Path to FFprobe executable.
        temp_base_dir: Base directory for temp files, defaults to system temp.
    """
    base_dir = temp_base_dir or Path(tempfile.gettempdir())
    if not base_dir.exists():
        return

    logger.info("Scanning for unfinalized sessions in %s...", base_dir)

    for entry in os.scandir(base_dir):
        if entry.is_dir() and entry.name.startswith("igpu_session_"):
            temp_dir = Path(entry.path)
            metadata_path = temp_dir / "session_metadata.json"
            if not metadata_path.exists():
                continue

            try:
                _recover_session(temp_dir, metadata_path, ffmpeg_path, ffprobe_path)
            except Exception as exc:
                logger.error("Failed to recover session in %s: %s", temp_dir, exc)


def _recover_session(
    temp_dir: Path, metadata_path: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    """Attempt to recover a single session given its temp dir and metadata."""
    logger.info("Found unfinalized session in %s. Attempting recovery...", temp_dir)

    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    session_id = metadata.get("session_id")
    output_target = metadata.get("output_target")

    if not session_id or not output_target:
        logger.warning("Invalid metadata in %s. Skipping recovery.", metadata_path)
        return

    # To avoid cyclic imports, we can construct the Finalizer manually or use the factory
    from igpu_recorder.ffmpeg import FrameRate, Resolution
    from igpu_recorder.finalizer import Finalizer
    from igpu_recorder.session import RecordingSession, SessionState, SegmentInfo

    finalizer = Finalizer(ffmpeg_path, ffprobe_path)

    # Reconstruct a mock snapshot to feed to the finalizer
    from igpu_recorder.session import SessionSnapshot
    
    segments = []
    # Discover segment files
    for entry in os.scandir(temp_dir):
        if entry.is_file() and entry.name.startswith("segment_") and entry.name.endswith(".mp4"):
            stat = entry.stat()
            if stat.st_size >= 1024:
                idx_str = entry.name.replace("segment_", "").replace(".mp4", "")
                try:
                    idx = int(idx_str)
                    segments.append(
                        SegmentInfo(
                            index=idx,
                            path=Path(entry.path),
                            start_time=0.0,
                            size_bytes=stat.st_size,
                            is_valid=True,
                        )
                    )
                except ValueError:
                    pass

    segments.sort(key=lambda s: s.index)

    if not segments:
        logger.warning("No valid segments found in %s. Cleaning up...", temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    resolution = Resolution(metadata.get("resolution", "1080p"))
    fps = FrameRate(metadata.get("fps", 60))
    from igpu_recorder.ffmpeg import HardwareBackend
    backend = HardwareBackend(metadata.get("backend", "auto"))

    class MockSession:
        def __init__(self):
            self.session_id = session_id
            self.output_target = Path(output_target)
            self.resolution = resolution
            self.fps = fps
            self.backend = backend
            self.temp_dir = temp_dir
            self.snapshot = SessionSnapshot(
                session_id=session_id,
                state=SessionState.STOPPED,
                resolution=resolution,
                fps=fps,
                backend=backend,
                output_target=Path(output_target),
                temp_dir=temp_dir,
                segments=tuple(segments),
                current_segment_index=len(segments),
                created_at=0.0,
                is_active=False,
            )

        def cleanup_temp_dir(self, force: bool = False):
            if force and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                logger.info("Cleaned up recovered temp dir %s", self.temp_dir)
                
        def get_snapshot(self):
            return self.snapshot

    mock_session = MockSession()
    logger.info("Recovering %d segments for session %s...", len(segments), session_id)

    # Generate output path
    timestamp_str = session_id.replace("session_", "iGPU-Recorder_Recovered_")
    output_path = Path(output_target).parent / f"{timestamp_str}.mp4"

    try:
        finalizer.finalize_session(mock_session, custom_output_path=output_path)
        logger.info("Successfully recovered session to %s", output_path)
    except Exception as exc:
        logger.error("Finalization failed during recovery: %s", exc)
        raise
