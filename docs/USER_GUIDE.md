# FlipSync user guide

How to operate FlipSync once it's running: from an empty project to an exported dataset.

This guide assumes FlipSync is already deployed and reachable at `http://localhost:3000` (or wherever you mapped it). If it isn't, see the [Quick start](../README.md#quick-start) and [`spec/deployment.md`](../spec/deployment.md) first.

Everything here happens in the browser. There's no command line and no audio editing.

---

## The workflow at a glance

1. **Create a project** — one target speaker per project.
2. **Upload source videos** — the files you want to extract dialogue from.
3. **Set the speaker** — upload a short reference clip, **or** let FlipSync scan a video for speakers and pick your target by ear.
4. **Run the pipeline** — separation → diarisation + matching → transcription → cleanup.
5. **Review segments** — approve, defer, or reject each candidate in the review queue.
6. **Export** — download a labelled WAV archive plus a training manifest.

The dashboard walks you through this as five stages — **Upload → Speaker → Process → Review → Export** — with a single "what's next" card showing the action the project needs right now. Steps 1–2 are setup, step 3 can happen before the run or as a pause partway through it (see below), step 4 runs unattended, step 5 is where you'll spend most of your time, and step 6 is one click.

---

## 1. Create a project

From the project list (`/`), click **New project**. A project holds one target speaker, its source files, and all the segments extracted for that speaker.

You set five things:

| Setting | Default | What it does |
|---------|---------|--------------|
| **Name** | — | Free text. How the project appears in the list. |
| **Whisper model** | `large-v3` | Transcription accuracy vs. speed. `large-v3` is recommended; drop to `medium` or `small` if you're VRAM-limited. |
| **Language** | Auto-detect | Language of the dialogue, or let Whisper detect it. |
| **Match threshold** | `0.75` | How closely a segment must match your reference clip to be kept. Segments below this are hidden by default (marked *below threshold*, not deleted). Lower it later to surface borderline matches. |
| **Target duration** | 30 min | Drives the progress bar only. A guide for how much approved audio you're aiming for, not a hard limit. |

Sensible starting point for voice cloning: aim for **30+ minutes** of approved, clean audio. Name, match threshold, target duration, Whisper model, and language can all be changed later from the dashboard.

---

## 2. Upload source videos

On the project dashboard (`/projects/{id}`), add your video files. Common container formats work (`.mkv`, `.mp4`, and so on), and files up to **10 GB** each are accepted.

As soon as a file finishes uploading, FlipSync extracts its audio track automatically — you don't trigger this. Each video shows a plain status in the list:

- **Extracting audio** → the audio track is being pulled from the video
- **Queued** → ready for the pipeline
- **Extraction failed** → something went wrong (bad file, unsupported codec); check the error on the dashboard

Wait for your videos to reach *Queued* before starting the pipeline. You can upload more later and re-run the pipeline for just those.

> **Large uploads:** video files are big. Uploads stream to disk as they arrive, so a 1–4 GB file is fine, but let each one finish before navigating away.

---

## 3. Set the speaker

This is the single most important input for match quality. Diarisation splits the audio by speaker; the reference is what FlipSync compares each speaker against to decide *which one is yours*. There are two ways to provide it.

### Option A — upload a reference clip

Upload one audio clip of the target speaker at the **Speaker** stage on the dashboard. Requirements and tips:

- **Minimum 5 seconds.** Longer is better — 15–30 seconds of clean speech is ideal.
- **One speaker only.** No overlapping dialogue, music, or effects. A clean solo line is worth more than a long noisy one.
- **Representative.** Use audio of the same speaker in a similar recording condition to your sources.

### Option B — pick a speaker from a video

No clean clip to hand? Start the pipeline anyway. Once vocal separation finishes, the dashboard pauses at the **Speaker** stage. In the *Find speakers* tab:

1. Choose a video that's been separated and click **Scan for speakers**.
2. FlipSync detects the speakers in that video and builds a short audio montage for each, listed by talk time — the target speaker is usually near the top.
3. Play the montages until you find your speaker, then click **Use this voice**. The montage becomes the reference.
4. Click **Continue** — processing resumes where it paused.

