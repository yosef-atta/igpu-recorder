"""Recording process controller.

Manages the lifecycle of an FFmpeg recording subprocess:
- Safe startup and early failure detection
- Graceful shutdown via stdin 'q' commands
- Bounded timeout with forced termination fallback
- Prevention of multiple simultaneous recorder processes
- Capturing exit codes and diagnostic stderr
- Background exit detection and typed status reporting
"""

from __future__ import annotations

import enum
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from igpu_recorder.exceptions import RecordingProcessError
from igpu_recorder.ffmpeg import build_recording_args
from igpu_recorder.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from igpu_recorder.ffmpeg import RecordingProfile

logger = get_logger("process_controller")

# Default timeouts in seconds
DEFAULT_STARTUP_CHECK_TIMEOUT = 0.5
DEFAULT_GRACEFUL_STOP_TIMEOUT = 5.0
DEFAULT_KILL_TIMEOUT = 2.0
DEFAULT_STDERR_RINGBUFFER_LINES = 100


class ProcessState(enum.Enum):
    """Lifecycle states of the recorder process controller."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"


@dataclass(frozen=True)
class ProcessStatus:
    """Typed snapshot of the recording process status."""

    state: ProcessState
    is_alive: bool
    pid: int | None
    exit_code: int | None
    unexpected_exit: bool
    stderr_tail: str


class ProcessController:
    """Controls the FFmpeg subprocess lifecycle safely and deterministically."""

    def __init__(
        self,
        startup_timeout: float = DEFAULT_STARTUP_CHECK_TIMEOUT,
        stop_timeout: float = DEFAULT_GRACEFUL_STOP_TIMEOUT,
        kill_timeout: float = DEFAULT_KILL_TIMEOUT,
        stderr_buffer_lines: int = DEFAULT_STDERR_RINGBUFFER_LINES,
    ) -> None:
        self._startup_timeout = startup_timeout
        self._stop_timeout = stop_timeout
        self._kill_timeout = kill_timeout
        self._stderr_buffer_lines = stderr_buffer_lines

        self._state = ProcessState.STOPPED
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._expected_stop = False
        self._exit_code: int | None = None

        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    @property
    def state(self) -> ProcessState:
        """Current process controller state."""
        with self._lock:
            self._refresh_state_locked()
            return self._state

    def get_status(self) -> ProcessStatus:
        """Return a snapshot of current process status."""
        with self._lock:
            self._refresh_state_locked()
            is_alive = self._state in (
                ProcessState.RUNNING,
                ProcessState.STARTING,
                ProcessState.STOPPING,
            )
            pid = self._process.pid if self._process else None
            unexpected = bool(
                self._state == ProcessState.EXITED
                and not self._expected_stop
                and self._exit_code != 0
            )
            stderr_tail = "".join(self._stderr_lines[-self._stderr_buffer_lines :])
            return ProcessStatus(
                state=self._state,
                is_alive=is_alive,
                pid=pid,
                exit_code=self._exit_code,
                unexpected_exit=unexpected,
                stderr_tail=stderr_tail,
            )

    def start(self, ffmpeg_path: Path | str, profile: RecordingProfile) -> None:
        """Start the recording subprocess.

        Args:
            ffmpeg_path: Path to FFmpeg executable.
            profile: Recording configuration profile.

        Raises:
            RecordingProcessError: If a process is already running or if startup fails.
        """
        with self._lock:
            self._refresh_state_locked()
            if self._state in (ProcessState.RUNNING, ProcessState.STARTING, ProcessState.STOPPING):
                raise RecordingProcessError("A recorder process is already active or stopping.")

            self._state = ProcessState.STARTING
            self._expected_stop = False
            self._exit_code = None
            self._stderr_lines = []

            cmd = build_recording_args(ffmpeg_path, profile)
            logger.info("Starting recorder process with command: %s", cmd)

            # Ensure parent output directory exists
            profile.output_path.parent.mkdir(parents=True, exist_ok=True)

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    creationflags=creationflags,
                )
            except (OSError, ValueError) as exc:
                self._state = ProcessState.STOPPED
                logger.error("Failed to spawn FFmpeg process: %s", exc)
                raise RecordingProcessError(f"Failed to spawn FFmpeg process: {exc}") from exc

            # Spawn background reader for stderr to prevent pipe buffer deadlocks
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(self._process,),
                name="FFmpegStderrDrain",
                daemon=True,
            )
            self._stderr_thread.start()

        # Check for immediate startup failure (outside the lock to not block status calls)
        time.sleep(self._startup_timeout)

        with self._lock:
            if self._process is not None:
                ret = self._process.poll()
                if ret is not None:
                    self._exit_code = ret
                    self._state = ProcessState.EXITED
                    stderr_tail = "".join(self._stderr_lines)
                    logger.error(
                        "FFmpeg process died immediately during startup (code %d). Stderr: %s",
                        ret,
                        stderr_tail,
                    )
                    err_msg = (
                        f"FFmpeg failed immediately on startup (code {ret}). "
                        f"Stderr: {stderr_tail.strip()}"
                    )
                    raise RecordingProcessError(err_msg)

            self._state = ProcessState.RUNNING
            pid_str = self._process.pid if self._process else None
            logger.info("Recorder process started successfully (PID: %s).", pid_str)

    def stop(self) -> int:
        """Gracefully stop the recording process, with fallback to terminate/kill.

        Sends 'q' via stdin, waits up to stop_timeout, then terminates if necessary.

        Returns:
            The exit code of the process.

        Raises:
            RecordingProcessError: If called when no process has been started.
        """
        with self._lock:
            self._refresh_state_locked()
            if self._process is None or self._state in (ProcessState.STOPPED, ProcessState.EXITED):
                logger.warning(
                    "Stop called but recorder process is not active (state: %s).",
                    self._state.value,
                )
                return self._exit_code if self._exit_code is not None else 0

            self._state = ProcessState.STOPPING
            self._expected_stop = True
            proc = self._process

        logger.info("Stopping recorder process gracefully (PID: %s)...", proc.pid)

        # Send 'q' to stdin to trigger clean MP4 finalization
        try:
            if proc.stdin:
                is_closed = getattr(proc.stdin, "closed", False)
                if not is_closed:
                    proc.stdin.write("q\n")
                    proc.stdin.flush()
                    proc.stdin.close()
        except (OSError, ValueError) as exc:
            logger.debug(
                "Failed to write 'q' to FFmpeg stdin (it may have already exited): %s",
                exc,
            )

        # Wait for graceful exit
        try:
            exit_code = proc.wait(timeout=self._stop_timeout)
            logger.info("Recorder process exited gracefully with code %d.", exit_code)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Recorder process did not exit within %s seconds. Attempting termination...",
                self._stop_timeout,
            )
            try:
                proc.terminate()
                exit_code = proc.wait(timeout=self._kill_timeout)
                logger.info("Recorder process terminated with code %d.", exit_code)
            except subprocess.TimeoutExpired:
                logger.error("Recorder process did not respond to terminate. Forcing kill...")
                proc.kill()
                exit_code = proc.wait(timeout=self._kill_timeout)
                logger.info("Recorder process killed with code %d.", exit_code)

        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)

        with self._lock:
            self._exit_code = exit_code
            self._state = ProcessState.STOPPED
            self._process = None

        return exit_code

    def kill(self) -> int:
        """Immediately force-kill the recorder process."""
        with self._lock:
            self._refresh_state_locked()
            if self._process is None:
                return self._exit_code if self._exit_code is not None else 0

            self._expected_stop = True
            proc = self._process

        logger.warning("Forcibly killing recorder process (PID: %s)...", proc.pid)
        try:
            proc.kill()
            exit_code = proc.wait(timeout=self._kill_timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("Error killing recorder process: %s", exc)
            exit_code = -1

        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)

        with self._lock:
            self._exit_code = exit_code
            self._state = ProcessState.STOPPED
            self._process = None

        return exit_code

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Drain stderr into a memory buffer to prevent pipe blocking."""
        try:
            if proc.stderr:
                for line in proc.stderr:
                    with self._lock:
                        self._stderr_lines.append(line)
                        if len(self._stderr_lines) > self._stderr_buffer_lines * 2:
                            self._stderr_lines = self._stderr_lines[-self._stderr_buffer_lines :]
        except (OSError, ValueError):
            pass
        finally:
            if proc.stderr and hasattr(proc.stderr, "closed") and not proc.stderr.closed:
                try:
                    if hasattr(proc.stderr, "close"):
                        proc.stderr.close()
                except OSError:
                    pass

    def _refresh_state_locked(self) -> None:
        """Update state based on process poll without acquiring lock again."""
        if self._process is not None:
            poll_ret = self._process.poll()
            if poll_ret is not None:
                self._exit_code = poll_ret
                if self._state not in (ProcessState.STOPPING, ProcessState.STOPPED):
                    self._state = ProcessState.EXITED
                    if not self._expected_stop:
                        logger.warning(
                            "Unexpected FFmpeg process termination detected (exit code %d).",
                            poll_ret,
                        )
