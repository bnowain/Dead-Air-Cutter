"""
cut_dead_air.py — Remove silent/frozen dead air from board meeting recordings.

Detects periods where BOTH audio is silent AND video is frozen, then cuts
those gaps out and re-encodes the result.  Designed for raw OBS recordings
of public meetings where the stream pauses during closed sessions or breaks.

Usage:
    python cut_dead_air.py <input_video> [options]

    Drag a video file onto cut_dead_air.bat (Windows) for default settings.

Options:
    --out PATH          Output file path  [default: <input>_trimmed.mp4]
    --noise-db FLOAT    Audio silence threshold in dB  [default: -50]
    --min-silence FLOAT Minimum silence duration in seconds  [default: 30]
    --freeze-noise FLOAT  Max frame MSE to count as frozen  [default: 0.003]
    --no-gpu            Disable CUDA acceleration (use CPU only)
    --audio-only        Skip freeze detection; cut on silence alone
    --dry-run           Detect and print gaps without cutting

Examples:
    python cut_dead_air.py meeting.mp4
    python cut_dead_air.py meeting.mp4 --out trimmed.mp4 --min-silence 60
    python cut_dead_air.py meeting.mp4 --dry-run
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── Config file ───────────────────────────────────────────────────────────────

def _tool_dir() -> Path:
    """Return the directory containing the exe (frozen) or this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg_path = _tool_dir() / "config.ini"
    if cfg_path.exists():
        cfg.read(cfg_path, encoding="utf-8")
    return cfg


def _cfg_get(cfg: configparser.ConfigParser, key: str, fallback):
    """Read a value from [settings] section, return fallback if missing."""
    try:
        val = cfg.get("settings", key)
        # Cast to the same type as the fallback
        if isinstance(fallback, bool):
            return val.strip().lower() in ("1", "yes", "true", "on")
        if isinstance(fallback, float):
            return float(val)
        return val
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


# ── ffmpeg auto-detection ─────────────────────────────────────────────────────

_WINDOWS_FFMPEG_CANDIDATES = [
    "/mnt/c/ffmpeg/bin/ffmpeg.exe",
    "/mnt/c/ffmpeg/ffmpeg-2024-12-26-git-fe04b93afa-full_build/bin/ffmpeg.exe",
    "/mnt/c/ProgramData/chocolatey/bin/ffmpeg.exe",
    "/mnt/c/Program Files/ffmpeg/bin/ffmpeg.exe",
]
_WINDOWS_FFPROBE_CANDIDATES = [
    p.replace("ffmpeg.exe", "ffprobe.exe") for p in _WINDOWS_FFMPEG_CANDIDATES
]


def _find_binary(name: str, candidates: list[str]) -> str:
    env_key = "FFPROBE_BIN" if "ffprobe" in name.lower() else "FFMPEG_BIN"
    if env_override := os.environ.get(env_key):
        return env_override
    if path := shutil.which(name):
        return path
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        f"Could not find '{name}'. Install ffmpeg and add to PATH, "
        f"or set {env_key} environment variable to its full path."
    )


def _ffmpeg() -> str:
    return _find_binary("ffmpeg", _WINDOWS_FFMPEG_CANDIDATES)


def _ffprobe() -> str:
    return _find_binary("ffprobe", _WINDOWS_FFPROBE_CANDIDATES)


def _to_ffmpeg_path(path: str | Path) -> str:
    """Convert WSL /mnt/X/ paths to Windows drive paths when using a .exe binary."""
    path_str = str(path)
    ffmpeg_bin = _ffmpeg()
    is_windows_exe = ffmpeg_bin.endswith(".exe") or "/mnt/c/" in ffmpeg_bin
    if is_windows_exe and path_str.startswith("/mnt/"):
        parts = path_str.split("/")
        if len(parts) >= 3 and len(parts[2]) == 1:
            drive = parts[2].upper()
            rest = "\\".join(parts[3:])
            return f"{drive}:\\{rest}"
    return path_str


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SilenceGap:
    start: float
    end: float
    duration: float


@dataclass
class VideoInfo:
    duration_sec: float
    width: int
    height: int
    fps: float
    codec_name: str
    audio_codec: str


@dataclass
class VideoEditResult:
    success: bool
    output_path: str | None = None
    duration_sec: float | None = None
    error: str | None = None
    ffmpeg_log: str = field(default="", repr=False)


# ── Core functions ────────────────────────────────────────────────────────────

