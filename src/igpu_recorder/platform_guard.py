"""Platform verification and Windows-specific system guards."""

import platform
import sys

from igpu_recorder.exceptions import PlatformNotSupportedError


def is_windows() -> bool:
    """Check if current operating system is Windows."""
    return sys.platform == "win32" or platform.system() == "Windows"


def assert_windows_platform() -> None:
    """Enforce that the application is running on Windows.

    Raises:
        PlatformNotSupportedError: If running on non-Windows platforms.
    """
    if not is_windows():
        msg = (
            f"iGPU Recorder requires Microsoft Windows (10/11) with Desktop Duplication support. "
            f"Detected OS: {platform.system()} ({sys.platform})."
        )
        raise PlatformNotSupportedError(msg)
