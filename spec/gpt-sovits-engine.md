# GPT-SoVITS — Second Voice Engine

**Status:** Design (approved approach, pending spec review)
**Date:** 2026-07-14
**Depends on:** existing XTTS-v2 voice stack (`services/xtts`, `models` table, Train stage)

## 1. Goal & scope

Add GPT-SoVITS (v2Pro line) as a **second, selectable fine-tune engine** alongside
XTTS-v2. A GPT-SoVITS model reaches **full feature parity** with an XTTS model in
the UI: train, preview, segment A/B compare, bundle download, delete. The engine
underneath differs; the workflow does not.

**Why GPT-SoVITS:** few-shot cloning that trains well on ~1–5 min of *average-quality*
audio — exactly what FlipSync extracts from video. The v1/v2/v2Pro line explicitly
tolerates non-pristine training sets (v3/v4 do not), and it is MIT-licensed
throughout, so it carries **no CPML-style acceptance gate**.

### In scope
- New profile-gated `gpt-sovits` processing service (port 8006), built to the
  `services/xtts` template.
- `engine` column on `models`; per-engine job routing in the orchestrator.
- Capabilities endpoint reports available engines + per-engine metadata.
- Frontend engine picker on the Train stage (shown only when >1 engine is healthy).
- Full preview / compare / download / delete parity for GPT-SoVITS models.

### Out of scope (v1)
- Languages beyond English. GPT-SoVITS supports en/zh/ja/ko/yue; FlipSync material
  is English-only, so the picker does **not** gate on language in v1. Capabilities
  still reports each engine's language list for future use.
- v3/v4 GPT-SoVITS lines (worse on average-quality audio; deferred).
- Migrating existing XTTS models — `engine` defaults to `'xtts'` for all existing rows.
- Changing `dataset_build`. The dataset manifest (`dataset.json`) is engine-agnostic
  and unchanged; each engine adapts it in its own service.

## 2. Architecture

Approach A: **separate service, own container.** GPT-SoVITS and XTTS never share a
Python environment (their torch / transformers pins conflict, and the XTTS image
took a long compat chain to stabilise — it stays untouched).

```
orchestrator (8000)
 ├── xtts (8005)          profile: xtts          [existing, unchanged]
 └── gpt-sovits (8006)    profile: gpt-sovits     [new]
```

Invariants preserved: services don't call each other or touch the DB; files on the
shared `/data` volume are the interface; GPU jobs serialise through the orchestrator's
global GPU semaphore; every service exposes `GET /health`.

### Service internals (`services/gpt-sovits/`)

Mirrors the xtts service structure:

```
gpt-sovits/
├── main.py              FastAPI app (port 8006): /health, POST /jobs, GET /jobs/{id}
├── engine.py            train + synthesise boundary; all ML imports lazy
├── dataset.py           manifest → GPT-SoVITS .list adapter (pure stdlib)
├── requirements.txt
├── Dockerfile           vendors the GPT-SoVITS repo at a pinned commit
├── scripts/gpu_smoke.py real end-to-end GPU check
└── tests/
```

**Vendoring:** the GPT-SoVITS repo is cloned into the image at a **pinned commit**
(recorded in the Dockerfile). It is not a pip package. `engine.py`:
- **Training** drives the vendored prep + train stages as **subprocesses**, parsing
  progress from stdout. The stages are CLI/config-shaped and not import-friendly.
- **Synthesis** imports the vendored inference API (`TTS_infer_pack`) **in-process**,
  so it gets model caching and idle-VRAM unload like the other GPU services.

Test seam identical to xtts: `engine.py` keeps all torch / GPT-SoVITS imports inside
functions so `main.py` and the API tests import cleanly without the ML stack. API-layer
tests patch `engine.train` / `engine.synthesise`; only `gpu_smoke.py` runs the real thing.

## 3. Training pipeline

GPT-SoVITS fine-tune is a multi-stage pipeline over the dataset `.list` file
(`wav_path|speaker|language|text`, one per line — produced by this service's
`dataset.py` from the manifest):

| Stage | What | Progress phase |
|-------|------|----------------|
| Prep 1 | text/phoneme + BERT feature extraction | `preparing` |
| Prep 2 | HuBERT (SSL) feature extraction | `preparing` |
| Prep 3 | semantic-token extraction (via base s2) | `preparing` |
| Train s2 | fine-tune SoVITS (VITS) → `SoVITS_*.pth` | `training_sovits` |
| Train s1 | fine-tune GPT (AR) → `GPT_*.ckpt` | `training_gpt` |
| Package | assemble bundle in `output_dir` | `packaging` |

