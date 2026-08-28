# iGPU Recorder — MVP Tasks

This is the implementation plan for the MVP defined in `PRD.md`.

The project is deliberately performance-first.

Do not build the complete UI before proving the recording pipeline.

# Phase 0 — Hardware Recording Spike

## Goal

Prove that the reference machine can record through a native/hardware path before application architecture is built around it.

## Tasks

- [x] Record Windows version.
- [x] Record CPU model.
- [x] Record iGPU/GPU model.
- [x] Record graphics driver version.
- [x] Select a known FFmpeg development build.
- [x] Confirm `ffmpeg` works.
- [x] Confirm `ffprobe` works.
- [x] Confirm `ddagrab` exists in the FFmpeg build.
- [x] Enumerate available H.264 hardware encoders.
- [x] Detect Intel QSV availability.
- [x] Detect AMD AMF availability.
- [x] Prove full-primary-display capture.
- [x] Prove mouse cursor capture.
- [x] Prove 720p30 recording.
- [x] Prove 720p60 recording.
- [x] Prove 1080p30 recording.
- [x] Prove 1080p60 recording.
- [x] Verify hardware Video Encode usage in Windows Task Manager.
- [x] Measure FFmpeg CPU utilization.
- [x] Measure GPU utilization.
- [x] Measure recording smoothness.
- [x] Verify generated MP4 with `ffprobe`.
- [x] Test hardware scaling from native resolution to 720p.
- [x] Test hardware scaling from native resolution to 1080p.
- [x] Determine the best pixel-format conversion path.
- [x] Determine encoder quality settings from measurements.
- [x] Document the winning FFmpeg graph.
- [x] Test graceful process termination.
- [x] Confirm finalized MP4 is playable and seekable.

## Exit criteria

- [x] One reproducible FFmpeg recording path exists.
- [x] It uses Desktop Duplication instead of Python full-rate frame capture.
- [x] Hardware video encoding is confirmed.
- [x] Reference machine records 1080p60 for 10 minutes successfully.
- [x] Resulting MP4 is valid.
- [x] No full-rate raw frames pass through Python.

Do not proceed to feature implementation until this phase passes or clearly identifies a hardware limitation.

# Phase 1 — Python Project Foundation

## Goal

Create a small maintainable Python application foundation without unnecessary runtime dependencies.

## Tasks

- [ ] Add `pyproject.toml`.
- [ ] Create `src/igpu_recorder/`.
- [ ] Create application entry point.
- [ ] Establish Python version policy.
- [ ] Add pytest.
- [ ] Add Ruff.
- [ ] Add static type checking.
- [ ] Add minimal structured logging.
- [ ] Add `.gitignore`.
- [ ] Add deterministic application version source.
- [ ] Add `tests/`.
- [ ] Add Windows-only platform guard.
- [ ] Add application-level exception boundary.

## Preferred development tooling

- Python 3.13
- `uv`
- pytest
- Ruff
- Pyright or equivalent static type checker

Runtime dependencies should remain intentionally small.

## Exit criteria

- [ ] Application entry point runs.
- [ ] Tests run.
- [ ] Lint passes.
- [ ] Type checks pass.
- [ ] Unsupported platforms fail clearly.

# Phase 2 — FFmpeg Capability Layer

## Goal

Turn the Phase 0 experiment into a deterministic application capability probe.

## Tasks

- [ ] Implement FFmpeg executable discovery.
- [ ] Implement ffprobe executable discovery.
- [ ] Read FFmpeg version.
- [ ] Detect `ddagrab`.
- [ ] Detect available hardware H.264 encoders.
- [ ] Add Intel QSV probe.
- [ ] Add AMD AMF probe.
- [ ] Verify encoder initialization instead of trusting encoder-list output alone.
- [ ] Model hardware backend as a typed abstraction.
- [ ] Model recording profile: resolution, FPS, backend, output path.
- [ ] Generate FFmpeg arguments as a list rather than shell-concatenated strings.
- [ ] Prevent command injection through paths.
- [ ] Capture FFmpeg stderr for diagnostics.
- [ ] Add timeout handling for startup probes.
- [ ] Add unit tests for command construction.
- [ ] Add unit tests for probe-result parsing.