A speaker with under 5 seconds of talk time can't be picked (same minimum as an uploaded clip). Scanning again replaces the previous candidate list, and you can re-pick a different speaker from the same scan without rescanning.

Either way, replacing the reference does **not** automatically re-run matching — re-run speaker matching (per-video ⋯ menu) for the new reference to take effect on already-processed videos.

---

## 4. Run the pipeline

On the dashboard, click **Start processing**. FlipSync runs four steps, in order, for each source:

1. **Vocal separation** (Demucs) — strips music and effects, isolates the vocal track. On the first run the models download (~5 GB total, cached afterwards), so the first job is slow.
2. **Diarisation + speaker matching** (pyannote) — splits the vocal track by speaker and scores each speaker against your reference clip. Every segment gets a **speaker match** score.
3. **Transcription** (faster-whisper) — transcribes the matched segments. The transcript is a review signal, not just a label: a borderline match with a clean, sensible transcript is often worth keeping.
4. **Cleanup** (FFmpeg) — loudness-normalises, trims leading/trailing silence, filters low-frequency noise, and flags clipping.

If no reference is set when you start, the pipeline runs vocal separation for every video and then pauses at the **Speaker** stage — set a speaker (upload or scan-and-pick, see above) and click **Continue**.

**Jobs run one at a time per project, and GPU jobs one at a time across the whole machine** — there's no parallel GPU work, so a full season takes a while. You don't need to babysit it; state is saved server-side and survives closed tabs and restarts.

Watch progress on the dashboard:

- **The next-action card** shows the running job and its progress.
- **The videos list** shows each file's status in plain terms (*Separating vocals*, *Matching speaker*, *Processed*), speaker coverage, and a low-coverage warning if the target speaker accounts for less than ~15% of that video.
- **Failed-job alerts** show any failures with the error message and a **Retry** where retrying makes sense. A failed extraction means the file itself is the problem — remove that video and re-upload it. Retrying separation or speaker matching asks for confirmation first if it would discard approved segments.

When the pipeline finishes, open the **review queue** from the dashboard.

---

## 5. Review segments

The review queue (`/projects/{id}/review`) is the main workspace: a segment list on the left, a detail panel on the right. Selecting a segment loads it for review.

### What each segment tells you

- **Speaker match** score — colour-coded: green ≥ 0.90, amber 0.75–0.89, red < 0.75.
- **Transcript confidence** — shown once transcription is done.
- **Waveform** (toggle to spectrogram), audio player, duration, source file and timestamp.
- **Flags** — e.g. *short segment* (transcript may be unreliable) or a *clipping* warning.

### Keyboard model

Review is built to be driven from the keyboard. Keys are active whenever a segment is loaded (the detail panel has focus):

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `R` | Restart playback |
| `A` | Approve and advance to next |
| `M` | Maybe (defer) and advance |
| `X` | Reject and advance |
| `J` | Next segment (no decision) |
| `K` | Previous segment |
| `E` | Edit the transcript |
| `[` / `]` | Slower / faster playback |
| `Esc` | Cancel a transcript edit |
| `?` | Show the shortcut overlay |

After A/M/X, focus jumps to the next segment automatically, so you can work down the queue without touching the mouse. **Approve** = keep it in the dataset. **Maybe** = come back to it. **Reject** = drop it. (Rejected by mistake? Open the segment and click **Un-reject** to put it back to pending.)

### Auto-approval

By default, FlipSync approves the easy wins for you. When a segment's transcript lands, if both its speaker match and transcript confidence clear the auto-approve thresholds — and it carries no warnings — it moves to **auto-approved**, shown teal in the list and timeline. Auto-approved segments count towards the export and the duration progress bar, so you don't have to touch them; your review time goes to the uncertain middle instead (sort by *Uncertainty* to see the most borderline segments first).

