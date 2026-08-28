# Hardware Recording Spike Report — Phase 0

## Environment & Hardware

- **Operating System:** Microsoft Windows 11 Pro 10.0.26200
- **CPU:** Intel(R) Core(TM) i5-9600K CPU @ 3.70GDz (6 Cores / 6 Threads)
- **iGPU:** Intel(R) UHD Graphics 630 (PCI ID: 8086:3E98, 1GB RAM)
- **Graphics Driver Version:** 26.20.100.7642 (DCH / Direct3D11/12)
- **Primary Display Resolution:** 1920x1080 @ 100Hz (Output 0)
- **FFmpeg Build:** `ffmpeg version N-120037-g36c8eef42c-20250625` (with oneVPL / QSV, D3D11VA, ddagrab)
- **FFprobe:** Verified operational.

## Hardware Encoders & Backends Probed

1. **Intel Quick Sync Video (`h264_qsv`):** **VERIFIED & OPERATIONAL** via oneVPL + D3D11VA.
2. **AMD AMF (`h264_amf`):** Present in local FFmpeg build, not present on this Intel-only reference system.
3. **Capture API (`ddagrab`):** Fully operational via D3D11 Desktop Duplication (`WinSta0\\Default` session context). Zero software frame-copy hot loops.


## Winning FFmpeg Graphs


### 1. 1080p60 Native Recording Profile (Winning Graph)
```bash
ffmpeg -init_hw_device d3d11va=d3d11 \
       -init_hw_device qsv=qsv@d3d11 \
       -filter_hw_device qsv \
       -f lavfi -i ddagrab=output_idx=0:draw_mouse=1:framerate=60 \
       -vf hwmap=derive_device=qsv,scale_qsv=format=nv12 \
       -c:v h264_qsv \
       -global_quality 23 \
       -fps_mode cfr \
       -y output.mp4
```

### 2. 720p60 Hardware Downscaled Recording Profile
```bash
ffmpeg -init_hw_device d3d11va=d3d11 \
       -init_hw_device qsv=qsv@d3d11 \
       -filter_hw_device qsv \
       -f lavfi -i ddagrab=output_idx=0:draw_mouse=1:framerate=60 \
       -vf hwmap=derive_device=qsv,scale_qsv=w=1280:h=720:format=nv12 \
       -c:v h264_qsv \
       -global_quality 23 \
       -fps_mode cfr \
       -y output_720p60.mp4
```

### 3. 720p30 / 1080p30 Profiles
- Substitute `framerate=30` and appropriate `scale_qsv` resolution parameters.


## Test Results Summary

| Target Profile | Duration | Encoded FPS | Resulting Bitrate | ffprobe Verification | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **720p30** | 5.00s | 30.0 fps | 139 kb/s | H.264 High, yuvj420p/nv12, 128px720 | **PASS** |
| **720p60** | 5.00s | 60.0 fps | 143 kb/s | H.264 High, yuvj420p/nv12, 1280x720 | **PASS** |
| **1080p30** | 5.00s | 30.0 fps | 299 kb/s | H.264 High, yuvj420p/nv12, 1920x1080 | **PASS** |
| **1080p60** | 5.00s | 60.0 fps | 1536 kb/s | H.264 High, yuvj420p/nv12, 1920x1080 | **PASS** |
| **Graceful Stop (`q` via stdin)** | 3.13s | 60.0 FPS | 378 kb/s | Clean MP4 container, moov atom finalized, seekable | **PASS** |
| **10-Minute Stress Test (1080p60)** | **10:00.00** | **60.0 fps** | **990 kb/s** | Valid MP4, 0 errors, perfect seeking | **PASS** |


## Key Findings & Architecture Decisions

1. **Hardware Pipeline & Zero Python Overhead:**
   - Frames move directly from `ddagrab` (D3D11) -> `hwmap=derive_device=qsv` -> `scale_qsv=format=nv12` -> `h264_qsv`.
   - CPU utilization remained extremely low (under 3% CPU for FFmpeg and 0% for Python).
   - Zero raw frames traverse the Python process space.

2. **CFR Timing & Duplication:**
   - Setting `-fps_mode cfr` along with `ddagrab` default `dup_frames=true` ensures constant 60 FPS output timestamps with no non-monotonic DTS warnings during static or dynamic scenes.

3. **Graceful Process Termination:**
   - Sending `b'q'` into standard input (`stdin`) triggers FFmpeg to finalize frame queues, close H.266 GOPs, write the `moov` atom, and cleanly exit with return code `0`.