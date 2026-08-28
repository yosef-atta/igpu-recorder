"""Application entry point and exception boundary."""

import sys
from collections.abc import Sequence

from igpu_recorder.exceptions import (
    IGpuRecorderError,
    PlatformNotSupportedError,
)
from igpu_recorder.logging import setup_logging
from igpu_recorder.platform_guard import assert_windows_platform
from igpu_recorder.version import APP_NAME, __version__


def entrypoint(args: Sequence[str] | None = None) -> int:
    """Core execution logic wrapped by error handling boundary.

    Args:
        args: Command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    logger = setup_logging()
    logger.info("Starting %s v%s...", APP_NAME, __version__)

    try:
        assert_windows_platform()
        logger.info("Platform verified: Windows environment confirmed.")

        if args and "--version" in args:
            print(f"{APP_NAME} v{__version__}")
            return 0

        # Placeholder for main UI / application state initialization in later phases
        logger.info("Foundation initialized successfully.")
        return 0

    except PlatformNotSupportedError as exc:
        logger.critical("Unsupported platform: %s", exc)
        return 1
    except IGpuRecorderError as exc:
        logger.error("Application error: %s", exc)
        return 2
    except Exception as exc:
        logger.exception("Unexpected fatal crash: %s", exc)
        return 3


def main() -> None:
    """CLI script entry point."""
    sys.exit(entrypoint(sys.argv[1:]))


if __name__ == "__main__":
    main()