def probe(input_path: str | Path) -> VideoInfo:
    cmd = [
        _ffprobe(), "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        _to_ffmpeg_path(input_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr[:500]}")
    data = json.loads(r.stdout)
    fmt = data["format"]
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if not video_stream:
        raise RuntimeError("No video stream found in file")
    fps_raw = video_stream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return VideoInfo(
        duration_sec=float(fmt.get("duration", 0)),
        width=int(video_stream.get("width", 0)),
        height=int(video_stream.get("height", 0)),
        fps=fps,
        codec_name=video_stream.get("codec_name", "unknown"),
        audio_codec=audio_stream.get("codec_name", "none") if audio_stream else "none",
    )


def detect_silence(
    input_path: str | Path,
    noise_db: float = -50.0,
    min_duration: float = 30.0,
) -> list[SilenceGap]:
    cmd = [
        _ffmpeg(),
        "-i", _to_ffmpeg_path(input_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    gaps: list[SilenceGap] = []
    for line in r.stderr.splitlines():
        if "silence_end" in line:
            m_end = re.search(r"silence_end: ([0-9.]+)", line)
            m_dur = re.search(r"silence_duration: ([0-9.]+)", line)
            if m_end and m_dur:
                end = float(m_end.group(1))
                dur = float(m_dur.group(1))
                gaps.append(SilenceGap(start=end - dur, end=end, duration=dur))
    gaps.sort(key=lambda g: g.start)
    return gaps


def _is_cuda_error(stderr: str) -> bool:
    """Return True if ffmpeg stderr indicates a CUDA/NVENC initialisation failure."""
    markers = ("cuda", "nvenc", "cuvid", "no capable devices", "device not found",
               "cannot load", "failed to initialise", "error while opening encoder")
    low = stderr.lower()
    return any(m in low for m in markers)


def detect_frozen_video(
    input_path: str | Path,
    min_duration: float = 30.0,
    noise: float = 0.003,
    use_gpu: bool = False,
) -> tuple[list[SilenceGap], bool]:
    """
    Detect frozen video periods.

    Returns (gaps, gpu_used).  If use_gpu=True but CUDA is unavailable,
    automatically retries with CPU and returns gpu_used=False.
    """
    def _run(gpu: bool) -> subprocess.CompletedProcess:
        extra = ["-hwaccel", "cuda"] if gpu else []
        cmd = [
            _ffmpeg(),
            *extra,
            "-i", _to_ffmpeg_path(input_path),
            "-vf", f"fps=1,freezedetect=n={noise}:d={min_duration}",
            "-f", "null", "-",
        ]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

    r = _run(use_gpu)

    if use_gpu and (r.returncode != 0 or _is_cuda_error(r.stderr)):
        print("        WARNING: GPU (CUDA) unavailable for freeze detection — falling back to CPU")
        r = _run(gpu=False)
        gpu_used = False
    else:
        gpu_used = use_gpu

    gaps: list[SilenceGap] = []
    pending_start: float | None = None
    for line in r.stderr.splitlines():
        if "freeze_start" in line:
            m = re.search(r"freeze_start: ([0-9.]+)", line)
            if m:
                pending_start = float(m.group(1))
        elif "freeze_end" in line and pending_start is not None:
            m = re.search(r"freeze_end: ([0-9.]+)", line)
            if m:
                end = float(m.group(1))
                gaps.append(SilenceGap(start=pending_start, end=end, duration=end - pending_start))
                pending_start = None
    gaps.sort(key=lambda g: g.start)
    return gaps, gpu_used


def merge_gaps(gaps: list[SilenceGap], max_gap: float = 5.0) -> list[SilenceGap]:
    if not gaps:
        return []
    merged = [gaps[0]]
    for g in gaps[1:]:
        prev = merged[-1]
        if g.start - prev.end <= max_gap:
            new_end = max(prev.end, g.end)
            merged[-1] = SilenceGap(start=prev.start, end=new_end, duration=new_end - prev.start)
        else:
            merged.append(g)
    return merged


def intersect_gaps(
    audio_gaps: list[SilenceGap],
    video_gaps: list[SilenceGap],
    min_overlap: float = 10.0,
) -> list[SilenceGap]:
    result: list[SilenceGap] = []
    for ag in audio_gaps:
        for vg in video_gaps:
            start = max(ag.start, vg.start)
            end = min(ag.end, vg.end)
            if end - start >= min_overlap:
                result.append(SilenceGap(start=start, end=end, duration=end - start))
    result.sort(key=lambda g: g.start)
    return result


def cut_and_transcode(
    input_path: str | Path,
    output_path: str | Path,
    gaps_to_remove: list[SilenceGap],
    crf: int = 23,
    audio_bitrate: str = "128k",
    use_gpu: bool = False,
) -> VideoEditResult:
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    info = probe(input_path)
    total = info.duration_sec

    if not gaps_to_remove:
        segments = [(0.0, total)]
    else:
        gaps = sorted(gaps_to_remove, key=lambda g: g.start)
        segments: list[tuple[float, float]] = []
        cursor = 0.0
        for gap in gaps:
            if gap.start > cursor:
                segments.append((cursor, gap.start))
            cursor = gap.end
        if cursor < total:
            segments.append((cursor, total))

    if not segments:
        return VideoEditResult(success=False, error="No content remains after removing all gaps.")

    # Build filter_complex: trim each kept segment, concat, scale to match original height
    filter_parts: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []

    for i, (seg_start, seg_end) in enumerate(segments):
        filter_parts.append(
            f"[0:v]trim=start={seg_start:.6f}:end={seg_end:.6f},setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={seg_start:.6f}:end={seg_end:.6f},asetpts=PTS-STARTPTS[a{i}]"
        )
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")

    n = len(segments)
    concat_inputs = "".join(f"{v_labels[i]}{a_labels[i]}" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vcombined][acombined]")
    # Preserve original resolution (scale=-2:height keeps aspect ratio, -2 = round to even)
    filter_parts.append(f"[vcombined]scale=-2:{info.height}[vout]")
    filter_complex = ";".join(filter_parts)

    if use_gpu:
        cmd = [
            _ffmpeg(), "-y",
            "-i", _to_ffmpeg_path(input_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[acombined]",
            "-c:v", "h264_nvenc", "-cq", str(crf), "-preset", "p4",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart",
            _to_ffmpeg_path(output_path),
        ]
    else:
        cmd = [
            _ffmpeg(), "-y",
            "-i", _to_ffmpeg_path(input_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[acombined]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart",
            _to_ffmpeg_path(output_path),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return VideoEditResult(
            success=False,
            error=f"ffmpeg exited with code {r.returncode}",
            ffmpeg_log=r.stderr[-3000:],
        )

    try:
        out_info = probe(output_path)
        duration = out_info.duration_sec
    except Exception:
        duration = None

    return VideoEditResult(
        success=True,
        output_path=str(output_path),
        duration_sec=duration,
        ffmpeg_log=r.stderr[-1000:],
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def main() -> int:
    # Load config.ini from tool directory (CLI args take precedence over it)
    cfg = _load_config()
    cfg_path = _tool_dir() / "config.ini"

    # Read config defaults (fall back to hardcoded values if key absent)
    default_noise_db     = _cfg_get(cfg, "noise_db",     -50.0)
    default_min_silence  = _cfg_get(cfg, "min_silence",   30.0)
    default_freeze_noise = _cfg_get(cfg, "freeze_noise",   0.003)
    default_no_gpu       = _cfg_get(cfg, "no_gpu",         False)
    default_audio_only   = _cfg_get(cfg, "audio_only",     False)
    default_ffmpeg_bin   = _cfg_get(cfg, "ffmpeg_bin",     "")
    default_ffprobe_bin  = _cfg_get(cfg, "ffprobe_bin",    "")

    # Honor ffmpeg/ffprobe paths from config (env vars take precedence)
    if default_ffmpeg_bin and not os.environ.get("FFMPEG_BIN"):
        os.environ["FFMPEG_BIN"] = default_ffmpeg_bin
    if default_ffprobe_bin and not os.environ.get("FFPROBE_BIN"):
        os.environ["FFPROBE_BIN"] = default_ffprobe_bin

    parser = argparse.ArgumentParser(
        description="Remove silent/frozen dead air from board meeting recordings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[0].strip(),
    )
    parser.add_argument("input", help="Input video file")
    parser.add_argument("--out", help="Output file path (default: <input>_trimmed.mp4)")
    parser.add_argument("--noise-db", type=float, default=default_noise_db,
                        help=f"Audio silence threshold in dB (default: {default_noise_db})")
    parser.add_argument("--min-silence", type=float, default=default_min_silence,
                        help=f"Minimum silence duration in seconds (default: {default_min_silence})")
    parser.add_argument("--freeze-noise", type=float, default=default_freeze_noise,
                        help=f"Max frame MSE to count as frozen (default: {default_freeze_noise})")
    parser.add_argument("--no-gpu", action="store_true", default=default_no_gpu,
                        help="Disable CUDA acceleration")
    parser.add_argument("--audio-only", action="store_true", default=default_audio_only,
                        help="Cut on silence alone, skip freeze detection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect and print gaps without cutting")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        return 1

    if args.out:
        output_path = Path(args.out)
    else:
        output_path = input_path.with_name(input_path.stem + "_trimmed.mp4")

    use_gpu = not args.no_gpu

    cfg_note = f"  Config:      {cfg_path}" if cfg_path.exists() else "  Config:      (none — using defaults)"

    print(f"\n{'='*60}")
    print(f"  Dead Air Cutter")
    print(f"{'='*60}")
    print(cfg_note)
    print(f"  Input:        {input_path.name}")
    print(f"  Output:       {output_path.name}")
    print(f"  Silence:      {args.noise_db} dB  |  min {args.min_silence}s")
    if not args.audio_only:
        print(f"  Freeze noise: {args.freeze_noise}")
    print(f"  GPU:          {'yes (CUDA)' if use_gpu else 'no (CPU)'}")
    print(f"  Mode:         {'audio-only' if args.audio_only else 'audio + freeze'}")
    print()

    # Probe input
    try:
        info = probe(input_path)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"  Duration:    {_fmt_time(info.duration_sec)}  ({info.width}x{info.height}  {info.fps:.2f}fps)")
    print()

    # Step 1: Silence detection
    print("[ 1/3 ] Scanning audio for silence...", flush=True)
    audio_gaps = detect_silence(input_path, noise_db=args.noise_db, min_duration=args.min_silence)
    if not audio_gaps:
        print("        No silence gaps found — nothing to cut.")
        return 0
    print(f"        Found {len(audio_gaps)} audio gap(s):")
    for g in audio_gaps:
        print(f"          {_fmt_time(g.start)} → {_fmt_time(g.end)}  ({g.duration:.0f}s)")

    # Step 2: Freeze detection (or skip)
    if args.audio_only:
        gaps_to_cut = audio_gaps
        print("\n[ 2/3 ] Skipping freeze detection (--audio-only)")
    else:
        print(f"\n[ 2/3 ] Scanning video for frozen frames...", flush=True)
        video_gaps, use_gpu = detect_frozen_video(
            input_path, min_duration=args.min_silence,
            noise=args.freeze_noise, use_gpu=use_gpu,
        )
        video_gaps = merge_gaps(video_gaps, max_gap=5.0)
        print(f"        Found {len(video_gaps)} freeze period(s) after merge")

        gaps_to_cut = intersect_gaps(audio_gaps, video_gaps, min_overlap=args.min_silence)
        if not gaps_to_cut:
            print("        No silent+frozen overlap found — nothing to cut.")
            return 0
        print(f"        Cutting {len(gaps_to_cut)} gap(s) (silent AND frozen):")
        for g in gaps_to_cut:
            print(f"          {_fmt_time(g.start)} → {_fmt_time(g.end)}  ({g.duration:.0f}s)")

    total_cut = sum(g.duration for g in gaps_to_cut)
    print(f"\n        Total dead air to remove: {_fmt_time(total_cut)}")
    print(f"        Estimated output duration: {_fmt_time(info.duration_sec - total_cut)}")

    if args.dry_run:
        print("\n[ DRY RUN ] No file written.")
        return 0

    # Step 3: Cut + transcode
    gpu_label = "GPU (CUDA/NVENC)" if use_gpu else "CPU"
    print(f"\n[ 3/3 ] Cutting and transcoding via {gpu_label}... (this may take a while)", flush=True)
    result = cut_and_transcode(
        input_path=input_path,
        output_path=output_path,
        gaps_to_remove=gaps_to_cut,
        use_gpu=use_gpu,
    )

    if not result.success and use_gpu and _is_cuda_error(result.ffmpeg_log or result.error or ""):
        print("        WARNING: GPU encode failed — falling back to CPU")
        result = cut_and_transcode(
            input_path=input_path,
            output_path=output_path,
            gaps_to_remove=gaps_to_cut,
            use_gpu=False,
        )

    if not result.success:
        print(f"\nERROR: {result.error}", file=sys.stderr)
        if result.ffmpeg_log:
            print("--- ffmpeg output (last 3000 chars) ---")
            print(result.ffmpeg_log)
        return 1

    print(f"\n{'='*60}")
    print(f"  Done!")
    print(f"  Output:   {result.output_path}")
    if result.duration_sec:
        print(f"  Duration: {_fmt_time(result.duration_sec)}")
        saved = info.duration_sec - result.duration_sec
        print(f"  Removed:  {_fmt_time(saved)}  ({saved/info.duration_sec*100:.1f}% of original)")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
