# iGPU Recorder — MVP Product Requirements Document

**Status:** Ready for implementation  
**Target:** Windows 10/11 desktop  
**Product type:** Personal local-only screen recorder  
**Primary engineering priority:** Smooth recording with the lowest practical runtime overhead on integrated graphics.

## 1. Problem

Existing screen recorders often fail one or more requirements for a simple personal recorder:

- Too heavy on CPU/iGPU.
- Poor visual quality.
- Unstable or unsmooth 60 FPS recording.
- Ads or paid restrictions.
- Unnecessary streaming/scene-management complexity.
- User-facing formats such as MKV when the desired result is simply MP4.
- Too many settings for a basic screen-recording workflow.

The product should solve one job extremely well:

> Open the app, select quality, select an output folder, verify the desktop preview, record, cut unwanted time when needed, resume, and finish with one normal MP4 file.

## 2. Product Goal

Build a lightweight Windows screen recorder where:

- Python owns UI, state, validation, session management, and process orchestration.
- Native/hardware-accelerated components own capture and encoding.
- Full-rate raw recording frames do not pass through Python.
- The primary performance target is 1080p at 60 FPS on a supported iGPU.
- The final user-facing file is MP4.

1080p60 is a measurable target on supported hardware, not a promise for every iGPU generation, driver, display configuration, or machine. The app must detect capabilities rather than assume them.

## 3. Product Principles

### 3.1 Performance before features

A feature that materially harms recording performance does not belong in the MVP.

### 3.2 No Python recording hot loop

Python must not capture, copy, transform, and encode 60 full-resolution frames every second.

Python orchestrates the recorder. FFmpeg/native GPU APIs perform the high-throughput work.

### 3.3 MP4-first

A successful recording ends as one `.mp4` file. The user should not need to manually remux or convert anything.

### 3.4 Simple state model

The user must always be in one explicit state:

- Idle
- Recording
- Paused/Cut
- Finalizing
- Error

### 3.5 Local only

The MVP has no account, cloud, upload, telemetry, analytics, ads, or network dependency for recording.

## 4. MVP Platform and Stack

### Operating system

Windows 10/11 x64 desktop.

### Display scope

Record the entire primary display.

Multi-monitor selection/stitching is deferred.

### Application language

Python.

### UI

Tkinter + ttk is the initial MVP choice because it ships with Python, has a small runtime footprint, and is sufficient for the intentionally simple UI.

### Native media worker

FFmpeg.

### Capture direction

Use FFmpeg `ddagrab` where supported. `ddagrab` uses Windows Desktop Duplication and provides D3D11 hardware frames, allowing capture to stay out of Python's full-rate frame path.

### Video codec

H.264.

### Container

MP4.

## 5. Hardware Encoding Strategy

Hardware support must be probed at runtime.

Initial preference targets:

1. Intel Quick Sync Video — `h264_qsv`
2. AMD AMF — `h264_amf`

Additional hardware backends may be added later.

A software encoder must not silently replace hardware encoding and then report the machine as 1080p60-capable.

If no validated hardware encoder is available, the MVP should report that hardware recording is unavailable.

## 6. Main Window

The application consists of one compact window.

From top to bottom:

1. Desktop preview.
2. Resolution selector.
3. Frame-rate selector.
4. Output-folder selector.
5. Recording controls.
6. Small status area.

The MVP has no scene editor, source list, mixer, timeline, streaming settings, account UI, or advanced codec panel.

## 7. Desktop Preview

The preview is centered near the top of the window and displays the complete primary desktop.

Its only purpose is to let the user confirm:

- The correct desktop is being captured.
- The complete display is visible.
- The recorder is not accidentally capturing a region or single window.

The preview is **not** the source used to create the recording.

### Preview performance policy

Initial target behavior:

- Idle: up to roughly 10 FPS.
- Recording while app is visible: up to roughly 5 FPS.
- App minimized: preview workload suspended.

