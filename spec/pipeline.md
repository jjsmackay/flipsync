# Pipeline

**Status:** DRAFT  
**Last updated:** 2026-07-13

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

**The reference gate.** Step 2 needs a `reference.wav` to match against. If the project has no reference when the pipeline starts, an explicit **"Set reference" stage** sits between steps 1 and 2: the orchestrator runs step 1 for every source but does not chain step 2, and the project rests in `awaiting_reference` once those jobs drain. The user sets a reference — by uploading a clip, or by scouting a source's vocals stem and picking a detected speaker (see [Step 2 — Scout mode](#scout-mode-reference-acquisition) below) — then triggers `pipeline/continue` to run step 2 for the sources waiting on it. If a reference is already set (uploaded previously, or picked on a prior run) this stage is skipped entirely and the pipeline runs straight through as before.

---

## Pre-step — Audio Extraction

**Tool:** FFmpeg (subprocess, called by orchestrator)  
**Input:** Video file (any container format FFmpeg supports)  
**Output:** `audio/raw/{source_id}.wav` — 16-bit PCM WAV, 44.1 kHz, stereo preserved

The orchestrator enqueues an `extract_audio` job immediately after writing the uploaded file to disk. Extraction runs FFmpeg as a subprocess within the orchestrator (not a separate service). This is a queued job, not a synchronous call — large video files (1–4 GB) can take 30–60+ seconds to extract, and the upload handler should return promptly. The source status moves to `extracting` when the job starts and `separation_pending` when it completes.

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

Default model: `htdemucs_ft` (the per-stem fine-tuned Demucs v4 bag — cleaner vocals than plain `htdemucs` at ~4× runtime, acceptable for an offline first stage). Fallbacks `htdemucs` and `mdx_extra` remain selectable; the orchestrator requests a specific variant per project via `demucs_model`. Changing the model does not retro-apply — reprocess Step 1 to adopt it.

An alternative separation backend, `bs_roformer` (BS-RoFormer via the `audio-separator` package), is selectable through the same `demucs_model` config. It typically yields cleaner vocals than Demucs at higher VRAM cost. It runs in the same vocal-separation service, chosen per project; the OOM chunk-retry path (`retry_with_chunk_secs`) applies to the Demucs backends only — RoFormer manages its own internal segmentation.

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
| `num_speakers` | *(none)* | Scout only. When set, forces pyannote to this exact speaker count; `min`/`max` are ignored |

The orchestrator uses project defaults for match mode. Scout mode exposes `num_speakers` to the user via the Set-reference panel's advanced drawer (see [Scout mode](#scout-mode-reference-acquisition)); broader parameter tuning for match mode is a future UI feature.

### Phase 2 — Speaker matching

The service extracts a speaker embedding from the reference clip using pyannote's embedding model, then scores **each segment individually**: it extracts an embedding for the segment (same embedding model, cropped to the segment's time range) and sets that segment's `match_confidence` to the cosine similarity between the segment's own embedding and the reference embedding. Per-segment scoring exists because a diarisation cluster can mix two speakers — a single cluster-averaged score would smear across all of that cluster's segments and mislead auto-approve banding, which trusts `match_confidence` per segment.

The cluster-level score is kept as a **secondary signal**. For each diarised speaker label, the service computes an average embedding across all segments attributed to that speaker (reusing the per-segment embeddings — no extra extraction) and scores it against the reference the same way. This per-cluster score is reported on every segment as `speaker_match_confidence`, so reviewers can see both "this clip sounds like the target" (`match_confidence`) and "this cluster overall matches the target" (`speaker_match_confidence`).

**Short-segment fallback.** Embeddings extracted from very short windows are noisy. For segments shorter than 1.0 s, the per-segment embedding is not trusted: `match_confidence` falls back to the segment's cluster score (i.e. equals `speaker_match_confidence`). The same fallback applies when per-segment embedding extraction fails — an extraction failure on one segment must never fail the job. (With the default `min_segment_duration` of 1.0 s no sub-second segments survive the duration filter, but the parameter can be lowered.)

Each segment receives `match_confidence` and `speaker_match_confidence` scores between 0.0 and 1.0. The full set of segments — all speakers, all confidence levels — is returned to the orchestrator in the job completion response. Nothing is discarded at this stage.

The orchestrator writes all segments to the database. Segments below the project's `match_threshold` are stored with status `below_threshold`; others with status `pending`. The user can lower the threshold to surface borderline matches.

### Segment WAV extraction

After scoring, the service slices `audio/vocals/{source_id}.wav` into individual WAV files, one per segment, using the diarisation timestamps. These are written to `segments/raw/{segment_id}.wav`.

Segments from different source files coexist in `segments/raw/`. Segment IDs are UUIDs, not sequential integers, to avoid collisions when processing multiple files.

### Low coverage warning

After processing a source file, the service reports the fraction of total audio attributed to the top matched speaker. If this is below 15% of the file duration, the orchestrator logs a `low_coverage` warning for that file. The UI surfaces this when the user views the project summary.

