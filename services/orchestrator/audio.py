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