When minimized, the actual recording must continue unaffected.

When the recorder window is visible, it may naturally appear inside the desktop recording. When minimized, the recording should show the desktop state with the app minimized.

The preview must preserve aspect ratio and avoid cropping meaningful desktop content.

The exact preview implementation is intentionally not locked before profiling. A Desktop Duplication-based Python library is a candidate only if measured overhead remains small.

## 8. User-Selectable Settings

### Resolution

The user chooses:

- `720p`
- `1080p`

Target output canvases:

| Selection | Output |
| --- | --- |
| 720p | 1280 × 720 |
| 1080p | 1920 × 1080 |

The full primary desktop is captured.

If the source aspect ratio differs from the selected output canvas, scale while preserving aspect ratio and pad if needed. Do not crop meaningful desktop content merely to fill the canvas.

### Frame rate

The user chooses:

- `30 FPS`
- `60 FPS`

### Output folder

The user chooses the folder where the final recording will be saved. The folder must be validated before recording begins.

## 9. Recording State Machine

The application has five explicit states:

```text
IDLE
RECORDING
PAUSED
FINALIZING
ERROR
```

## 10. IDLE State

Controls:

- Primary button: `Start Recording`
- Primary button enabled.
- `Stop Recording` disabled.
- Resolution enabled.
- FPS enabled.
- Output folder enabled.

No recording process exists.

## 11. Starting a Recording

When `Start Recording` is pressed:

1. Validate output folder.
2. Validate FFmpeg/ffprobe availability.
3. Validate `ddagrab` support.
4. Validate the selected hardware encoder can actually initialize.
5. Freeze the selected session settings.
6. Allocate a temporary recording session.
7. Start segment `000`.
8. Confirm that FFmpeg actually started successfully.
9. Transition to `RECORDING`.

The UI must not claim that recording started before the underlying recorder successfully starts.

## 12. RECORDING State

Controls:

- Primary button label becomes `CUT`.
- Primary button remains enabled.
- `Stop Recording` becomes enabled.
- Resolution locked.
- FPS locked.
- Output folder locked.

The user cannot mutate encoding settings in the middle of a logical recording.

## 13. CUT Behavior

`CUT` behaves like a pause where paused time is completely absent from the finished video.

Implementation model:

> CUT creates a segment boundary.

When `CUT` is pressed:

1. Gracefully stop the current FFmpeg segment.
2. Allow FFmpeg to finalize the segment.
3. Validate that the segment exists and is usable.
4. Transition to `PAUSED`.
5. Change the primary button label to `Resume`.

CUT does not create black frames or frozen frames. Time between CUT and Resume must not exist in the final output.

## 14. PAUSED State

Controls:

- Primary button: `Resume`
- Primary button enabled.
- Stop enabled.
- Settings remain locked.

No active recording segment is running. The logical recording session remains open.

## 15. Resume Behavior

When `Resume` is pressed:

1. Create the next sequential segment.
2. Reuse the immutable session settings.
3. Start the hardware recording process.
4. Verify successful startup.
5. Transition to `RECORDING`.
6. Change the primary button back to `CUT`.

Example temporary sequence:

```text
segment_000.mp4
segment_001.mp4
segment_002.mp4
```

## 16. Stop Recording

`Stop Recording` works from either `RECORDING` or `PAUSED`.

### From RECORDING

1. Gracefully close the active segment.
2. Validate the segment.
3. Begin finalization.

### From PAUSED

1. No active segment needs to be stopped.
2. Begin finalization using already completed segments.

The UI transitions to `FINALIZING` while the final file is being created.

## 17. Finalization

The finished recording should be assembled without performing a complete second video encode.

Because all segments use identical codec, resolution, frame rate, pixel format, and encoder configuration, FFmpeg stream-copy concatenation/remuxing should be used where compatible.

Target behavior:

