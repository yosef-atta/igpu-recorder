"""Application-level exception hierarchy."""


class IGpuRecorderError(Exception):
    """Base exception for all iGPU Recorder errors."""


class PlatformNotSupportedError(IGpuRecorderError):
    """Raised when running on an unsupported operating system or architecture."""


class HardwareProbeError(IGpuRecorderError):
    """Raised when hardware encoder or capture device probing fails."""


class RecordingProcessError(IGpuRecorderError):
    """Raised when the recorder process encounters a runtime error."""


class FinalizationError(IGpuRecorderError):
    """Raised when MP4 segment concatenation/finalization fails."""


class InvalidConfigurationError(IGpuRecorderError):
    """Raised when user-specified or internal settings are invalid."""
