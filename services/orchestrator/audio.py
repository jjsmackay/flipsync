"""Audio file introspection shared by routers and job handlers."""

import asyncio
import wave

_FFPROBE_TIMEOUT_SECS = 10.0


def wave_duration(path: str) -> float:
    """Duration of a WAV file via the stdlib wave module (0.0 on failure)."""
    try:
        with wave.open(path, "r") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


async def get_duration(path: str) -> float:
    """Return audio duration in seconds without blocking the event loop.

    Tries ffprobe (any format) via an async subprocess, falls back to the wave
    module for WAV files (works in test environments without ffprobe).
    Returns 0.0 when both fail.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECS)
        val = float(stdout.decode().strip())
        if val > 0:
            return val
    except Exception:
        # ffprobe missing, hung, or emitted junk. Kill a still-running process
        # before falling back — a timed-out wait_for leaves it alive otherwise.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    # Fallback: WAV header read, offloaded to a thread to stay non-blocking.
    return await asyncio.to_thread(wave_duration, path)


async def slice_wav(src: str, dst: str, start_secs: float, end_secs: float) -> bool:
    """Re-slice [start_secs, end_secs] of ``src`` into ``dst`` via ffmpeg.

    Used to re-cut a segment's raw WAV from the source's separated-vocals file
    when a user nudges its boundaries. Input-side ``-ss`` + ``-t`` is
    sample-accurate for WAV (every sample is a keyframe) and re-encodes to
    pcm_s16le so the output matches what diarisation originally wrote. Returns
    True on success; False if ffmpeg is missing, errors, or the range is empty.
    """
    duration = end_secs - start_secs
    if duration <= 0:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-ss", f"{start_secs:.6f}",
            "-i", src,
            "-t", f"{duration:.6f}",
            "-c:a", "pcm_s16le",
            dst,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


async def concat_wavs(srcs: list[str], dst: str) -> bool:
    """Concatenate ``srcs`` end-to-end into ``dst`` via ffmpeg (in order).

    Used to stitch multiple review segments into one clip. Each input is first
    normalised to a common format (44100 Hz stereo) so clips cut from different
    sources — which may differ in rate/channels — concat without error; cleanup
    later downmixes to the dataset's 22050/mono. A hard seam remains at each
    join (that's inherent to concatenation). Returns True on success.
    """
    if len(srcs) < 2:
        return False
    inputs: list[str] = []
    for s in srcs:
        inputs += ["-i", s]
    n = len(srcs)
    norm = "".join(
        f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}];"
        for i in range(n)
    )
    joined = "".join(f"[a{i}]" for i in range(n))
    filter_complex = f"{norm}{joined}concat=n={n}:v=0:a=1[out]"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:a", "pcm_s16le",
            dst,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0
