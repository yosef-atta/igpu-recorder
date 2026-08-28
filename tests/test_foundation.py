"""Unit tests for version, logging, platform guard, and main entrypoint."""

import logging
from io import StringIO
from unittest.mock import patch

import pytest

from igpu_recorder.exceptions import (
    HardwareProbeError,
    PlatformNotSupportedError,
)
from igpu_recorder.logging import get_logger, setup_logging
from igpu_recorder.main import entrypoint
from igpu_recorder.platform_guard import assert_windows_platform, is_windows
from igpu_recorder.version import APP_NAME, __version__


def test_version_constants() -> None:
    """Test version string and application name."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0
    assert APP_NAME == "iGPU Recorder"


def test_logging_setup() -> None:
    """Test logger initialization and formatting output."""
    stream = StringIO()
    logger = setup_logging(level=logging.DEBUG, stream=stream)
    assert logger.name == "igpu_recorder"

    logger.debug("test debug message")
    output = stream.getvalue()
    assert "[DEBUG] [igpu_recorder] test debug message" in output

    sub_logger = get_logger("test_module")
    assert sub_logger.name == "igpu_recorder.test_module"


def test_platform_guard_windows() -> None:
    """Test platform guard under mocked Windows platform."""
    with patch("sys.platform", "win32"), patch("platform.system", return_value="Windows"):
        assert is_windows() is True
        # Should not raise
        assert_windows_platform()


def test_platform_guard_non_windows() -> None:
    """Test platform guard under mocked Linux/Darwin platform."""
    with patch("sys.platform", "linux"), patch("platform.system", return_value="Linux"):
        assert is_windows() is False
        with pytest.raises(PlatformNotSupportedError) as exc_info:
            assert_windows_platform()
        assert "requires Microsoft Windows" in str(exc_info.value)


def test_entrypoint_success() -> None:
    """Test successful entrypoint execution."""
    with patch("igpu_recorder.main.assert_windows_platform"):
        exit_code = entrypoint([])
        assert exit_code == 0


def test_entrypoint_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Test entrypoint --version flag."""
    with patch("igpu_recorder.main.assert_windows_platform"):
        exit_code = entrypoint(["--version"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert f"{APP_NAME} v{__version__}" in captured.out


def test_entrypoint_unsupported_platform() -> None:
    """Test entrypoint handling of PlatformNotSupportedError."""
    with patch(
        "igpu_recorder.main.assert_windows_platform",
        side_effect=PlatformNotSupportedError("Unsupported OS"),
    ):
        exit_code = entrypoint([])
        assert exit_code == 1


def test_entrypoint_app_error() -> None:
    """Test entrypoint handling of custom IGpuRecorderError."""
    with patch(
        "igpu_recorder.main.assert_windows_platform",
        side_effect=HardwareProbeError("Probe failed"),
    ):
        exit_code = entrypoint([])
        assert exit_code == 2


def test_entrypoint_unexpected_error() -> None:
    """Test entrypoint handling of unexpected exception."""
    with patch(
        "igpu_recorder.main.assert_windows_platform",
        side_effect=RuntimeError("Unexpected boom"),
    ):
        exit_code = entrypoint([])
        assert exit_code == 3