Every auto-approval is still yours to override: press `A` to confirm it as approved, or Maybe/Reject to demote it. The two thresholds, and the toggle to turn auto-approval off, live in the project settings on the dashboard — changing them re-sorts the pending/auto-approved split immediately. When you trust the batch, the **Confirm all auto-approved** bulk preset converts them to approved in one move.

### Editing transcripts

Click the transcript (or press `E`) to edit it inline. Your edit is saved to a separate field, so the original whisper output is preserved and an **undo edit** option restores it. Editing is worth it: the transcript ships in the export manifest.

### Filtering, sorting, and the timeline

The filter bar narrows the list by status, source, minimum confidence, minimum duration, and sort order. **Filter state lives in the URL**, so you can bookmark or share a filtered view. Below it, a **timeline** strip shows every segment as a coloured bar by status — handy for seeing where approved dialogue clusters and where the gaps are. Click a bar to jump to that segment.

### Bulk operations

For the obvious cases, open **Bulk actions** above the list. Presets cover the common moves — confirm all auto-approved, approve all pending ≥ 0.90, reject all pending under 1.5–2.0 seconds, reset *maybe* back to pending — and a custom builder lets you filter by action, status, confidence, duration, and source with a **live preview count** before you apply. The count reflects only segments the chosen action can legally touch (approving never touches rejected segments, for instance). Do a bulk pass first, then hand-review what's left.

### Tuning the threshold

If the queue looks thin, lower the **match threshold** on the dashboard. Segments that were *below threshold* move back to *pending* so you can review them; your existing approve/reject decisions are never touched. Raising the threshold does the reverse. You can also re-run transcription for newly surfaced segments from the dashboard.

---

## 6. Export

The **Export** button sits in the review queue header. It's greyed out until you have at least one approved or auto-approved segment — both are included in the export.

Clicking it shows a confirmation panel with the counts (approved, and how many of those were auto-approved) and total duration, plus cautions for any segments with clipping warnings or missing transcripts (the clipping caution links to a filtered view so you can review those first). Confirm, and FlipSync runs a final clean/normalise pass and builds the archive. When it's done, the panel becomes a **Download** link.

### What you get

- **Labelled WAV files** — 22.05 kHz mono, one per approved segment.
- **`manifest.json`** — segment metadata and transcripts, formatted for **XTTS-v2** training with no post-processing.

A segment approved without a transcript is excluded from the manifest unless you add one. Exporting again replaces the previous export — but only once the new one succeeds; a failed re-export leaves the previous archive intact and downloadable.

---

## Re-running and iterating

FlipSync is built to be iterative — any step can be re-run without losing your review decisions:

- **Reprocess a video** (vocal separation and/or speaker matching) from its ⋯ menu on the dashboard, e.g. to try a different Demucs model on a noisy file. If this would discard approved segments, you'll be asked to confirm.
- **Re-run transcription** for all untranscribed segments, or re-transcribe a single segment (your manual edits are preserved).
- **Adjust the threshold** at any time to widen or narrow the review pool.

---

## Troubleshooting

| Symptom | What to do |
|---------|-----------|
| **Pipeline paused — "Needs speaker"** | Vocal separation finished but there's no reference to match against. At the Speaker stage on the dashboard, upload a clip or scan a video and pick your speaker, then click **Continue**. |
| **Thin dataset / low-coverage warning** | The target speaker is a small share of that source. Add more sources, or lower the match threshold and review the borderline segments. |
| **Everything scores red** | Your reference is probably noisy or mixed. Replace it with a cleaner solo sample (or scan and pick a different speaker), then re-run speaker matching. |
| **A source failed extraction** | Check the error on the dashboard — usually an unsupported or corrupt file. Re-upload or re-encode it. |
| **Clipping warnings on approved segments** | These flag possible distortion. Listen and decide; the warning icon stays even after re-approval because it's a fact about the audio, not a workflow state. |
| **First job is very slow** | Models download on the first run (~5 GB) and cache to disk. Subsequent runs are fast. |

For anything deeper — the data model, service APIs, per-step processing detail — see the [specification](../spec/README.md).
