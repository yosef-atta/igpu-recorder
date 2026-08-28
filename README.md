# iGPU Recorder

A small, performance-first Windows screen recorder for people who want a simple MP4 workflow without the weight and complexity of a streaming suite.

## MVP Goal

Open the app, choose recording quality, choose an output folder, confirm the full desktop in the preview, record, optionally cut unwanted time, resume, and finish with one normal MP4 file.

The main success target is smooth **1080p / 60 FPS** recording on supported integrated graphics with minimal CPU overhead.

## MVP Controls

- Resolution: `720p` or `1080p`
- Frame rate: `30 FPS` or `60 FPS`
- Output folder picker
- Full-primary-display preview
- `Start Recording`
- `CUT`
- `Resume`
- `Stop Recording`

`Stop Recording` is disabled while idle and becomes available after recording starts.

While recording, the Start button becomes `CUT`. Pressing `CUT` pauses capture by ending the current segment. The button then becomes `Resume`, which starts the next segment. When recording stops, compatible segments are joined into one MP4 without a full second encode pass.

## Performance Direction

Python owns the application and orchestration layer, not the high-throughput raw-frame pipeline.

Planned architecture:

```text
Tkinter UI
    |
Python application state / session manager
    |
FFmpeg process controller
    |
Windows Desktop Duplication (`ddagrab`)
    |
D3D11 hardware frames
    |
Hardware H.264 encoder (QSV / AMF)
    |
MP4 segment(s)
    |
Stream-copy finalization
    |
Final MP4
```

The recorder should avoid copying 60 full-resolution frames per second through Python. Native Windows/GPU APIs and FFmpeg should perform capture and encoding work whenever possible.

## Preview

The preview exists to confirm that the complete primary desktop is being captured. It is deliberately separate from the actual recording path.

The preview may run at a lower refresh rate than the recording and should stop refreshing while the app is minimized. Recording itself must continue normally while minimized.

## Hardware Encoding

The application will probe supported hardware encoders instead of assuming them.

Initial targets:

- Intel Quick Sync Video: `h264_qsv`
- AMD AMF: `h264_amf`

A software encoder must not silently replace a missing hardware path and then claim that the machine meets the 1080p60 goal.

## Output

The MVP produces:

- H.264 video
- `.mp4` container
- One final user-facing video file
- No watermark
- No ads
- No account
- No telemetry

## Scope

The first MVP intentionally does **not** include:

- Streaming
- Webcam
- Microphone
- System audio
- Multi-monitor stitching
- Region/window capture
- Video editing
- Annotations
- Cloud features
- Accounts
- Telemetry

Audio can be considered after the video-only 1080p60 performance target is proven.

## Documentation

- Product requirements: `docs/mvp/PRD.md`
- Implementation plan: `docs/mvp/TASKS.md`

## License

Project source is licensed under the MIT License.

FFmpeg is a separate project distributed under its own licensing terms and is not relicensed by this repository's MIT License.