## Exit criteria

- [ ] App can identify a usable hardware backend.
- [ ] App rejects unsupported hardware cleanly.
- [ ] No silent software fallback exists.

# Phase 3 — Recording Process Controller

## Goal

Create a reliable process boundary around FFmpeg.

## Tasks

- [ ] Implement recorder process start.
- [ ] Detect immediate FFmpeg startup failure.
- [ ] Implement graceful FFmpeg stop.
- [ ] Add bounded shutdown timeout.
- [ ] Add forced termination only as last-resort cleanup.
- [ ] Capture exit code.
- [ ] Capture diagnostic stderr.
- [ ] Prevent multiple simultaneous recorder processes.
- [ ] Detect unexpected FFmpeg death.
- [ ] Expose typed process status to application layer.
- [ ] Add process-controller tests with fake subprocesses.

## Exit criteria

- [ ] Recorder can start and stop repeatedly.
- [ ] MP4 segment metadata is finalized on normal stop.
- [ ] Unexpected exits are surfaced to the application state machine.

# Phase 4 — Recording Session + CUT / Resume

## Goal

Implement one logical recording composed of multiple hardware-encoded segments.

## Tasks

- [ ] Define `RecordingSession`.
- [ ] Generate unique session ID.
- [ ] Create private temporary session directory.
- [ ] Snapshot immutable settings at Start.
- [ ] Generate deterministic segment names.
- [ ] Implement segment `000`.
- [ ] Implement CUT.
- [ ] Gracefully finalize segment during CUT.
- [ ] Reject empty/invalid completed segments.
- [ ] Implement Resume.
- [ ] Increment segment index on Resume.
- [ ] Ensure resumed segments use identical encoding settings.
- [ ] Support repeated CUT/Resume cycles.
- [ ] Implement Stop from RECORDING.
- [ ] Implement Stop from PAUSED.
- [ ] Preserve completed segments after unexpected failure where possible.

## Exit criteria

Given:

```text
Start
record 10s
CUT
wait 5s
Resume
record 10s
Stop
```

the final logical recording contains approximately 20 seconds of video rather than 25 seconds.

# Phase 5 — MP4 Finalizer

## Goal

Produce one normal MP4 without re-encoding the completed recording.

## Tasks

- [ ] Build concat input deterministically.
- [ ] Escape temporary paths safely.
- [ ] Concatenate compatible segments using stream copy.
- [ ] Avoid complete video re-encode.
- [ ] Handle a session containing only one segment.
- [ ] Apply MP4 fast-start metadata relocation where appropriate.
- [ ] Validate final file using ffprobe.
- [ ] Verify expected resolution.
- [ ] Verify expected codec.
- [ ] Verify expected FPS.
- [ ] Verify non-zero duration.
- [ ] Handle destination filename collisions.
- [ ] Never overwrite an existing output silently.
- [ ] Clean temporary session only after successful validation.
- [ ] Preserve temporary files when finalization fails.
- [ ] Return recovery path on failure.

## Exit criteria

- [ ] CUT/Resume recordings become one MP4.
- [ ] Finalization performs no full encode pass.
- [ ] Failed finalization does not destroy source segments.

# Phase 6 — Preview Spike

## Goal

Find the cheapest preview implementation that provides enough visual confidence without compromising recorder performance.

## Tasks

- [ ] Build a minimal full-primary-display preview prototype.
- [ ] Compare suitable Windows capture approaches.
- [ ] Evaluate a Desktop Duplication-based Python library such as DXcam.
- [ ] Measure preview CPU use.
- [ ] Measure preview GPU use.
- [ ] Downscale before UI rendering where practical.
- [ ] Cap idle preview around 10 FPS.
- [ ] Cap recording preview around 5 FPS.
- [ ] Suspend preview when the window is minimized.
- [ ] Restart preview on restore.
- [ ] Run 1080p60 recording with preview active.
- [ ] Run 1080p60 recording with preview suspended.
- [ ] Compare frame stability.
- [ ] Choose implementation based on measurements.