A full season with low per-episode coverage may still produce enough material in aggregate. The orchestrator tracks cumulative approved duration across all files and surfaces this in the project header.

### Scout mode (reference acquisition)

A reference-less diarisation pass over one source, used to derive a reference from the source material itself instead of an uploaded clip. Triggered by `POST /projects/{project_id}/reference/scout` (see [API Contracts](api-contracts.md)) and available once that source has a vocals stem (step 1 complete).

The service runs the same pyannote diarisation as Phase 1 above, producing anonymous speaker clusters (`SPEAKER_00`, `SPEAKER_01`, …), but skips Phase 2 entirely — there is no reference embedding yet to match against. If the request carries `num_speakers`, pyannote is forced to that exact count (this is how the user resolves a cluster that has merged two people); otherwise the default `min`/`max` range applies. No `match_confidence` is computed.

For each speaker, it writes a **bounded pool of individual turn WAVs** to `output_dir/{speaker_label}/{index}.wav` — the speaker's turns taken whole, longest-first, until the pool reaches `pool_max_secs` (default 90.0) or `pool_max_turns` (default 20). The pool is bounded because only the longest turns can ever enter a 30 s reference, so a 1-hour source yields no larger a pool than a short one. The service returns, per speaker, the pool as a list of `{index, start, end, duration}`.

The pool serves two purposes: each turn is individually playable so the user can hear the cluster and exclude any wrong-voice turn, and the reference is assembled from the kept turns. The **reference is not built by the service** — the orchestrator assembles it at pick time from the pool minus the excluded turns (longest-first up to a 30 s cap; see [`POST /reference/scout/select`](api-contracts.md)). Excluding a turn lets the next-longest kept turn take its place — the reference stays full-length.

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

### Sentence-aligned re-segmentation

Diarisation turns are speaker-attribution units, not good TTS training units — a turn can be a 40-second monologue or a sub-second fragment. TTS training wants sentence-ish utterances of roughly 1–15 seconds. The transcription service therefore re-segments eligible segments on sentence boundaries as part of transcription.

**Eligibility.** The orchestrator marks a segment `resegment: true` in the job request only when the segment is untranscribed (`transcript IS NULL`) and its status is `pending` or `below_threshold`. Segments the user has touched (`maybe`, `approved`, `rejected`) and single-segment re-transcription (`transcription_segment` jobs) are never re-segmented — transcription only fills in the transcript.

**Splitting.** The service transcribes with word-level timestamps enabled. It splits the word sequence into utterances at:

- sentence-terminal punctuation (`.`, `!`, `?`, `…`, optionally followed by a closing quote or bracket), and
- inter-word silence gaps ≥ 0.6 seconds.

Utterances are then normalised to the target band:

- An utterance longer than 15 seconds is force-split at its largest internal inter-word gap (repeated until all pieces are ≤ 15 s).
- An utterance shorter than 1 second is merged with its following utterance (or preceding, if it is the last) provided the merged duration stays ≤ 15 s.

If the result is a single utterance spanning the whole segment — or the segment produced no words — the segment is returned unsplit, exactly as a non-resegmented transcription.

**Child boundaries.** Adjacent children split the inter-word silence between them evenly: each child's end and its successor's start sit at the midpoint of the gap between the last word of one utterance and the first word of the next. The first child starts at the parent's start; the last child ends at the parent's end. No audio is lost or duplicated.

Whisper word timestamps can overshoot the audio, so every boundary is **clamped** to `[0, file duration]` and edges are forced non-decreasing — a child can never have `end <= start`. If clamping collapses a child to near-zero length (< 0.05 s), its text is merged into the neighbouring child rather than emitting a degenerate row.

**Child WAVs.** The service slices child WAVs from the parent segment WAV and writes them to the same directory, using full UUID filenames it generates. Timestamps returned for children are absolute (parent `start_secs` + in-segment offsets).

**Orchestrator handling.** When a completed segment arrives with `children`, the orchestrator first **re-checks eligibility at write time** — the results land minutes after eligibility was snapshotted at submit, and the user may have acted in between. The parent must still have status `pending`/`below_threshold`, `transcript IS NULL`, and `transcript_edited IS NULL`. If it does, the orchestrator — in one transaction — inserts one row per child (inheriting `source_id`, `speaker_label`, `match_confidence`, and status from the parent; transcript and confidence from the child; a `short_transcript` flag if the child is under 2 seconds) and deletes the parent row. The parent WAV file is deleted best-effort after the transaction commits. Any child with `end_secs <= start_secs` is defensively skipped.

If the parent is **no longer eligible** — or every child was skipped — nothing is split and nothing is deleted: the children's texts are joined (single spaces) into the parent's `transcript`, `transcript_confidence` is set to the minimum child confidence, and the normal short-transcript flag and auto-approve evaluation apply as for a plain write. Either way the result is deduplicated on the parent id. Approval state is never at risk.

### Optional word alignment

