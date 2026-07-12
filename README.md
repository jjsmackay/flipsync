# FlipSync

Extract clean, speaker-specific dialogue audio from video files. Produce datasets ready for voice cloning.

Drop in a video collection, point FlipSync at your target speaker — upload a short reference clip, or scan a video and pick the speaker by ear — and walk away with labelled WAV files and a training manifest. No command line, no audio engineering knowledge required.

---

## Use cases

**Voice banking.** If you have a degenerative illness that will take your voice, FlipSync can extract a corpus from home videos or recordings before that happens. Feed the output into a voice cloning model and preserve your voice for use as a TTS assistant.

**Home automation.** Clone a voice you love for your home assistant. The immediate use case that prompted this project: extracting a TV character's dialogue across multiple seasons to use as a Rhasspy or Home Assistant TTS voice.

**Indie game development.** Build character voice datasets without full voice actor budgets.

**Research and linguistics.** Produce speaker-specific audio corpora from existing recordings.

---

## How it works

FlipSync runs four processing steps in sequence:

1. **Vocal separation** (Demucs) — strips music and effects, isolates the vocal track
2. **Diarisation + speaker matching** (pyannote.audio) — segments the audio by speaker, scores each segment against your reference clip
3. **Transcription** (faster-whisper) — transcribes every matched segment; transcript quality is a review signal alongside the speaker match score
4. **Cleanup** (FFmpeg) — normalises loudness, trims silence, filters low-frequency noise

You review the results in a browser UI: listen to each segment, read the transcript, approve or reject. Keyboard-driven. Bulk operations for the obvious cases. Export when done.

Output: labelled 22kHz mono WAV files and a `manifest.json` compatible with XTTS-v2 training.

---

## Requirements

- Linux with an Nvidia GPU (6 GB VRAM minimum, 10 GB+ recommended)
- CUDA 11.8+
- Docker 24.0+ with the [Nvidia Container Runtime](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- A free [HuggingFace](https://huggingface.co) account (for pyannote model licences — one-time step)

GPU is a hard requirement. Demucs and pyannote will not run acceptably on CPU.

---

## Quick start

```bash
git clone https://github.com/jjsmackay/flipsync.git
cd flipsync

cp .env.example .env
# Add your HuggingFace token to .env — the file has step-by-step instructions,
# including the gated pyannote repos you must accept first.

docker compose up -d
```

Open [http://localhost:3000](http://localhost:3000).

Service images are pulled prebuilt from GitHub Container Registry (`ghcr.io/jjsmackay/flipsync/*`), so there's no local build step. On the first processing job, the Demucs, pyannote, and Whisper models download (~5 GB total) and cache to disk. This happens once; subsequent runs are fast.

Ports and a few deployment options (custom hostnames behind a reverse proxy, model-cache location) are configurable via `.env` — see the comments there and [`spec/deployment.md`](spec/deployment.md).

---

## Using FlipSync

Once it's running, everything happens in the browser — no command line:

1. **Create a project** — one target speaker per project.
2. **Upload source videos** — audio is extracted automatically as each file lands.
3. **Set the speaker** — upload a reference clip (5+ seconds of clean speech), or scan a video for speakers and pick your target by ear; this is what matching scores against.
4. **Run the pipeline** — click *Start processing*. The four steps run in sequence, one job at a time (the pipeline pauses for step 3 if you haven't set a speaker yet).
5. **Review segments** — keyboard-driven approve / maybe / reject in the review queue, with filters, a timeline, and bulk operations for the easy cases.
6. **Export** — download the labelled WAVs and `manifest.json`.

Full walkthrough — including the keyboard shortcuts, threshold tuning, and troubleshooting — in **[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)**.

---

## Documentation

- **[User guide](docs/USER_GUIDE.md)** — how to operate FlipSync end to end.
- **[Specification](spec/README.md)** — design documents (architecture, data models, API contracts, per-step pipeline detail). Start with [`spec/overview.md`](spec/overview.md).

---

## Status

**Building in the open.** The spec is complete and all five build waves are implemented — orchestrator, the four processing services, and the review UI. Current focus is end-to-end hardening and deployment shake-out, so treat it as pre-release rather than production-ready.

Issues and spec feedback welcome.

---

## License

Apache 2.0. See [LICENSE](LICENSE).

FlipSync assumes you have rights to your source material.
