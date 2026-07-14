# Handoff — GPT-SoVITS second voice engine

Paste this to start an implementation session.

---

Implement the GPT-SoVITS voice engine for FlipSync. **Full design is
`spec/gpt-sovits-engine.md` — read it first; it's the source of truth.** Work on
branch `feature/gpt-sovits-engine` (already exists, spec committed).

**What:** a second, selectable fine-tune engine alongside XTTS-v2, with full UI
parity (train, preview, compare, download, delete). English-only in v1. MIT-licensed,
so no CPML-style acceptance gate.

**Architecture (Approach A):** a new profile-gated `services/gpt-sovits/` container
(port 8006), built to the `services/xtts/` template. GPT-SoVITS repo vendored at a
pinned commit; training subprocess-drives the prep→s2→s1 stages (parse progress from
stdout), synthesis imports the inference API in-process. XTTS stays untouched.

**Don't break invariants:** services never call each other or touch the DB; files on
`/data` are the interface; GPU jobs serialise through the orchestrator's semaphore;
`GET /health` on every service; flat error format + `AppError`. All ML imports in
`engine.py` stay lazy (test seam) — API tests patch `engine`, only `gpu_smoke.py`
hits the GPU.

**Build order (suggested):**
1. Migration `014_model_engine.sql` (`engine` column, default `'xtts'`) + `engine`
   routing in `_handle_finetune`/`_handle_preview` + create-model `engine` field +
   `/capabilities` `engines[]` and derived `voice_training` flag. (Orchestrator, testable now.)
2. `services/gpt-sovits/`: `dataset.py` (manifest→`.list`), stdout→progress parser,
   `main.py` + `engine.py` skeleton with the test seam, API tests against patched engine.
3. Real training/synthesis in `engine.py` + Dockerfile (vendored commit, pretrained
   cache at `GPT_SOVITS_PRETRAINED_DIR`) + compose service + `gpu_smoke.py`.
4. Frontend: engine picker (only when >1 engine healthy), per-engine Advanced params,
   engine badge on model cards.

**Resolve against the real repo (spec §11), don't guess:** vendored commit + exact
prep/train entrypoint names; `FINETUNE_MIN_VRAM_GB` from a real run; output SR read
at runtime; whether v2Pro's speaker-verification model is inference-required.

**Per-engine specifics:** GPT-SoVITS bundle = `gpt.ckpt` + `sovits.pth` + `config.json`
+ `reference.wav` + `reference.txt` (it needs a reference clip *and* transcript at
inference — select a training segment at packaging time). No base/untrained preview.
`BUNDLE_MANDATORY` becomes per-engine.

Tests must pass before commit; merge straight to `main` when done (solo-owner workflow).