Two independent training runs (s2 then s1), each with its own epoch counter. Progress
is parsed from stdout and mapped onto the **same progress dict** the orchestrator
already polls: `{phase, epoch, total_epochs, step, total_steps, train_loss, eta_secs}`.
`total_epochs` is reported **per sub-stage** and the `phase` field distinguishes them,
so the existing `on_progress` percent math in `_handle_finetune` needs the phase-aware
tweak in §6.

**Pretrained base weights** (chinese-hubert-base, chinese-roberta BERT, base s1/s2
v2Pro checkpoints, and the v2Pro speaker-verification model) live under
`GPT_SOVITS_PRETRAINED_DIR`, bind-mounted at `${MODELS_ROOT}/gpt-sovits`. Downloaded
from the public `lj1995/GPT-SoVITS` HF repo on first run — **no HF token required**.

**VRAM:** `FINETUNE_MIN_VRAM_GB` preflight in the service (GPT-SoVITS trains lighter
than XTTS; value pinned during implementation against a real run, provisionally 8.0).
Fails fast with a clear message rather than OOM-ing mid-run.

## 4. Model bundle & conditioning

XTTS ships one `model.pth` + `config.json` + `vocab.json` (+ optional
`speaker_latents.pt`). GPT-SoVITS is different and the bundle layout reflects it:

```
models/{id}/
├── gpt.ckpt            GPT (s1) weights          [mandatory]
├── sovits.pth          SoVITS (s2) weights        [mandatory]
├── config.json         engine + version + SR + relative paths   [mandatory]
├── reference.wav       conditioning reference clip (3–10 s)      [mandatory]
└── reference.txt       transcript of reference.wav               [mandatory]
```

**Conditioning reference.** GPT-SoVITS synthesis *requires* a reference clip **and its
transcript** at inference time (XTTS does not). At packaging time the service selects a
representative training segment (good duration ~3–10 s, non-empty transcript) and writes
`reference.wav` + `reference.txt` into the bundle — the GPT-SoVITS analogue of XTTS's
`speaker_latents.pt`. Preview/compare use the stored reference by default; the target
text varies per request. This keeps the orchestrator→service synthesis contract
engine-neutral (see §5).

`BUNDLE_MANDATORY` / `BUNDLE_OPTIONAL` in `routers/models.py` become **per-engine**
(dict keyed by engine) so download streams the right files.

## 5. Orchestrator changes

### 5.1 Data model — migration `014_model_engine.sql`
```sql
ALTER TABLE models ADD COLUMN engine TEXT NOT NULL DEFAULT 'xtts';
-- Values: 'xtts' | 'gpt_sovits'. Existing rows default to 'xtts'.
```
No other schema change. `params` (already JSON) carries engine-specific hyperparameters.

### 5.2 Service client
Add `gpt_sovits` → `GPT_SOVITS_URL` (default `http://gpt-sovits:8006`) to the service map.

### 5.3 Job routing
`dataset_build` is unchanged (engine-agnostic manifest). `finetune` and `preview`
route by the model's `engine`:

- **`_handle_finetune`**: read `models.engine`; pick target service (`xtts` |
  `gpt_sovits`) and the param set. XTTS resolves omitted params against `projects.xtts_*`
  columns as today. GPT-SoVITS resolves omitted params against **service-supplied
  defaults** (no new project columns in v1) — the request's Train-panel params are
  forwarded, the service fills the rest. `on_progress` becomes phase-aware (§6).
- **`_handle_preview`**: route to the model's engine service. For a fine-tuned
  GPT-SoVITS model, the service loads the bundle (incl. stored `reference.wav`/`.txt`).
  Base-model (untrained) GPT-SoVITS preview is **not offered** in v1 — GPT-SoVITS has
  no meaningful zero-shot-without-reference path in our flow; preview requires a trained
  model. (XTTS base preview is unaffected.)

### 5.4 Create-model request
`POST /projects/{id}/models` gains `engine: Literal["xtts","gpt_sovits"] = "xtts"`.
Validation: reject `gpt_sovits` with 503 `engine_unavailable` if that service isn't
healthy (mirrors the existing `xtts_unavailable` check). `params` stays a permissive
optional bag; unknown keys for the chosen engine are ignored by that engine.
`engine` is persisted on the row and echoed in listings (`_serialize_model`).

