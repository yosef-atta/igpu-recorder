"""Main Tkinter user interface for iGPU Recorder.

Implements the compact single-window UI specified in Phase 8 / PRD:
- Centered desktop preview region with aspect-ratio preservation
- Resolution selectors (720p, 1080p)
- Frame-rate selectors (30 FPS, 60 FPS)
- Output folder entry and Browse dialog
- Primary action button (Start Recording / CUT / Resume / Reset)
- Stop Recording button
- Status area displaying lifecycle states and file save results
- Full state machine synchronization
- Minimized-window detection suspending preview capture
- Non-blocking asynchronous background execution of all FFmpeg operations
"""

from __future__ import annotations

import contextlib
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

from igpu_recorder.ffmpeg import FrameRate, Resolution
from igpu_recorder.logging import get_logger
from igpu_recorder.preview import PreviewController, PreviewFrame, PreviewMode
from igpu_recorder.state_machine import (
    ApplicationState,
    ApplicationStateMachine,
    UIControlsState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger("ui")


def bgra_to_ppm(data: bytes, width: int, height: int) -> bytes:
    """Convert raw 32-bit BGRA image bytes into binary PPM (P6) format for Tkinter PhotoImage.

    Args:
        data: Raw BGRA byte buffer.
        width: Image pixel width.
        height: Image pixel height.

    Returns:
        Binary PPM P6 bytes.
    """
    ppm_data = bytearray(width * height * 3)
    # BGRA -> RGB mapping
    ppm_data[0::3] = data[2::4]
    ppm_data[1::3] = data[1::4]
    ppm_data[2::3] = data[0::4]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return bytes(header + ppm_data)


class MainWindow:
    """Main application window managing UI layout, state sync, and asynchronous operations."""

    def __init__(
        self,
        root: tk.Tk | None = None,
        state_machine: ApplicationStateMachine | None = None,
        preview_controller: PreviewController | None = None,
    ) -> None:
        self._owns_root = root is None
        self._root = root or tk.Tk()
        self._state_machine = state_machine or ApplicationStateMachine()
        self._preview_controller = preview_controller or PreviewController()

        self._async_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._is_closing = False

        # Preview rendering tracking
        self._last_frame_photo: tk.PhotoImage | None = None
        self._pending_frame: PreviewFrame | None = None
        self._frame_draw_scheduled = False
        self._preview_canvas_img_id: int | None = None

        # Minimized state tracking
        self._is_minimized = False

        self._setup_window()
        self._create_widgets()
        self._wire_state_machine()
        self._wire_preview_controller()
        self._bind_window_events()

        # Initial UI synchronization from current state
        initial_controls = self._state_machine.derive_ui_controls()
        self._apply_ui_controls(self._state_machine.state, initial_controls)

        logger.info("MainWindow initialized successfully.")

    @property
    def root(self) -> tk.Tk:
        """Return underlying Tk root window."""
        return self._root

    @property
    def state_machine(self) -> ApplicationStateMachine:
        """Return connected application state machine."""
        return self._state_machine

    @property
    def preview_controller(self) -> PreviewController:
        """Return connected preview controller."""
        return self._preview_controller

    def _setup_window(self) -> None:
        """Configure root window properties and styling."""
        self._root.title("iGPU Recorder")
        self._root.geometry("520x620")
        self._root.minsize(480, 580)

        # Style configuration
        style = ttk.Style(self._root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")

        style.configure("TLabel", font=("Segoe UI", 9))
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=6)

    def _create_widgets(self) -> None:
        """Build all UI components from top to bottom."""
        main_frame = ttk.Frame(self._root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Desktop Preview Area (Top, Centered)
        preview_group = ttk.LabelFrame(main_frame, text="Desktop Preview", padding=6)
        preview_group.pack(fill=tk.X, pady=(0, 10))

        preview_container = ttk.Frame(preview_group)
        preview_container.pack(fill=tk.X, expand=True)

        # 480x270 matches 16:9 aspect ratio
        self._preview_width = 480
        self._preview_height = 270
        self._preview_canvas = tk.Canvas(
            preview_container,
            width=self._preview_width,
            height=self._preview_height,
            bg="#181818",
            highlightthickness=1,
            highlightbackground="#333333",
        )
        self._preview_canvas.pack(anchor=tk.CENTER, pady=2)
        self._preview_placeholder_text_id = self._preview_canvas.create_text(
            self._preview_width // 2,
            self._preview_height // 2,
            text="Initializing Preview...",
            fill="#888888",
            font=("Segoe UI", 10),
        )

        # 2. Settings Container (Resolution, FPS, Output Folder)
        settings_group = ttk.LabelFrame(main_frame, text="Settings", padding=10)
        settings_group.pack(fill=tk.X, pady=(0, 10))

        # Resolution Selector (720p / 1080p)
        res_frame = ttk.Frame(settings_group)
        res_frame.pack(fill=tk.X, pady=3)
        ttk.Label(res_frame, text="Resolution:", width=14, anchor=tk.W).pack(side=tk.LEFT)

        self._res_var = tk.StringVar(value=self._state_machine.settings.resolution.value)
        self._res_720_radio = ttk.Radiobutton(
            res_frame,
            text="720p (1280x720)",
            value=Resolution.R720P.value,
            variable=self._res_var,
            command=self._on_resolution_changed,
        )
        self._res_720_radio.pack(side=tk.LEFT, padx=(0, 15))

        self._res_1080_radio = ttk.Radiobutton(
            res_frame,
            text="1080p (1920x1080)",
            value=Resolution.R1080P.value,
            variable=self._res_var,
            command=self._on_resolution_changed,
        )
        self._res_1080_radio.pack(side=tk.LEFT)

        # FPS Selector (30 FPS / 60 FPS)
        fps_frame = ttk.Frame(settings_group)
        fps_frame.pack(fill=tk.X, pady=3)
        ttk.Label(fps_frame, text="Frame Rate:", width=14, anchor=tk.W).pack(side=tk.LEFT)

        self._fps_var = tk.IntVar(value=self._state_machine.settings.fps.value)
        self._fps_30_radio = ttk.Radiobutton(
            fps_frame,
            text="30 FPS",
            value=FrameRate.FPS30.value,
            variable=self._fps_var,
            command=self._on_fps_changed,
        )
        self._fps_30_radio.pack(side=tk.LEFT, padx=(0, 15))

        self._fps_60_radio = ttk.Radiobutton(
            fps_frame,
            text="60 FPS",
            value=FrameRate.FPS60.value,
            variable=self._fps_var,
            command=self._on_fps_changed,
        )
        self._fps_60_radio.pack(side=tk.LEFT)

        # Output Folder Selector
        folder_frame = ttk.Frame(settings_group)
        folder_frame.pack(fill=tk.X, pady=3)
        ttk.Label(folder_frame, text="Output Folder:", width=14, anchor=tk.W).pack(side=tk.LEFT)

        self._output_dir_var = tk.StringVar(value=str(self._state_machine.settings.output_dir))
        self._output_entry = ttk.Entry(folder_frame, textvariable=self._output_dir_var)
        self._output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._output_entry.bind("<FocusOut>", self._on_output_entry_changed)
        self._output_entry.bind("<Return>", self._on_output_entry_changed)

        self._browse_button = ttk.Button(
            folder_frame,
            text="Browse...",
            command=self._on_browse_output_dir,
            width=10,
        )
        self._browse_button.pack(side=tk.RIGHT)

        # 3. Recording Controls Area
        controls_group = ttk.Frame(main_frame, padding=(0, 5))
        controls_group.pack(fill=tk.X, pady=(0, 10))

        self._primary_button = ttk.Button(
            controls_group,
            text="Start Recording",
            style="Primary.TButton",
            command=self._on_primary_action,
        )
        self._primary_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self._stop_button = ttk.Button(
            controls_group,
            text="Stop Recording",
            style="Secondary.TButton",
            command=self._on_stop_action,
        )
        self._stop_button.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(6, 0))

        # 4. Status Area
        status_frame = ttk.Frame(main_frame, relief=tk.SUNKEN, padding=6)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self._status_label = ttk.Label(
            status_frame,
            text="Ready",
            style="Status.TLabel",
            anchor=tk.W,
        )
        self._status_label.pack(fill=tk.X)

    def _wire_state_machine(self) -> None:
        """Subscribe to state transitions from the ApplicationStateMachine."""
        self._state_machine.add_state_listener(self._on_state_machine_transition)

    def _wire_preview_controller(self) -> None:
        """Subscribe to live desktop preview frames from the PreviewController."""
        self._preview_controller.add_listener(self._on_preview_frame_received)

    def _bind_window_events(self) -> None:
        """Bind window lifecycle and visibility events."""
        self._root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._root.bind("<Unmap>", self._on_window_unmap)
        self._root.bind("<Map>", self._on_window_map)

    # -------------------------------------------------------------------------
    # State Machine & UI Synchronization
    # -------------------------------------------------------------------------

    def _on_state_machine_transition(
        self, state: ApplicationState, controls: UIControlsState
    ) -> None:
        """Callback invoked from state machine on state change (may occur on worker thread)."""
        # Ensure UI update is safely executed on the Tkinter main thread
        try:
            self._root.after(0, self._apply_ui_controls, state, controls)
        except Exception as exc:
            logger.debug("Could not schedule UI control update: %s", exc)

    def _apply_ui_controls(self, state: ApplicationState, controls: UIControlsState) -> None:
        """Apply derived UI control states to Tkinter widgets on the main thread."""
        if self._is_closing:
            return

        # Update Primary Action Button
        self._primary_button.config(
            text=controls.primary_action_label,
            state=tk.NORMAL if controls.primary_action_enabled else tk.DISABLED,
        )

        # Update Stop Recording Button
        self._stop_button.config(
            state=tk.NORMAL if controls.stop_button_enabled else tk.DISABLED,
        )

        # Lock / Unlock Settings Controls
        settings_state = tk.DISABLED if controls.settings_locked else tk.NORMAL
        self._res_720_radio.config(state=settings_state)
        self._res_1080_radio.config(state=settings_state)
        self._fps_30_radio.config(state=settings_state)
        self._fps_60_radio.config(state=settings_state)
        self._output_entry.config(state=settings_state)
        self._browse_button.config(state=settings_state)

        # Update Status Text
        status_text = controls.status_text
        if state == ApplicationState.IDLE and self._state_machine.last_finalization_result:
            res = self._state_machine.last_finalization_result
            status_text = f"Ready — Saved {res.output_path.name} ({res.duration:.1f}s)"

        self._status_label.config(text=status_text)

        # Sync Preview Mode with Application State
        if not self._is_minimized:
            if state == ApplicationState.RECORDING:
                self._preview_controller.set_mode(PreviewMode.RECORDING)
            else:
                self._preview_controller.set_mode(PreviewMode.IDLE)

    # -------------------------------------------------------------------------
    # Asynchronous Worker Execution (Non-blocking UI)
    # -------------------------------------------------------------------------

    def _run_async(self, task: Callable[[], None]) -> None:
        """Execute a blocking recorder or finalization action in a background worker thread."""
        thread = threading.Thread(target=task, daemon=True, name="UIRecorderWorker")
        thread.start()

    # -------------------------------------------------------------------------
    # UI Event Handlers
    # -------------------------------------------------------------------------

    def _on_resolution_changed(self) -> None:
        """Handle resolution radio button change."""
        val = self._res_var.get()
        res = Resolution(val)
        try:
            self._state_machine.update_settings(resolution=res)
        except Exception as exc:
            logger.warning("Failed to update resolution: %s", exc)

    def _on_fps_changed(self) -> None:
        """Handle FPS radio button change."""
        val = self._fps_var.get()
        fps = FrameRate(val)
        try:
            self._state_machine.update_settings(fps=fps)
        except Exception as exc:
            logger.warning("Failed to update FPS: %s", exc)

    def _on_browse_output_dir(self) -> None:
        """Open native directory chooser dialog to select output directory."""
        current_dir = str(self._state_machine.settings.output_dir)
        chosen = filedialog.askdirectory(
            parent=self._root,
            title="Select Recording Output Folder",
            initialdir=current_dir if Path(current_dir).exists() else None,
        )
        if chosen:
            chosen_path = Path(chosen)
            self._output_dir_var.set(str(chosen_path))
            try:
                self._state_machine.update_settings(output_dir=chosen_path)
            except Exception as exc:
                logger.warning("Failed to update output dir: %s", exc)

    def _on_output_entry_changed(self, _event: tk.Event | None = None) -> None:
        """Handle direct text edit in output directory entry field."""
        text = self._output_dir_var.get().strip()
        if text:
            path = Path(text)
            try:
                self._state_machine.update_settings(output_dir=path)
            except Exception as exc:
                logger.warning("Failed to update output dir from entry: %s", exc)

    def _on_primary_action(self) -> None:
        """Dispatch primary action button click based on current state."""
        state = self._state_machine.state

        # Temporarily disable button to prevent double-clicks while worker launches
        self._primary_button.config(state=tk.DISABLED)

        match state:
            case ApplicationState.IDLE:
                self._run_async(self._execute_start)
            case ApplicationState.RECORDING:
                self._run_async(self._execute_cut)
            case ApplicationState.PAUSED:
                self._run_async(self._execute_resume)
            case ApplicationState.ERROR:
                self._state_machine.reset_error()
            case ApplicationState.FINALIZING:
                pass

    def _on_stop_action(self) -> None:
        """Dispatch stop recording button click."""
        state = self._state_machine.state
        if state in (ApplicationState.RECORDING, ApplicationState.PAUSED):
            self._stop_button.config(state=tk.DISABLED)
            self._primary_button.config(state=tk.DISABLED)
            self._run_async(self._execute_stop)

    def _execute_start(self) -> None:
        """Background worker execution for start."""
        try:
            self._state_machine.start()
        except Exception as exc:
            logger.error("Error starting recording from UI: %s", exc)

    def _execute_cut(self) -> None:
        """Background worker execution for cut."""
        try:
            self._state_machine.cut()
        except Exception as exc:
            logger.error("Error cutting recording from UI: %s", exc)

    def _execute_resume(self) -> None:
        """Background worker execution for resume."""
        try:
            self._state_machine.resume()
        except Exception as exc:
            logger.error("Error resuming recording from UI: %s", exc)

    def _execute_stop(self) -> None:
        """Background worker execution for stop & finalize."""
        try:
            self._state_machine.stop()
        except Exception as exc:
            logger.error("Error stopping recording from UI: %s", exc)

    # -------------------------------------------------------------------------
    # Preview Frame Rendering & Minimization Detection
    # -------------------------------------------------------------------------

    def _on_preview_frame_received(self, frame: PreviewFrame) -> None:
        """Background callback from PreviewController when a new frame is captured."""
        if self._is_closing or self._is_minimized:
            return

        self._pending_frame = frame
        if not self._frame_draw_scheduled:
            self._frame_draw_scheduled = True
            try:
                self._root.after_idle(self._draw_pending_preview_frame)
            except Exception:
                self._frame_draw_scheduled = False

    def _draw_pending_preview_frame(self) -> None:
        """Render the latest captured desktop preview frame on the Tkinter canvas."""
        self._frame_draw_scheduled = False
        frame = self._pending_frame
        if not frame or self._is_closing or self._is_minimized:
            return

        try:
            ppm_data = bgra_to_ppm(frame.data, frame.width, frame.height)
            photo = tk.PhotoImage(master=self._root, data=ppm_data, format="PPM")

            if self._preview_canvas_img_id is None:
                # Remove initial placeholder text
                if self._preview_placeholder_text_id is not None:
                    self._preview_canvas.delete(self._preview_placeholder_text_id)
                    self._preview_placeholder_text_id = None

                self._preview_canvas_img_id = self._preview_canvas.create_image(
                    self._preview_width // 2,
                    self._preview_height // 2,
                    image=photo,
                )
            else:
                self._preview_canvas.itemconfig(self._preview_canvas_img_id, image=photo)

            # Retain photo reference to prevent Python garbage collection
            self._last_frame_photo = photo

        except Exception as exc:
            logger.debug("Failed to render preview frame to canvas: %s", exc)

    def _on_window_unmap(self, event: tk.Event) -> None:
        """Handle window unmap event (window minimized or hidden)."""
        # Ensure event corresponds to the root window itself
        is_iconic = self._root.wm_state() == "iconic"
        if event.widget == self._root and is_iconic and not self._is_minimized:
            logger.info("Window minimized: suspending preview capture workload.")
            self._is_minimized = True
            self._preview_controller.suspend()

    def _on_window_map(self, event: tk.Event) -> None:
        """Handle window map event (window restored or unminimized)."""
        if event.widget == self._root and self._root.wm_state() != "iconic" and self._is_minimized:
            logger.info("Window restored: resuming preview capture workload.")
            self._is_minimized = False
            is_rec = self._state_machine.state == ApplicationState.RECORDING
            self._preview_controller.resume(is_recording=is_rec)

    # -------------------------------------------------------------------------
    # Window Close & Shutdown
    # -------------------------------------------------------------------------

    def _on_window_close(self) -> None:
        """Handle window close request gracefully."""
        if self._is_closing:
            return

        self._is_closing = True
        logger.info("MainWindow closing requested...")

        # Stop preview controller worker thread
        try:
            self._preview_controller.stop()
        except Exception as exc:
            logger.warning("Error stopping preview controller on window close: %s", exc)

        # If recording or paused, abort/cleanup active session
        try:
            if self._state_machine.state in (
                ApplicationState.RECORDING,
                ApplicationState.PAUSED,
            ) and self._state_machine.active_session:
                self._state_machine.active_session.stop()
        except Exception as exc:
            logger.warning("Error stopping active session on window close: %s", exc)

        # Unregister listeners
        with contextlib.suppress(Exception):
            self._state_machine.remove_state_listener(self._on_state_machine_transition)
            self._preview_controller.remove_listener(self._on_preview_frame_received)

        with contextlib.suppress(Exception):
            self._root.destroy()

    def run(self) -> None:
        """Start the preview engine and run the Tkinter main event loop."""
        self._preview_controller.start()
        if self._owns_root:
            self._root.mainloop()
