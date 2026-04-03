# Pipeline

**Status:** DRAFT  
**Last updated:** 2026-04-03

---

## Overview

The pipeline has five steps. In v1, step 5 (synthesis) is out of scope. Steps run in sequence per source file; transcription (step 3) runs across all diarised segments after all source files complete steps 1 and 2.

```
Step 1  Vocal Separation      per source file     Demucs
Step 2  Diarisation + Match   per source file     pyannote + cosine similarity
Step 3  Transcription         all matched segs    faster-whisper
Step 4  Cleanup + Export      approved segs only  FFmpeg
```

Any step can be re-run in isolation. Re-running step 1 for a file invalidates its step 2 output. Re-running step 2 does not affect transcriptions from other files. Re-running step 3 for a segment does not affect its approval state. These invalidation rules are enforced by the orchestrator — see [Data Models](data-models.md) for processing state transitions.

---

## Pre-step — Audio Extraction

**Tool:** FFmpeg (subprocess, called by orchestrator)  
**Input:** Video file (any container format FFmpeg supports)  
**Output:** `audio/raw/{source_id}.wav` — 16-bit PCM WAV, 44.1 kHz, stereo preserved

The orchestrator enqueues an `extract_audio` job immediately after writing the uploaded file to disk. Extraction runs FFmpeg as a subprocess within the orchestrator (not a separate service). This is a queued job, not a synchronous call — large video files (1–4 GB) can take 30–60+ seconds to extract, and the upload handler should return promptly. The source status moves to `extracting` when the job starts and `step1_pending` when it completes.

If extraction fails (corrupt file, unsupported codec), the source file is marked `extraction_failed` and the user is notified. Processing does not proceed for that file. The user must delete and re-upload.

Command:
```
ffmpeg -i {input} -vn -acodec pcm_s16le -ar 44100 {output}
```

---

## Step 1 — Vocal Separation

**Service:** `vocal-separation`  
**Tool:** Demucs  
**Input:** `audio/raw/{source_id}.wav`  
**Output:** `audio/vocals/{source_id}.wav`

### Processing

Demucs separates the audio into four stems: vocals, drums, bass, other. Only the vocals stem is retained. The other stems are discarded after processing.

Default model: `htdemucs`. This is Demucs v4's hybrid transformer model and the best general-purpose option. A fallback model (`mdx_extra`) is available if `htdemucs` produces poor output for a specific file; the orchestrator can request a specific model variant.

### GPU memory and chunking

Demucs processes audio in chunks internally. For very long files, the service first attempts whole-file processing. If a CUDA OOM error occurs, the service catches it, clears the cache, and retries with a chunk duration of 60 seconds with 1-second overlap between chunks. Chunks are processed sequentially and stitched before returning the output path.

If chunked processing also OOMs (very long file, low VRAM), the service returns an error with a `suggested_chunk_seconds` value derived from available VRAM. The orchestrator surfaces this to the user as an actionable message rather than a generic failure.

### Output quality

Demucs quality varies by content. Heavy background music, overlapping dialogue, and low-quality source audio all degrade separation. The orchestrator surfaces a warning when a source file has known risk factors (e.g. audio bitrate below 128 kbps). 

The user can trigger a re-run of step 1 for any source file, optionally selecting a different Demucs model. Re-running invalidates step 2 output for that file only.

### Error states

| State | Cause | Recovery |
|-------|-------|---------|
| `extraction_failed` | Corrupt or unsupported source audio | User replaces file |
| `oom_retry` | CUDA OOM on first attempt | Service retries with chunking automatically |
| `oom_fatal` | CUDA OOM even with chunking | User reduces chunk size or upgrades GPU |
| `model_error` | Demucs model load failure | Check model cache volume |

---

## Step 2 — Diarisation and Speaker Matching

**Service:** `diarisation`  
**Tools:** pyannote.audio, scipy cosine similarity  
**Input:** `audio/vocals/{source_id}.wav`, `reference.wav`  
**Output:** Segment WAVs written to `segments/raw/`, segment metadata returned to orchestrator for database storage

### Phase 1 — Diarisation

pyannote.audio segments the vocals audio into a speaker timeline. Each segment has a start time, end time, and an anonymous speaker label (`SPEAKER_00`, `SPEAKER_01`, etc.). Labels are local to this file — `SPEAKER_00` in episode 1 is not the same speaker as `SPEAKER_00` in episode 2.

