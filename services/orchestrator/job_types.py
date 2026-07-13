"""Per-job-type properties — the single registry every consumer derives from.

A leaf module (imports nothing from this app) so both jobs.py and status.py
can use it without circular imports. Adding a job type means adding one entry
here plus its handler in jobs.HANDLERS — an import-time assertion in jobs.py
fails loudly if the two ever disagree.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class JobSpec:
    # Processing service the handler submits to (None = fully in-process,
    # e.g. FFmpeg extraction). Also used for the pre-GPU-lock readiness wait.
    service: str | None = None
    # GPU-bound: serialises under the host-wide GPU lock — at most one GPU job
    # runs across ALL projects at any time (they share the GPU; concurrent GPU
    # jobs contend for VRAM and OOM). CPU jobs are not gated.
    gpu: bool = False
    # Voice jobs (v1.5 XTTS work) are excluded from the active-jobs count that
    # drives project-status recomputation: a running dataset build, fine-tune
    # or preview must not flip the project out of 'review'/'exported' (which
    # would block export). They still appear in active_jobs API responses,
    # still block project deletion, and still serialise via the per-project
    # FIFO and the host-wide GPU lock.
    voice: bool = False


JOB_TYPES: dict[str, JobSpec] = {
    "extract_audio": JobSpec(),
    "vocal_separation": JobSpec(service="vocal_separation", gpu=True),
    "diarisation": JobSpec(service="diarisation", gpu=True),
    "scout_speakers": JobSpec(service="diarisation", gpu=True),
    "transcription_bulk": JobSpec(service="transcription", gpu=True),
    "transcription_segment": JobSpec(service="transcription", gpu=True),
    "export": JobSpec(service="cleanup"),
    "dataset_build": JobSpec(service="cleanup", voice=True),
    "finetune": JobSpec(service="xtts", gpu=True, voice=True),
    "preview": JobSpec(service="xtts", gpu=True, voice=True),
}

GPU_JOB_TYPES = frozenset(t for t, spec in JOB_TYPES.items() if spec.gpu)
GPU_JOB_SERVICES: dict[str, str] = {
    t: spec.service for t, spec in JOB_TYPES.items() if spec.gpu
}
VOICE_JOB_TYPES = frozenset(t for t, spec in JOB_TYPES.items() if spec.voice)