```text
segment_000.mp4
segment_001.mp4
segment_002.mp4
        |
        v
stream-copy concat/remux
        |
        v
final recording.mp4
```

Finalization responsibilities:

1. Validate all expected segments.
2. Generate a deterministic ordered segment list.
3. Concatenate/remux.
4. Use stream copy where possible.
5. Produce one MP4.
6. Validate output with ffprobe.
7. Only then remove temporary files.

If finalization fails:

- Do not delete valid segments.
- Report the recovery/session folder.
- Do not falsely report success.

## 18. Output File

Default filename format:

```text
iGPU-Recorder_YYYY-MM-DD_HH-mm-ss.mp4
```

Requirements:

- Saved inside the selected folder.
- Never silently overwrite an existing recording.
- Add a deterministic numeric suffix on collision.
- Seekable and playable by normal MP4 players.
- No watermark.
- No application-imposed duration limit.

## 19. Video Requirements

Supported MVP combinations:

| Resolution | FPS |
| --- | --- |
| 720p | 30 |
| 720p | 60 |
| 1080p | 30 |
| 1080p | 60 |

Additional requirements:

- Capture the entire primary display.
- Include the mouse cursor.
- H.264 video.
- MP4 container.
- Hardware encoding for the primary performance path.
- Common playback-compatible format/profile.
- No user-facing MKV workflow.
- No bitrate/codec complexity exposed in the initial UI.

Encoder-specific quality settings must be selected from benchmarks rather than guessed before implementation.

## 20. Proposed Architecture

```text
+-------------------------------+
|          Tkinter UI           |
|                               |
| Resolution / FPS / Folder     |
| Preview / Start / Cut / Stop  |
+---------------+---------------+
                |
                v
+-------------------------------+
|       Application Core        |
|                               |
| State Machine                 |
| Session Manager               |
| Capability Probe              |
| Process Controller            |
| Finalizer                     |
+---------------+---------------+
                |
                v
+-------------------------------+
|            FFmpeg             |
|                               |
| ddagrab                       |
| D3D11 desktop frames          |
| HW scaling / conversion       |
| QSV / AMF H.264 encoding      |
+---------------+---------------+
                |
                v
          MP4 segments
                |
                v
       Stream-copy finalize
                |
                v
          Final MP4 file
```

Python must never become the full-rate raw-video transport between capture and encoder.

## 21. Session Model

Each recording session owns:

- Session ID.
- Selected resolution.
- Selected FPS.
- Selected hardware backend.
- Final target path.
- Temporary session folder.
- Ordered segment list.
- Current segment index.
- Current FFmpeg process, if recording.
- Diagnostic log.
- Session state.

Session settings are immutable after recording begins.

## 22. Performance Requirements

Performance is a release gate, not a cleanup task for later.

Every supported hardware backend should be benchmarked at:

- 720p30 for 10 minutes.
- 720p60 for 10 minutes.
- 1080p30 for 10 minutes.
- 1080p60 for 10 minutes.

1080p60 testing should include real desktop activity such as browser scrolling, window movement, animations, video playback, app switching, minimize/restore, CUT, and Resume.

Collect where practical:

- Actual encoded FPS.
- Dropped frames.
- Duplicated frames.
- Python CPU use.
- FFmpeg CPU use.
- Python memory use.
- FFmpeg memory use.
- GPU 3D utilization.
- GPU copy utilization.
- GPU video-encode utilization.
- Active recording wall-clock duration.
- Final video duration.
- Final file size.

## 23. 1080p60 Performance Gate

A backend can be called `1080p60 capable` only when the reference machine demonstrates:

- 1920×1080 final video.
- Approximately 60 FPS final stream.
- No obvious sustained recording stutter.
- CPU is not saturated by the recorder.
- Hardware Video Encode engine is demonstrably active.
- Python UI remains responsive.
- Preview suspension/minimization does not affect recording.
- A 10-minute recording completes successfully.
- Final MP4 is valid and seekable.