pyannote parameters exposed to the orchestrator:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `min_segment_duration` | 1.0s | Discard segments shorter than this |
| `min_speakers` | 1 | Minimum speaker count hint |
| `max_speakers` | 10 | Maximum speaker count hint |

The orchestrator uses project defaults for v1. Parameter tuning is a future UI feature.

### Phase 2 — Speaker matching

The service extracts a speaker embedding from the reference clip using pyannote's embedding model. For each diarised speaker label, it computes an **average embedding** across all segments attributed to that speaker. It then computes cosine similarity between the reference embedding and each speaker's average embedding, assigning the resulting score to every segment belonging to that speaker.

Each segment receives a `match_confidence` score between 0.0 and 1.0. The full set of segments — all speakers, all confidence levels — is returned to the orchestrator in the job completion response. Nothing is discarded at this stage.

The orchestrator writes all segments to the database. Segments below the project's `match_threshold` are stored with status `below_threshold`; others with status `pending`. The user can lower the threshold to surface borderline matches.

### Segment WAV extraction

After scoring, the service slices `audio/vocals/{source_id}.wav` into individual WAV files, one per segment, using the diarisation timestamps. These are written to `segments/raw/{segment_id}.wav`.

Segments from different source files coexist in `segments/raw/`. Segment IDs are UUIDs, not sequential integers, to avoid collisions when processing multiple files.

### Low coverage warning

After processing a source file, the service reports the fraction of total audio attributed to the top matched speaker. If this is below 15% of the file duration, the orchestrator logs a `low_coverage` warning for that file. The UI surfaces this when the user views the project summary.

A full season with low per-episode coverage may still produce enough material in aggregate. The orchestrator tracks cumulative approved duration across all files and surfaces this in the project header.

### Error states

| State | Cause | Recovery |
|-------|-------|---------|
| `huggingface_token_missing` | First run, token not set | Set `HF_TOKEN` env var, restart service |
| `model_download_failed` | Network unavailable on first run | Check connectivity, retry |
| `reference_too_short` | Reference clip under 5 seconds | User uploads a longer clip |
| `diarisation_failed` | pyannote runtime error | Check logs, retry |

---

## Step 3 — Transcription

**Service:** `transcription`  
**Tool:** faster-whisper  
**Input:** Segment WAV paths, queried from database by orchestrator (segments with status `pending` or `maybe` and `transcript IS NULL`)  
**Output:** Per-segment transcript text and confidence scores, returned to orchestrator for database storage

### Scope

Transcription runs across all segments above the display threshold, not just high-confidence speaker matches. The transcript is a review signal alongside the speaker match score — a borderline speaker match with a coherent, accurate-looking transcript is worth keeping.

Transcription does not re-run automatically when a segment is re-processed via diarisation. It is triggered separately, either for all pending segments or for a specific segment.

### Model selection

| Model | Speed | Accuracy | Use case |
|-------|-------|----------|---------|
| `tiny` | Very fast | Low | Rough pass, large datasets |
| `base` | Fast | Moderate | Quick iteration |
| `small` | Moderate | Moderate-high | Balance of speed and quality |
| `medium` | Moderate-slow | High | Good quality on limited VRAM |
| `large-v2` | Slow | High | Default; production datasets |
| `large-v3` | Slow | Highest | Optional upgrade |

Default: `large-v2`. The model is set at project level and applies to all transcription runs in that project.

### Language

Default: auto-detect. For non-English or mixed-language content, set `language` explicitly at project level. Auto-detect adds a small overhead per segment and occasionally misidentifies short segments.

### Confidence scoring

faster-whisper returns per-word log-probabilities. The service converts these to a segment-level `transcript_confidence` score (mean of per-word probabilities, clamped to [0.0, 1.0]). This score is returned to the orchestrator alongside the transcript text and written to the database.

Very short segments (under 2 seconds) produce unreliable transcript confidence scores. The UI notes this when displaying such segments.

### Batch processing

The service transcribes segments in batches of 16 by default, using GPU batching where available. Progress is reported back to the orchestrator after each batch so the UI can show a running count.

### Error states

