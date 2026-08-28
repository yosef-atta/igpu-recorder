"""Deterministic application version source and metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("igpu-recorder")
except PackageNotFoundError:
    __version__ = "0.1.0"

APP_NAME = "iGPU Recorder"