Exact CPU-percentage thresholds should be established from the Phase 0 baseline instead of invented before measurements exist.

## 24. Error Handling

The MVP must clearly handle:

- FFmpeg missing or incompatible.
- ffprobe missing.
- `ddagrab` unavailable.
- No supported hardware encoder.
- Hardware encoder listed but unable to initialize.
- Invalid or unwritable output folder.
- Filename collision.
- Insufficient storage where detectable.
- FFmpeg unexpected exit.
- Display mode/resolution changes.
- Desktop Duplication access loss.
- CUT failure.
- Resume failure.
- Stop while paused.
- App close while recording.
- Invalid/empty recording segment.
- Final concat/remux failure.

Errors must never cause valid completed segments to be deleted automatically.

## 25. Minimize / Restore Behavior

Minimizing the application:

- Does not stop recording.
- Does not pause recording.
- Does not change output resolution.
- Does not change FPS.
- Suspends preview capture/rendering.
- Leaves the actual recording process running.

Restoring the application:

- Restarts the low-rate preview.
- Reflects the current recording state correctly.
- Does not restart or alter the active recording session.

## 26. Privacy

The application:

- Records locally.
- Makes no network calls as part of recording.
- Uploads nothing.
- Collects no analytics.
- Sends no telemetry.
- Requires no login.

Diagnostic logs must never contain captured frame/image content.

## 27. Protected Content

Windows Desktop Duplication may protect restricted video content, which can appear black in a recording. This is expected platform behavior. The application will not attempt to bypass content protection.

## 28. Explicit MVP Non-Goals

Not included in the first MVP:

- System audio.
- Microphone recording.
- Webcam.
- Multiple audio tracks.
- Streaming.
- Twitch/YouTube integration.
- Multi-monitor stitching.
- Monitor picker.
- Window-only capture.
- Region capture.
- Annotations/drawing.
- Watermarks.
- Global hotkeys.
- Timeline editor.
- Video editing.
- GIF/WebM export.
- Cloud storage.
- Accounts.
- Telemetry.
- Auto-update.
- Plugin system.

Audio is a logical future milestone, but it should not be added before the video-only 1080p60 target is proven.

## 29. MVP Acceptance Criteria

- [ ] One simple Windows application window exists.
- [ ] Preview shows the full primary desktop.
- [ ] User can select 720p.
- [ ] User can select 1080p.
- [ ] User can select 30 FPS.
- [ ] User can select 60 FPS.
- [ ] User can select an output folder.
- [ ] Idle state shows `Start Recording`.
- [ ] Stop is disabled while idle.
- [ ] Start successfully starts hardware recording.
- [ ] Start button becomes `CUT` while recording.
- [ ] Stop becomes enabled while recording.
- [ ] CUT removes paused time from the finished recording.
- [ ] CUT changes the primary button to `Resume`.
- [ ] Resume continues the same logical recording session.
- [ ] Stop works while recording.
- [ ] Stop works while paused.
- [ ] Recording settings cannot mutate mid-session.
- [ ] App minimization does not interrupt recording.
- [ ] Preview suspends while minimized.
- [ ] Final output is one MP4.
- [ ] Final assembly avoids a full video re-encode.
- [ ] Existing files are never silently overwritten.
- [ ] Failed finalization preserves recoverable segments.
- [ ] Unsupported hardware fails clearly.
- [ ] No ads exist.
- [ ] No telemetry exists.
- [ ] No watermark exists.
- [ ] No account exists.
- [ ] Reference-machine 1080p60 benchmark passes.

## 30. Technical References

- Microsoft Desktop Duplication API: https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api
- FFmpeg `ddagrab`: https://ffmpeg.org/ffmpeg-filters.html#ddagrab
- FFmpeg codecs: https://ffmpeg.org/ffmpeg-codecs.html

The exact production FFmpeg graph must be selected from Phase 0 benchmark results rather than assumed in advance.
