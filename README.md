# Dead Air Cutter

Removes silent + frozen dead air from board meeting recordings in one step.

Designed for raw OBS recordings where the stream pauses during closed sessions,
breaks, or off-air periods — producing a clean, continuous video.

## Quick Start (Windows)

**Drag and drop** a video file onto `cut_dead_air.bat`.

Output is written to the same folder as the input, named `<filename>_trimmed.mp4`.

## Requirements

- Python 3.x on `PATH`
- `ffmpeg` / `ffprobe` on `PATH` (or set `FFMPEG_BIN` / `FFPROBE_BIN` env vars)
- NVIDIA GPU optional (falls back to CPU automatically)

## How It Works

1. **Audio scan** — finds periods of silence below the noise floor (default -50 dB) lasting at least 30 seconds
2. **Video scan** — finds frozen frames (no motion) using ffmpeg `freezedetect`
3. **Intersect** — only cuts regions that are *both* silent *and* frozen (safe — won't cut over quiet speech or a still presenter)
4. **Transcode** — re-encodes in a single ffmpeg pass, preserving original resolution

## Command Line Options

```
python cut_dead_air.py <input_video> [options]

  --out PATH            Output path  [default: <input>_trimmed.mp4]
  --noise-db FLOAT      Audio threshold in dB  [default: -50]
  --min-silence FLOAT   Minimum gap duration in seconds  [default: 30]
  --freeze-noise FLOAT  Max frame MSE for frozen detection  [default: 0.003]
  --no-gpu              Force CPU encoding
  --audio-only          Skip freeze detection; cut on silence alone
  --dry-run             Print detected gaps without cutting
```

## Tuning Tips

| Scenario | Suggestion |
|---|---|
| Recording has HVAC hum during breaks | Default `-50 dB` works well |
| Truly silent recording (no background noise) | Try `--noise-db -91` |
| Cutting too aggressively | Raise `--min-silence` (e.g. `60`) |
| Camera has sensor noise (no pixel-perfect freeze) | Raise `--freeze-noise 0.01` |
| Presenter is very still but audio continues | Default (audio+freeze intersect) protects this |
| Want to cut on silence alone | `--audio-only` |