## Exit criteria

- [ ] Preview clearly displays the entire primary desktop.
- [ ] Preview does not become part of the recording data path.
- [ ] Minimization stops preview workload.
- [ ] Recording performance remains acceptable.

# Phase 7 — Application State Machine

## Goal

Make invalid UI/recorder states impossible or explicit.

## States

```text
IDLE
RECORDING
PAUSED
FINALIZING
ERROR
```

## Tasks

- [ ] Implement explicit state enum.
- [ ] Implement valid state transitions.
- [ ] Reject invalid transitions.
- [ ] Centralize state mutations.
- [ ] Lock settings outside IDLE.
- [ ] Support Start from IDLE.
- [ ] Support CUT from RECORDING.
- [ ] Support Resume from PAUSED.
- [ ] Support Stop from RECORDING.
- [ ] Support Stop from PAUSED.
- [ ] Support finalization success to IDLE.
- [ ] Support recoverable ERROR to IDLE.
- [ ] Add state-machine unit tests.

## Exit criteria

Every button state can be derived from application state rather than manually toggled from unrelated code paths.

# Phase 8 — Main UI

## Goal

Build the intentionally simple interface defined in the PRD.

## Tasks

- [ ] Create one main Tkinter window.
- [ ] Add centered preview region.
- [ ] Add 720p selector.
- [ ] Add 1080p selector.
- [ ] Add 30 FPS selector.
- [ ] Add 60 FPS selector.
- [ ] Add output-folder field.
- [ ] Add Browse button.
- [ ] Add primary action button.
- [ ] Add Stop Recording button.
- [ ] Add small status area.
- [ ] Wire UI entirely through application state.
- [ ] Show `Start Recording` in IDLE.
- [ ] Show `CUT` in RECORDING.
- [ ] Show `Resume` in PAUSED.
- [ ] Disable Stop in IDLE.
- [ ] Enable Stop in RECORDING.
- [ ] Enable Stop in PAUSED.
- [ ] Disable controls during FINALIZING.
- [ ] Add minimized-window detection.
- [ ] Keep UI responsive while FFmpeg operations execute.
- [ ] Prevent blocking subprocess waits on the Tkinter main thread.

## Exit criteria

The complete MVP workflow can be performed without using a terminal.

# Phase 9 — Shutdown and Recovery

## Goal

Avoid losing recordings during common failure/close scenarios.

## Tasks

- [ ] Intercept app close while RECORDING.
- [ ] Gracefully stop active segment before exit when possible.
- [ ] Intercept app close while PAUSED.
- [ ] Preserve valid segments.
- [ ] Handle finalizer failure.
- [ ] Handle recorder process crash.
- [ ] Handle Desktop Duplication access loss.
- [ ] Handle display mode change.
- [ ] Handle unwritable output folder.
- [ ] Detect low-disk conditions where practical.
- [ ] Keep recovery session metadata.
- [ ] Never delete unfinalized recoverable segments automatically.

## Exit criteria

A recorder failure does not silently destroy already completed segments.

# Phase 10 — Automated Tests

## Unit tests

- [ ] State transitions.
- [ ] Recording profile validation.
- [ ] Filename generation.
- [ ] Filename collision handling.
- [ ] Segment numbering.
- [ ] FFmpeg command construction.
- [ ] Encoder probing.
- [ ] Process-controller behavior.
- [ ] CUT behavior.
- [ ] Resume behavior.
- [ ] Stop-from-paused behavior.
- [ ] Finalizer command construction.
- [ ] Cleanup rules.
- [ ] Recovery rules.

## Integration tests