When a project has `align_words` enabled, the service runs a wav2vec2 forced-alignment pass over each segment's whisper words before re-segmentation, replacing whisper's heuristic word start/end times with alignment-derived ones. It refines only timestamps — word text and per-word probability (and thus `transcript_confidence` and auto-approval) are unchanged. Alignment affects re-segmentation boundaries only; for a segment that is not re-segmented it is a no-op. Off by default. Changing it does not retro-apply — re-transcribe to adopt.

### Auto-approval

Transcription completion is the moment both review signals (speaker match and transcript confidence) exist, so this is where auto-approval is applied. Segments that clear both confidence bars are moved to `auto_approved` — provisionally included in the export, visibly badged, and demotable — so human review can focus on the uncertain middle band.

A segment is eligible for auto-approval when all of the following hold:

- the project has `auto_approve_enabled` set (default: enabled)
- status is `pending`
- the effective transcript is non-empty
- `match_confidence >= max(match_threshold, auto_approve_match_threshold)`
- `transcript_confidence >= auto_approve_transcript_threshold`
- `flags` is empty and `clipping_warning` is 0

Auto-approval runs when transcription results are written (parents or children), and re-runs synchronously when the user changes any of the thresholds via `PATCH /projects` — see [API Contracts](api-contracts.md). It never touches user-set statuses; only `pending` ↔ `auto_approved` moves are ever made by the system.

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
**Input:** Approved segment WAV paths (statuses `approved`, `auto_approved`, and `clipping_warning`)  
**Output:** `export/{segment_id}.wav`

This step runs only when the user triggers an export. It processes the export set — `approved` and `auto_approved` are treated identically here and in the manifest, and `clipping_warning` segments are included too (keep-unless-rejected: a segment flagged for clipping stays in the export, flag recorded, unless the user rejects it). Cleanup will re-flag clipping segments; they simply retain the `clipping_warning` status.

### Processing chain

Applied in order per segment:

**1. Loudness normalisation**  
Two-pass EBU R128 normalisation. Target: -23 LUFS, true peak -2 dBTP, LRA 7 LU. This produces consistent volume across segments from different episodes, recording environments, and source quality levels.

```
ffmpeg -i {input} -af loudnorm=I=-23:TP=-2:LRA=7:print_format=json {pass1_output}
ffmpeg -i {input} -af loudnorm=I=-23:TP=-2:LRA=7:measured_I={i}:measured_TP={tp}:measured_LRA={lra}:measured_thresh={thresh}:linear=true {output}
```

**2. Silence trimming**  
Remove leading and trailing silence above -50 dBFS with a minimum silence duration of 0.1 seconds. Preserves natural breath and short pauses within the segment. Skipped entirely when `do_trim_silence` is false — the diariser's boundaries are kept as-is (useful when trimming eats speech onsets).

**3. High-pass filter**  
Gentle high-pass at 80 Hz to remove low-frequency rumble and handling noise. TV audio rarely contains meaningful content below 80 Hz for voice.

**4. Silence padding**  
Re-add a small amount of digital silence to the head (`silence_pad_start_secs`, default 0.05 s) and tail (`silence_pad_end_secs`, default 0.2 s) after trimming, via `adelay`/`apad`. A hard cut at the word boundary gives the voice-clone model an abrupt onset/offset and can click; the pad restores a clean attack and decay. Applied **after** the silent-after-trim reject check, so padding never resurrects an empty segment. Set either to 0 to disable that edge.

**5. Clipping detection**  
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
      "source_id": "...",
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

**Staging.** A new export is built in `export_tmp/` inside the project directory; the previous `export/`, manifest, and archive stay intact — and downloadable — until the replacement is complete. Only on success does the orchestrator atomically swap the staging directory into `export/`, rewrite the archive, and update `export_path`s and `exported_at` in one transaction. On any failure the staging directory is removed best-effort and the previous export is untouched. Stale staging directories are cleaned up at the start of the next export.

Export is non-destructive. Re-exporting after changing approvals regenerates `export/` (via the staging swap above) without affecting the database or `segments/raw/`.

---

## Re-processing rules

| Re-run | Invalidates | Preserves |
|--------|------------|---------|
| Step 1 (one file) | Step 2 output for that file; segment WAVs from that file; transcripts for those segments | Approvals and transcripts for all other files |
| Step 2 (one file) | Segment WAVs and transcripts for that file | Approvals for all other files; step 1 output |
| Step 3 (one segment) | Transcript for that segment | Approval state for that segment |
| Step 3 (all) | All transcripts | All approval states |
| Step 4 / export | Export directory | All approval states; all transcripts |

The orchestrator enforces these rules. A re-run request that would invalidate approved segments (including `auto_approved`) must be confirmed by the user before it proceeds. Re-running step 2 for a single episode does not touch approvals from other episodes.

One addition to the table above: bulk transcription may **replace** an untranscribed `pending` or `below_threshold` segment with sentence-aligned children (new rows, new UUIDs — see Step 3, sentence-aligned re-segmentation). This never touches reviewed segments; approval state is structurally unaffected because only unreviewed segments are eligible.
