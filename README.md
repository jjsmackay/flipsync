# FlipSync

Extract clean, speaker-specific dialogue audio from video files. Produce datasets ready for voice cloning.

Drop in a video collection, upload a short reference clip of your target speaker, and walk away with labelled WAV files and a training manifest. No command line, no audio engineering knowledge required.

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
git clone https://github.com/your-org/flipsync.git
cd flipsync

cp .env.example .env
# Add your HuggingFace token to .env — see docs/huggingface-setup.md

docker compose up --build
```

Open [http://localhost:3000](http://localhost:3000).

On first run Docker downloads the Demucs, pyannote, and Whisper models (~5 GB total). This happens once; subsequent starts are fast.

---

## Documentation

Full specification in [`/spec`](spec/README.md). Start with [`spec/overview.md`](spec/overview.md).

---

## Status

**Building in the open.** The spec is complete. Implementation is in progress. Not yet production-ready.

If you're reading this early: the spec is the most useful thing here right now. Issues and spec feedback welcome.

---

## License

Apache 2.0. See [LICENSE](LICENSE).

FlipSync assumes you have rights to your source material.