### 5.5 Capabilities
`GET /capabilities` grows an `engines` array so the frontend can build the picker
without probing each service:
```json
"xtts": true,
"voice_training": true,
"engines": [
  {"id": "xtts",       "name": "XTTS-v2",     "healthy": true,  "languages": ["en", "..."]},
  {"id": "gpt_sovits", "name": "GPT-SoVITS",  "healthy": true,  "languages": ["en","zh","ja","ko","yue"]}
]
```
The existing top-level `"xtts"` boolean is **retained for backward-compat** but is no
longer what drives the terminal-stage Train-vs-Export decision. A new derived
`"voice_training": <bool>` = *any* engine healthy replaces it for that decision, and the
frontend switches to reading `voice_training`. This is what makes a GPT-SoVITS-only
deployment (xtts absent, gpt-sovits healthy) correctly offer the Train stage.

## 6. Progress percent (phase-aware)

Today `_handle_finetune.on_progress` computes a single 0–99% from
`(epoch-1 + step/total_steps) / total_epochs`. GPT-SoVITS has two training sub-stages.
The percent function is generalised to weight sub-stages by phase:

- `preparing` → 0–5%
- `training_sovits` → 5–50% (scaled by its epoch/step)
- `training_gpt` → 50–95% (scaled by its epoch/step)
- `packaging` → 95–99%

XTTS reports only `training`/`preparing`/`packaging`; its mapping is unchanged. The
`progress_detail` dict is stored verbatim as now, so the frontend can show phase labels.

## 7. Frontend changes

- **Train stage:** an **engine selector** (segmented control or dropdown) sourced from
  `capabilities.engines`, shown **only when >1 engine is healthy**; otherwise the single
  engine is implicit (no new UI for XTTS-only deployments). Selected engine posts on the
  create-model request.
- **Advanced params:** per-engine param panels. XTTS keeps its current knobs; GPT-SoVITS
  shows its own (SoVITS epochs, GPT epochs, batch size). Panels are driven by engine id,
  not a shared schema — kept deliberately simple in v1 (no dynamic schema from the server).
- **Model cards:** show an **engine badge** (`XTTS-v2` / `GPT-SoVITS`).
- **Preview / Compare panels:** unchanged in shape — they call the orchestrator, which
  routes by engine. The only behavioural difference: a GPT-SoVITS model has no base/
  untrained preview (§5.3); the panel reflects that it needs a ready model.

## 8. Deployment

- `docker-compose.yml`: new `gpt-sovits` service, `profiles: ["gpt-sovits"]`, image
  `ghcr.io/jjsmackay/flipsync/gpt-sovits:latest`, `GPU` reservation, shared `/data`
  volume, `GPT_SOVITS_URL` wired into the orchestrator env.
- Weight cache bind mount: `${MODELS_ROOT:-/mnt/models/flipsync}/gpt-sovits` →
  `GPT_SOVITS_PRETRAINED_DIR`. Survives `compose down`. **Wire it from day one** (unlike
  the RoFormer/MMS caches that were documented-but-not-wired and re-download on recreate).
- `IDLE_UNLOAD_SECS` honoured in the synthesis path, consistent with the other upstream
  GPU services.
- No `HF_TOKEN` and no CPML-style acceptance env — the models are MIT and public.

## 9. Error handling

Flat error format everywhere (`{"error","message","detail"}`), `AppError` in the
orchestrator. New error codes:
- `engine_unavailable` (503) — requested engine's service not healthy at create time.
- Service-side training/synth failures surface as `finetune`/`preview` job failures and
  fail the linked model row (existing `_fail_model` path; the training-queued-wedge fix
  already covers redeploy-mid-train).

## 10. Testing

Mirror the xtts suites:
- **Service unit:** `dataset.py` manifest→`.list` conversion; stdout→progress-dict
  parser (pure, no GPU); reference-segment selection; VRAM preflight helper.
- **Service API:** submit `finetune`/`synthesise`, poll to completion, verify output —
  against a patched `engine` (no GPU in CI).
- **Orchestrator:** `engine` routing in `_handle_finetune`/`_handle_preview`
  (asserts the right service URL is called per engine); create-model validation
  (`engine_unavailable`); phase-aware percent math; per-engine bundle download.
- **GPU smoke** (`scripts/gpu_smoke.py`, not CI): real train → ready → preview WAV on
  a committed short fixture.
- **Frontend:** engine picker visibility (0/1/2 healthy engines), engine badge, create
  request carries the selected engine.

## 11. Open items to pin during implementation
- Exact vendored commit + the precise prep/train entrypoint names and CLI/config shape
  at that commit (the stage table in §3 is by role; names are pinned against the commit).
- `FINETUNE_MIN_VRAM_GB` value from a real run (provisional 8.0).
- Output sample rate read at runtime off the loaded model (v2Pro; do not hardcode).
- Whether v2Pro's speaker-verification model is required for inference or training-only
  (affects which pretrained files are mandatory in the cache).