- [ ] Start/Stop real FFmpeg.
- [ ] Start/CUT/Resume/Stop.
- [ ] Multi-CUT session.
- [ ] 720p30 output validation.
- [ ] 720p60 output validation.
- [ ] 1080p30 output validation.
- [ ] 1080p60 output validation.
- [ ] App minimize while recording.
- [ ] Existing destination filename.
- [ ] Finalizer failure preserves segments.

## Exit criteria

Core state/session logic has deterministic automated coverage and the Windows reference machine passes real media integration tests.

# Phase 11 — Performance Qualification

## Goal

Prove the actual reason for the application's existence.

## Benchmarks

For each validated hardware backend:

- [ ] 720p30 × 10 minutes.
- [ ] 720p60 × 10 minutes.
- [ ] 1080p30 × 10 minutes.
- [ ] 1080p60 × 10 minutes.

## During each benchmark collect

- [ ] Python CPU.
- [ ] FFmpeg CPU.
- [ ] Python working set.
- [ ] FFmpeg working set.
- [ ] GPU 3D utilization.
- [ ] GPU Copy utilization.
- [ ] GPU Video Encode utilization.
- [ ] Encoded FPS.
- [ ] Dropped frames.
- [ ] Duplicated frames where measurable.
- [ ] File size.
- [ ] Video duration.
- [ ] Wall-clock active recording duration.

## 1080p60 workload

Include:

- [ ] Browser scrolling.
- [ ] Video playback.
- [ ] Window dragging.
- [ ] App switching.
- [ ] Minimize recorder.
- [ ] Restore recorder.
- [ ] CUT.
- [ ] Resume.
- [ ] Final Stop.

## Exit criteria

1080p60 on the reference iGPU is smooth enough to satisfy the product goal and hardware encoding is verified.

If this phase fails, optimize before adding features.

# Phase 12 — Windows Packaging

## Goal

Make the recorder usable without development tooling.

## Tasks

- [ ] Choose packaging approach based on startup/runtime measurements.
- [ ] Prefer a normal directory build over costly self-extraction if measurements favor it.
- [ ] Decide how FFmpeg is supplied after licensing/distribution review.
- [ ] Include required third-party notices.
- [ ] Keep project source under MIT.
- [ ] Produce Windows x64 build.
- [ ] Test on a clean Windows machine.
- [ ] Verify hardware encoder discovery after packaging.
- [ ] Verify output-folder dialog.
- [ ] Verify recording.
- [ ] Verify CUT/Resume.
- [ ] Verify MP4 finalization.
- [ ] Record packaged application size.
- [ ] Record cold-start time.

## Exit criteria

A clean Windows machine can launch and use the recorder without a Python development environment.

# Definition of Done — MVP

The MVP is done only when:

- [ ] Entire primary display records.
- [ ] 720p works.
- [ ] 1080p works.
- [ ] 30 FPS works.
- [ ] 60 FPS works.
- [ ] Output-folder selection works.
- [ ] Preview works.
- [ ] Start works.
- [ ] CUT works.
- [ ] Resume works.
- [ ] Stop works.
- [ ] Stop from paused works.
- [ ] Final output is MP4.
- [ ] Finalization avoids full re-encode.
- [ ] Minimize does not stop recording.
- [ ] Preview suspends while minimized.
- [ ] Hardware encoder is actually used.
- [ ] Reference-machine 1080p60 test passes.
- [ ] Failed finalization preserves recoverable segments.
- [ ] No telemetry exists.
- [ ] No ads exist.
- [ ] No watermark exists.
- [ ] No account exists.

# Recommended Implementation Order

```text
Phase 0
  ↓
Phase 1
  ↓
Phase 2
  ↓
Phase 3
  ↓
Phase 4
  ↓
Phase 5
  ↓
Phase 7
  ↓
Phase 6
  ↓
Phase 8
  ↓
Phase 9
  ↓
Phase 10
  ↓
Phase 11
  ↓
Phase 12
```

The important rule is:

> Prove recording first. Build the recorder second. Polish the application last.