| State | Cause | Recovery |
|-------|-------|---------|
| `model_load_failed` | Model cache missing or corrupt | Clear cache volume, restart |
| `segment_too_short` | Under 0.5 seconds | Segment flagged; transcript set to empty string |
| `transcription_timeout` | Segment unusually long (>60s) | Segment flagged; manual review |

---

## Step 4 — Cleanup and Normalisation

**Service:** `cleanup`  
**Tool:** FFmpeg (subprocess)  
**Input:** Approved segment WAV paths  
**Output:** `export/{segment_id}.wav`

This step runs only when the user triggers an export. It processes approved segments only.

### Processing chain

Applied in order per segment:

**1. Loudness normalisation**  
Two-pass EBU R128 normalisation. Target: -23 LUFS, true peak -2 dBTP, LRA 7 LU. This produces consistent volume across segments from different episodes, recording environments, and source quality levels.

```
ffmpeg -i {input} -af loudnorm=I=-23:TP=-2:LRA=7:print_format=json {pass1_output}
ffmpeg -i {input} -af loudnorm=I=-23:TP=-2:LRA=7:measured_I={i}:measured_TP={tp}:measured_LRA={lra}:measured_thresh={thresh}:linear=true {output}
```

**2. Silence trimming**  
Remove leading and trailing silence above -50 dBFS with a minimum silence duration of 0.1 seconds. Preserves natural breath and short pauses within the segment.

**3. High-pass filter**  
Gentle high-pass at 80 Hz to remove low-frequency rumble and handling noise. TV audio rarely contains meaningful content below 80 Hz for voice.

**4. Clipping detection**  
Scan for samples at or above -0.1 dBFS for more than 3 consecutive samples. Segments that clip are flagged `clipping_warning` rather than silently excluded. The export proceeds but the manifest records the flag. The user can choose to reject these segments and re-export.

### Output format

Output WAVs: 16-bit PCM, 22.05 kHz mono. This matches XTTS-v2 training requirements. The cleanup service performs the sample rate conversion and channel downmix as part of the FFmpeg pass.

### Error states

| State | Cause | Recovery |
|-------|-------|---------|
| `ffmpeg_error` | FFmpeg runtime failure | Check logs; usually a corrupt input segment |
| `clipping_warning` | Segment clips after normalisation | Review and optionally reject; re-export |
| `silent_after_trim` | Segment is all silence | Segment auto-rejected; user notified |

---

## Export

**Handler:** Orchestrator  
**Triggered by:** User action in the UI

After cleanup completes, the orchestrator writes `export/manifest.json` from the database. The manifest includes full metadata per segment for provenance and quality filtering during training:

```json
{
  "version": "1",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "exported_at": "2026-04-03T14:32:00Z",
  "speaker": "target",
  "segments": [
    {
      "id": "7f3c2a1b-...",
      "audio_file": "7f3c2a1b.wav",
      "text": "The transcript text for this segment.",
      "source": "s01e01.mkv",
      "start_secs": 142.31,
      "end_secs": 146.88,
      "duration_secs": 4.57,
      "match_confidence": 0.91,
      "transcript_confidence": 0.88
    }
  ],
  "stats": {
    "segment_count": 743,
    "total_duration_secs": 4821.3
  }
}
```

The `text` field uses `COALESCE(transcript_edited, transcript)`. Segments with no transcript (edge case: transcription failed for a segment the user approved anyway) are excluded from the manifest with a warning logged. The `audio_file` path is relative to the export directory.

The orchestrator then packages `export/` into a `.tar.gz` archive and makes it available for download. The archive includes the WAVs and `manifest.json` only — no intermediate files.

Export is non-destructive. Re-exporting after changing approvals regenerates `export/` from scratch without affecting the database or `segments/raw/`.

---

## Re-processing rules

| Re-run | Invalidates | Preserves |
|--------|------------|---------|
| Step 1 (one file) | Step 2 output for that file; segment WAVs from that file; transcripts for those segments | Approvals and transcripts for all other files |
| Step 2 (one file) | Segment WAVs and transcripts for that file | Approvals for all other files; step 1 output |
| Step 3 (one segment) | Transcript for that segment | Approval state for that segment |
| Step 3 (all) | All transcripts | All approval states |
| Step 4 / export | Export directory | All approval states; all transcripts |

The orchestrator enforces these rules. A re-run request that would invalidate approved segments from other files must be confirmed by the user before it proceeds. Re-running step 2 for a single episode does not touch approvals from other episodes.
