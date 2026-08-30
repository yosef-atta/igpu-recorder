"""iGPU Recorder package."""

from igpu_recorder.finalizer import FinalizationResult, Finalizer, VideoStreamMetadata
from igpu_recorder.preview import (
    BasePreviewCapture,
    GDIPreviewCapture,
    PreviewConfig,
    PreviewController,
    PreviewFrame,
    PreviewMode,
)
from igpu_recorder.process_controller import ProcessController, ProcessState, ProcessStatus
from igpu_recorder.session import RecordingSession, SegmentInfo, SessionSnapshot, SessionState
from igpu_recorder.version import __version__

__all__ = [
    "BasePreviewCapture",
    "FinalizationResult",
    "Finalizer",
    "GDIPreviewCapture",
    "PreviewConfig",
    "PreviewController",
    "PreviewFrame",
    "PreviewMode",
    "ProcessController",
    "ProcessState",
    "ProcessStatus",
    "RecordingSession",
    "SegmentInfo",
    "SessionSnapshot",
    "SessionState",
    "VideoStreamMetadata",
    "__version__",
]


