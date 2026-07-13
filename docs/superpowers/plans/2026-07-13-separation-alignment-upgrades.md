# Separation & Alignment Upgrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `htdemucs_ft` the default separation model, add a RoFormer separation backend behind the existing `model` param, and add an optional wav2vec2 forced-alignment pass to sharpen transcription word timestamps.

**Architecture:** Three independent slices. (A) is a pure config/default change — no schema, no new deps. (B) and (C) each add a heavy new dependency to an existing service image, so each **opens with a dependency-solve spike that is a hard go/no-go gate**: if the dep can't coexist cleanly in the existing image, the fallback is a dedicated second image behind the same HTTP contract, and the rest of that slice does not proceed until the gate is resolved. All three preserve FlipSync's invariants: services don't call each other, the orchestrator owns all DB writes, the `model`/`align` switches ride the existing `POST /jobs` contracts, and config changes do not retro-apply (reprocess to adopt).

**Tech Stack:** Python, FastAPI, faster-whisper/ctranslate2 (transcription), Demucs 4.0.1 + `audio-separator` (separation), whisperx (alignment), SQLite (orchestrator config), React/TS (settings UI).

## Global Constraints

- **Australian English** in all copy/comments; **YYYY-MM-DD** dates in technical docs.
- **The spec is the source of truth.** Every behavioural change updates `spec/pipeline.md`, `spec/api-contracts.md`, and/or `spec/data-models.md` in the same slice.
- **Services never call each other; the browser never calls a processing service.** All coordination and all DB writes go through the orchestrator (invariants #1, #2, #4).
- **Processing services never touch the DB.** Inputs via HTTP request body, outputs via HTTP response body.
- **Error format everywhere:** `{"error": "snake_case", "message": "Human-readable.", "detail": {}}`. Orchestrator uses `AppError` (never `HTTPException`).
- **Vocal-separation image is pinned `torch<2.6`** because Demucs 4.0.1 unpickles checkpoints via `torch.load` (weights_only default flipped in 2.6). Any new dep in that image MUST resolve against this pin.
- **Transcription image** (`nvidia/cuda:12.9.2-cudnn-runtime-ubuntu24.04`, Python 3.12) currently has **no torch/torchaudio** — only `faster-whisper==1.2.1` + `ctranslate2==4.8.1`. Alignment introduces a torch stack; it must not disturb the ctranslate2 CUDA linkage.
- **Config changes do not retro-apply.** New project defaults come from `ProjectCreate` pydantic defaults; existing projects keep their stored value until the user changes it and reprocesses. Do not attempt to mutate existing rows in a migration.
- **GPU jobs are serialised host-wide** by the orchestrator's GPU semaphore; new resident models (RoFormer, wav2vec2 aligner) MUST participate in each service's idle-VRAM unload lifecycle (`is_model_loaded`/`unload_models`/`preload_models`).
- **Tests must pass before every commit.** Run commands are given per task.

---

## File Structure

**Slice A — htdemucs_ft default (no new files):**
- Modify: `services/vocal-separation/separator.py` (VALID_MODELS, default), `services/vocal-separation/main.py` (JobRequest default, `PRELOAD_MODELS` default)
- Modify: `services/orchestrator/routers/projects.py` (`DEMUCS_MODELS`, `ProjectCreate.demucs_model` default)
- Modify: `spec/pipeline.md` §Step 1
- Test: existing service + orchestrator test suites

**Slice B — RoFormer backend:**
- Modify: `services/vocal-separation/separator.py` (model-family branch, RoFormer path, lifecycle)
- Modify: `services/vocal-separation/requirements.txt`, `services/vocal-separation/Dockerfile`
- Modify: `services/orchestrator/routers/projects.py` (`DEMUCS_MODELS` add `bs_roformer`)
- Create: `services/vocal-separation/tests/test_roformer.py`
- Modify: `spec/pipeline.md` §Step 1, `spec/api-contracts.md` §Vocal Separation Service
- Spike output: `docs/superpowers/plans/notes/2026-07-13-roformer-dep-solve.md`

**Slice C — optional alignment pass:**
- Create: `services/transcription/aligner.py`, `services/transcription/tests/test_aligner.py`
- Modify: `services/transcription/transcriber.py` (thread `align` through), `services/transcription/main.py` (`JobRequest.align`), `services/transcription/requirements.txt`, `services/transcription/Dockerfile`
- Create: `services/orchestrator/migrations/012_align_words.sql`
- Modify: `services/orchestrator/routers/projects.py` (config field), `services/orchestrator/jobs.py` (payload)
- Modify: `spec/pipeline.md` §Step 3, `spec/api-contracts.md` §Transcription Service, `spec/data-models.md` (project config)
- Modify: `frontend/` settings panel (model dropdown + align toggle) — see Task C7
- Spike output: `docs/superpowers/plans/notes/2026-07-13-alignment-dep-solve.md`

---

# Slice A — Default to htdemucs_ft

`htdemucs_ft` is the per-stem fine-tuned Demucs v4 bag-of-4 — modestly cleaner vocals for a one-value change, at ~4× runtime (acceptable for an offline first stage). No schema change: `demucs_model` already exists (migration 011); we add an allowed value and flip two defaults. Existing projects are untouched by design.

### Task A1: Accept and default to `htdemucs_ft` in the vocal-separation service

**Files:**
- Modify: `services/vocal-separation/separator.py:24` (VALID_MODELS), `:79` (`separate` default)
- Modify: `services/vocal-separation/main.py:108` (`PRELOAD_MODELS` default), `:159` (`JobRequest.model` default)
- Test: `services/vocal-separation/tests/` (new test in existing suite)

**Interfaces:**
- Produces: `separator.VALID_MODELS` now includes `"htdemucs_ft"`; service default model is `"htdemucs_ft"`.

- [ ] **Step 1: Write the failing test**

Add to `services/vocal-separation/tests/test_separator.py` (create the file if the suite splits differently — match the existing test module that imports `separator`):

```python
import separator as sep


def test_htdemucs_ft_is_a_valid_model():
    assert "htdemucs_ft" in sep.VALID_MODELS


def test_separate_defaults_to_htdemucs_ft():
    import inspect
    assert inspect.signature(sep.separate).parameters["model_name"].default == "htdemucs_ft"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/test_separator.py -k htdemucs_ft -v`
Expected: FAIL — `"htdemucs_ft"` not in the set; default is `"htdemucs"`.

- [ ] **Step 3: Make the change**

In `separator.py`:
```python
VALID_MODELS = {"htdemucs", "htdemucs_ft", "mdx_extra"}
```
and change the `separate` signature default:
```python
def separate(
    input_path: str,
    output_path: str,
    model_name: str = "htdemucs_ft",
    chunk_secs: Optional[int] = None,
    shifts: int = 0,
    progress_callback=None,
) -> None:  # noqa: E501
```

In `main.py`:
```python
preload = os.environ.get("PRELOAD_MODELS", "htdemucs_ft").split(",")
```
and the `JobRequest` default:
```python
    model: str = "htdemucs_ft"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/ -v`
Expected: PASS (whole suite green — no other test asserts the old default).

- [ ] **Step 5: Commit**

```bash
git add services/vocal-separation/separator.py services/vocal-separation/main.py services/vocal-separation/tests/
git commit -m "feat(vocal-separation): accept and default to htdemucs_ft"
```

### Task A2: Accept and default to `htdemucs_ft` in orchestrator project config

**Files:**
- Modify: `services/orchestrator/routers/projects.py:160` (`DEMUCS_MODELS`), `:199` (`ProjectCreate.demucs_model` default)
- Test: `services/orchestrator/tests/test_projects.py`

**Interfaces:**
- Consumes: `separator.VALID_MODELS` (must stay a superset of `DEMUCS_MODELS`).
- Produces: new projects are created with `config.demucs_model == "htdemucs_ft"`; PATCH accepts `"htdemucs_ft"`.

- [ ] **Step 1: Write the failing test**

Add to `services/orchestrator/tests/test_projects.py`:
```python
def test_new_project_defaults_to_htdemucs_ft(client):
    r = client.post("/projects", json={"name": "ft-default"})
    pid = r.json()["id"]
    detail = client.get(f"/projects/{pid}").json()
    assert detail["config"]["demucs_model"] == "htdemucs_ft"


def test_patch_accepts_htdemucs_ft(client):
    pid = client.post("/projects", json={"name": "ft-patch"}).json()["id"]
    r = client.patch(f"/projects/{pid}", json={"demucs_model": "htdemucs_ft"})
    assert r.status_code == 200
    assert client.get(f"/projects/{pid}").json()["config"]["demucs_model"] == "htdemucs_ft"
```
(Use the existing `client` fixture in that module; match its signature.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/test_projects.py -k htdemucs_ft -v`
Expected: FAIL — default is `"htdemucs"`; PATCH rejects the unknown value with 422.

- [ ] **Step 3: Make the change**

In `routers/projects.py`:
```python
DEMUCS_MODELS = {"htdemucs", "htdemucs_ft", "mdx_extra"}
```
and the `ProjectCreate` default:
```python
    demucs_model: str = "htdemucs_ft"
```
(Leave `ProjectPatch.demucs_model: Optional[str] = None` and both `_validate_demucs_model` bindings as-is — they pick up the new allowed value automatically.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Update the spec**

In `spec/pipeline.md` §Step 1, change the default-model line (currently line ~53) to:
```
Default model: `htdemucs_ft` (the per-stem fine-tuned Demucs v4 bag — cleaner vocals than plain `htdemucs` at ~4× runtime, acceptable for an offline first stage). Fallbacks `htdemucs` and `mdx_extra` remain selectable; the orchestrator requests a specific variant per project via `demucs_model`. Changing the model does not retro-apply — reprocess Step 1 to adopt it.
```

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/routers/projects.py services/orchestrator/tests/test_projects.py spec/pipeline.md
git commit -m "feat(orchestrator): default demucs_model to htdemucs_ft"
```

---

# Slice B — RoFormer separation backend

RoFormer (mel-band / BS-RoFormer) is current SOTA for vocal isolation, accessed via the FOSS `audio-separator` package (runs the model as a PyTorch checkpoint; downloads weights to its own cache). It rides the existing `model` param — **no API-contract change**. The one real risk is the dependency solve inside the `torch<2.6`-pinned image, which is why Task B0 gates everything.

### Task B0 — CHECKPOINT (spike, not TDD): resolve the dependency solve

**This task is a hard gate. Tasks B1–B5 do not begin until this produces a GO decision and a pinned dependency set.**

**Files:**
- Create: `docs/superpowers/plans/notes/2026-07-13-roformer-dep-solve.md` (findings + decision)

- [ ] **Step 1: Attempt a joint install in a throwaway env**

```bash
cd services/vocal-separation
uv run --with "torch<2.6" --with "torchaudio<2.6" --with "demucs==4.0.1" \
       --with "audio-separator" --with "onnxruntime-gpu" \
       python -c "import torch, demucs, audio_separator.separator as s; print('torch', torch.__version__); print('audio-separator OK')"
```
Record the resolved `torch`, `audio-separator`, and `onnxruntime-gpu` versions, and any resolver conflict.

- [ ] **Step 2: Verify Demucs checkpoint loading still works under the resolved torch**

```bash
uv run --with "torch<2.6" --with "torchaudio<2.6" --with "demucs==4.0.1" --with "audio-separator" --with "onnxruntime-gpu" \
       python -c "from demucs.pretrained import get_model; get_model('htdemucs'); print('demucs load OK under resolved torch')"
```
Expected: no `weights_only`/unpickling error. If it breaks, the shared env is NO-GO.

- [ ] **Step 3: Confirm the RoFormer model runs and emits a vocals file**

Using the ~5–10s committed test fixture WAV, load a RoFormer checkpoint via `audio_separator.separator.Separator(model_file_dir=...)`, call `.load_model(model_filename="model_bs_roformer_ep_317_sdr_12.9755.ckpt")` then `.separate(fixture)`, and assert one output file contains `(Vocals)`. (Requires network for first weight download; note the cache dir.)

- [ ] **Step 4: Record the decision**

Write the notes file with an explicit verdict:
- **GO (same image):** a single `torch` version satisfies Demucs 4.0.1 **and** audio-separator, Demucs checkpoints still load, RoFormer runs. Record the exact pins to add in Task B1. **Proceed to B1.**
- **NO-GO (second image):** conflict is unresolvable. **Stop this slice** and open a follow-up plan for a dedicated `vocal-separation-roformer` image exposing the same `GET /health` + `POST /jobs` + `GET /jobs/{id}` contract, with the orchestrator routing by model family. Do not force the shared env.

- [ ] **Step 5: Commit the spike notes**

```bash
git add docs/superpowers/plans/notes/2026-07-13-roformer-dep-solve.md
git commit -m "docs(vocal-separation): RoFormer dependency-solve spike + decision"
```

### Task B1: Add RoFormer deps and the model-family branch (GO path only)

**Files:**
- Modify: `services/vocal-separation/requirements.txt`, `services/vocal-separation/Dockerfile`
- Modify: `services/vocal-separation/separator.py` (VALID_MODELS, `_ROFORMER_CKPT`, `_load_model` branch, `separate` branch, `_separate_roformer`)
- Test: `services/vocal-separation/tests/test_roformer.py`

**Interfaces:**
- Consumes: the pinned versions from Task B0.
- Produces:
  - `separator.VALID_MODELS` includes `"bs_roformer"`.
  - `separator._ROFORMER_CKPT: dict[str, str]` maps friendly name → checkpoint filename.
  - `separator._is_roformer(model_name: str) -> bool`.
  - `separator._separate_roformer(input_path: str, output_path: str, model_name: str) -> None` — writes the vocals stem to `output_path`.

- [ ] **Step 1: Write the failing test (family routing + name registry, no GPU)**

Create `services/vocal-separation/tests/test_roformer.py`:
```python
import separator as sep


def test_bs_roformer_is_valid():
    assert "bs_roformer" in sep.VALID_MODELS


def test_family_detection():
    assert sep._is_roformer("bs_roformer") is True
    assert sep._is_roformer("htdemucs") is False
    assert sep._is_roformer("htdemucs_ft") is False


def test_roformer_has_a_checkpoint_mapping():
    assert sep._ROFORMER_CKPT["bs_roformer"].endswith(".ckpt")


def test_separate_routes_roformer_to_roformer_impl(monkeypatch, tmp_path):
    called = {}

    def fake_roformer(input_path, output_path, model_name):
        called["args"] = (input_path, output_path, model_name)

    monkeypatch.setattr(sep, "_separate_roformer", fake_roformer)
    out = str(tmp_path / "vocals.wav")
    sep.separate("in.wav", out, model_name="bs_roformer")
    assert called["args"] == ("in.wav", out, "bs_roformer")


def test_separate_still_uses_demucs_for_htdemucs(monkeypatch, tmp_path):
    # Demucs path must NOT be routed to the roformer impl.
    monkeypatch.setattr(sep, "_separate_roformer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrong path")))
    monkeypatch.setattr(sep, "_load_model", lambda name: (_ for _ in ()).throw(RuntimeError("stop-after-route")))
    import pytest
    with pytest.raises(RuntimeError, match="stop-after-route"):
        sep.separate("in.wav", str(tmp_path / "v.wav"), model_name="htdemucs")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/test_roformer.py -v`
Expected: FAIL — `bs_roformer` unknown; `_is_roformer`/`_ROFORMER_CKPT`/`_separate_roformer` undefined.

- [ ] **Step 3: Implement the branch in `separator.py`**

Extend the registry and add the RoFormer path. `output_dir`/`model_file_dir` come from env so weights persist under the `MODELS_ROOT` bind mount:
```python
import shutil

VALID_MODELS = {"htdemucs", "htdemucs_ft", "mdx_extra", "bs_roformer"}

# Friendly name -> audio-separator checkpoint filename.
_ROFORMER_CKPT = {
    "bs_roformer": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
}

# Where audio-separator caches downloaded model weights. Point at the shared
# model-storage bind mount so weights survive `compose down`.
_ROFORMER_MODEL_DIR = os.environ.get(
    "ROFORMER_MODEL_DIR", "/mnt/models/flipsync/audio-separator"
)


def _is_roformer(model_name: str) -> bool:
    return model_name in _ROFORMER_CKPT


def _separate_roformer(input_path: str, output_path: str, model_name: str) -> None:
    """Separate vocals with a RoFormer checkpoint via audio-separator.

    audio-separator writes stem files into its output dir and returns their
    paths; we move the vocals stem to `output_path` to match the Demucs
    contract (a single vocals WAV at the orchestrator-specified path).
    """
    from audio_separator.separator import Separator  # lazy: heavy import

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    os.makedirs(_ROFORMER_MODEL_DIR, exist_ok=True)

    sep_engine = _model_cache.get(model_name)
    if sep_engine is None:
        sep_engine = Separator(
            output_dir=os.path.dirname(output_path),
            model_file_dir=_ROFORMER_MODEL_DIR,
        )
        sep_engine.load_model(model_filename=_ROFORMER_CKPT[model_name])
        _model_cache[model_name] = sep_engine

    stem_paths = sep_engine.separate(input_path)
    vocals = next((p for p in stem_paths if "(Vocals)" in os.path.basename(p)), None)
    if vocals is None:
        raise RuntimeError("RoFormer produced no vocals stem")
    shutil.move(vocals, output_path)
    logger.info("Wrote RoFormer vocals to %s", output_path)
```
Add the family branch at the very top of `separate()` (before the Demucs-specific torchaudio load):
```python
def separate(input_path, output_path, model_name="htdemucs_ft",
             chunk_secs=None, shifts=0, progress_callback=None) -> None:
    if _is_roformer(model_name):
        if progress_callback:
            progress_callback(50)
        _separate_roformer(input_path, output_path, model_name)
        if progress_callback:
            progress_callback(100)
        return
    import torchaudio
    # ...existing Demucs body unchanged...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/ -v`
Expected: PASS (routing tests green; Demucs path untouched).

- [ ] **Step 5: Add deps and Dockerfile install**

Append to `requirements.txt` using the **exact pins recorded in Task B0** (illustrative — replace with the spike's resolved versions):
```
# RoFormer backend (see docs/superpowers/plans/notes/2026-07-13-roformer-dep-solve.md).
# Pins below MUST match the versions the dep-solve spike confirmed coexist with
# demucs 4.0.1 under torch<2.6.
audio-separator==<pinned>
onnxruntime-gpu==<pinned>
```
No Dockerfile command change is needed if it already runs `pip install -r requirements.txt` (it does). Add a one-line comment above `COPY requirements.txt .` noting RoFormer weights download on first use to `ROFORMER_MODEL_DIR`.

- [ ] **Step 6: Commit**

```bash
git add services/vocal-separation/separator.py services/vocal-separation/requirements.txt services/vocal-separation/Dockerfile services/vocal-separation/tests/test_roformer.py
git commit -m "feat(vocal-separation): add RoFormer backend behind model param"
```

### Task B2: Wire RoFormer into the idle-VRAM unload lifecycle

**Files:**
- Modify: `services/vocal-separation/separator.py` (`preload_models`), verify `is_model_loaded`/`unload_models`
- Test: `services/vocal-separation/tests/test_roformer.py`

**Interfaces:**
- Consumes: `_model_cache` (shared cache; RoFormer `Separator` instances live here under their model name).
- Produces: `preload_models(["bs_roformer"])` loads the RoFormer engine; `unload_models()` clears it; `is_model_loaded()` reflects it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_roformer.py`:
```python
def test_roformer_participates_in_unload(monkeypatch):
    sep._model_cache["bs_roformer"] = object()  # stand-in engine
    assert sep.is_model_loaded() is True
    sep.unload_models()
    assert sep.is_model_loaded() is False
    assert "bs_roformer" not in sep._model_cache


def test_preload_roformer_uses_load_path(monkeypatch):
    loaded = {}
    monkeypatch.setattr(sep, "_load_model", lambda name: loaded.setdefault("name", name))
    sep.preload_models(["bs_roformer"])
    assert loaded["name"] == "bs_roformer"
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/test_roformer.py -k unload -v`
Expected: `test_roformer_participates_in_unload` PASSES already (`unload_models` clears the whole cache). `test_preload_roformer_uses_load_path` FAILS because `_load_model` doesn't handle RoFormer names.

- [ ] **Step 3: Route `_load_model` for RoFormer**

At the top of `_load_model`, branch so preloading a RoFormer name builds its engine through the same cache instead of calling Demucs `get_model`:
```python
def _load_model(model_name: str):
    if _is_roformer(model_name):
        engine = _model_cache.get(model_name)
        if engine is None:
            from audio_separator.separator import Separator
            os.makedirs(_ROFORMER_MODEL_DIR, exist_ok=True)
            engine = Separator(model_file_dir=_ROFORMER_MODEL_DIR)
            engine.load_model(model_filename=_ROFORMER_CKPT[model_name])
            _model_cache[model_name] = engine
        return engine
    import torch
    # ...existing Demucs body unchanged...
```
Then simplify `_separate_roformer` to reuse it: replace its inline load block with `sep_engine = _load_model(model_name)` — but note `_separate_roformer` needs `output_dir` set on the engine per call; set it before separating:
```python
    sep_engine = _load_model(model_name)
    sep_engine.output_dir = os.path.dirname(output_path)
    stem_paths = sep_engine.separate(input_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/vocal-separation && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio --with anyio --with soundfile --with numpy python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/vocal-separation/separator.py services/vocal-separation/tests/test_roformer.py
git commit -m "feat(vocal-separation): RoFormer honours idle-VRAM unload + preload"
```

### Task B3: Accept `bs_roformer` in orchestrator project config

**Files:**
- Modify: `services/orchestrator/routers/projects.py:160` (`DEMUCS_MODELS`)
- Test: `services/orchestrator/tests/test_projects.py`

**Interfaces:**
- Consumes: `separator.VALID_MODELS` (superset invariant).
- Produces: PATCH/create accept `demucs_model == "bs_roformer"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_projects.py`:
```python
def test_patch_accepts_bs_roformer(client):
    pid = client.post("/projects", json={"name": "roformer"}).json()["id"]
    r = client.patch(f"/projects/{pid}", json={"demucs_model": "bs_roformer"})
    assert r.status_code == 200
    assert client.get(f"/projects/{pid}").json()["config"]["demucs_model"] == "bs_roformer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/test_projects.py -k bs_roformer -v`
Expected: FAIL with 422 (unknown value).

- [ ] **Step 3: Make the change**

```python
DEMUCS_MODELS = {"htdemucs", "htdemucs_ft", "mdx_extra", "bs_roformer"}
```
(The column stays named `demucs_model` for backward compatibility — it now means "separation model", RoFormer included. Do not rename the column.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/routers/projects.py services/orchestrator/tests/test_projects.py
git commit -m "feat(orchestrator): accept bs_roformer as a separation model"
```

### Task B4: Spec + document the RoFormer backend

**Files:**
- Modify: `spec/pipeline.md` §Step 1, `spec/api-contracts.md` §Vocal Separation Service

- [ ] **Step 1: Update `spec/pipeline.md` §Step 1**

Add after the default-model paragraph:
```
An alternative separation backend, `bs_roformer` (BS-RoFormer via the `audio-separator` package), is selectable through the same `demucs_model` config. It typically yields cleaner vocals than Demucs at higher VRAM cost. It runs in the same vocal-separation service, chosen per project; the OOM chunk-retry path (`retry_with_chunk_secs`) applies to the Demucs backends only — RoFormer manages its own internal segmentation.
```

- [ ] **Step 2: Update `spec/api-contracts.md` §Vocal Separation Service**

In the `POST /jobs` request-body doc, extend the `model` field's allowed values to `htdemucs | htdemucs_ft | mdx_extra | bs_roformer` and add a note that `bs_roformer` weights download on first use to `ROFORMER_MODEL_DIR` (defaults under `MODELS_ROOT`).

- [ ] **Step 3: Commit**

```bash
git add spec/pipeline.md spec/api-contracts.md
git commit -m "docs: spec the RoFormer separation backend"
```

---

# Slice C — Optional wav2vec2 alignment pass

An off-by-default per-project flag `align_words`. When on, the transcription service runs whisperx forced alignment to replace whisper's heuristic word start/end times with wav2vec2-aligned ones **before** sentence-aligned re-segmentation — sharpening child boundaries. It is a **no-op unless a segment is being re-segmented**, and it never changes the word text or whisper's per-word probability (so `transcript_confidence` and auto-approval are unaffected). Like RoFormer, it adds a heavy dependency (a torch stack) to a currently torch-free image, so Task C0 gates it.

### Task C0 — CHECKPOINT (spike, not TDD): resolve the alignment dependency solve

**Hard gate. C1–C7 do not begin until this yields a GO decision.**

**Files:**
- Create: `docs/superpowers/plans/notes/2026-07-13-alignment-dep-solve.md`

- [ ] **Step 1: Attempt a joint install alongside the pinned faster-whisper**

```bash
cd services/transcription
uv run --with "faster-whisper==1.2.1" --with "ctranslate2==4.8.1" --with "whisperx" \
       python -c "import faster_whisper, ctranslate2, whisperx; print('ok', ctranslate2.__version__)"
```
Record whether whisperx forces a different `ctranslate2`/`faster-whisper`/`torch` and whether the resolve succeeds. whisperx pulls torch, torchaudio, transformers, and pyannote — capture the full added footprint and image-size delta.

- [ ] **Step 2: Confirm ctranslate2 CUDA still loads**

Verify `import ctranslate2; ctranslate2.get_cuda_device_count()` still works under the resolved set (the transcription image links ctranslate2 against system CUDA/cuDNN — a torch wheel bundling its own CUDA must not shadow that).

- [ ] **Step 3: Confirm whisperx alignment runs on the fixture**

Load `whisperx.load_align_model(language_code="en", device=...)`, align a short known transcript against the committed fixture WAV, and confirm word-level `start`/`end` come back.

- [ ] **Step 4: Record the decision**

- **GO (same image):** whisperx coexists with `faster-whisper==1.2.1` / `ctranslate2==4.8.1`, ctranslate2 CUDA intact, alignment runs. Record exact pins for C6. **Proceed.**
- **NO-GO / heavy:** if whisperx drags an incompatible ctranslate2 or bloats the image unacceptably, **fall back** to torchaudio-native forced alignment (`torchaudio.pipelines.MMS_FA` / `torchaudio.functional.forced_align`) — same `aligner.py` interface, lighter deps (torch+torchaudio only, no pyannote/transformers). Record which backend C6 installs. Either way the `aligner.align_words` signature in C2/C3 is unchanged.

- [ ] **Step 5: Commit the spike notes**

```bash
git add docs/superpowers/plans/notes/2026-07-13-alignment-dep-solve.md
git commit -m "docs(transcription): alignment dependency-solve spike + decision"
```

### Task C1: Add `align_words` project config (migration + schema)

**Files:**
- Create: `services/orchestrator/migrations/012_align_words.sql`
- Modify: `services/orchestrator/routers/projects.py` (`_project_detail` config dict, `ProjectCreate`, `ProjectPatch`, create INSERT, patch loop)
- Test: `services/orchestrator/tests/test_projects.py`

**Interfaces:**
- Produces: project config key `align_words` (bool), default `false`; readable in `GET /projects/{id}`, settable via create/PATCH.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_projects.py`:
```python
def test_align_words_defaults_off_and_toggles(client):
    pid = client.post("/projects", json={"name": "align"}).json()["id"]
    assert client.get(f"/projects/{pid}").json()["config"]["align_words"] is False
    r = client.patch(f"/projects/{pid}", json={"align_words": True})
    assert r.status_code == 200
    assert client.get(f"/projects/{pid}").json()["config"]["align_words"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/test_projects.py -k align_words -v`
Expected: FAIL — `KeyError: 'align_words'` in the config dict.

- [ ] **Step 3: Add the migration**

Create `services/orchestrator/migrations/012_align_words.sql`:
```sql
-- Migration 012: optional wav2vec2 forced-alignment pass for transcription.
-- When enabled, the transcription service refines whisper word timestamps via
-- forced alignment before sentence-aligned re-segmentation. Off by default;
-- only affects segments being re-segmented. Changing it does not retro-apply —
-- re-transcribe to adopt.
ALTER TABLE projects ADD COLUMN align_words INTEGER NOT NULL DEFAULT 0;
```

- [ ] **Step 4: Wire the schema (four edits, matching the existing pattern)**

In `routers/projects.py`:
1. `_project_detail` config dict — add: `"align_words": bool(p["align_words"]),`
2. `ProjectCreate` — add field: `align_words: bool = False`
3. `ProjectPatch` — add field: `align_words: Optional[bool] = None`
4. create INSERT — add `align_words` to the column list and `?` to VALUES, and pass `int(body.align_words)` in the params tuple (place it consistently, e.g. right after `whisper_vad_filter`).
5. patch loop — add `"align_words"` to the migration-011-style tuple of plain knobs (the loop already does `int(val) if isinstance(val, bool)`), so the boolean stores as 0/1.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/migrations/012_align_words.sql services/orchestrator/routers/projects.py services/orchestrator/tests/test_projects.py
git commit -m "feat(orchestrator): add align_words project config (migration 012)"
```

### Task C2: Alignment merge logic (pure, TDD)

**Files:**
- Create: `services/transcription/aligner.py`, `services/transcription/tests/test_aligner.py`

**Interfaces:**
- Produces:
  - `aligner.Word` — dataclass with `word: str`, `start: float`, `end: float`, `probability: float` (mirrors the faster-whisper word attributes `transcriber.py`/`resegment.py` consume).
  - `aligner.merge_alignment(whisper_words, aligned) -> list[Word]` — `aligned` is `list[tuple[float, float]]`; overwrites start/end positionally, preserves word text and probability; on length mismatch returns the originals unchanged.

- [ ] **Step 1: Write the failing test**

Create `services/transcription/tests/test_aligner.py`:
```python
import aligner
from aligner import Word


class _W:
    def __init__(self, word, start, end, probability):
        self.word = word; self.start = start; self.end = end; self.probability = probability


def test_merge_overwrites_timestamps_keeps_text_and_probability():
    words = [_W(" Hello", 0.0, 0.4, 0.9), _W(" world.", 0.4, 0.9, 0.8)]
    merged = aligner.merge_alignment(words, [(0.05, 0.42), (0.42, 0.95)])
    assert [ (w.word, w.start, w.end, w.probability) for w in merged ] == [
        (" Hello", 0.05, 0.42, 0.9),
        (" world.", 0.42, 0.95, 0.8),
    ]


def test_merge_falls_back_on_count_mismatch():
    words = [_W(" Hi", 0.0, 0.3, 0.9), _W(" there.", 0.3, 0.7, 0.7)]
    merged = aligner.merge_alignment(words, [(0.0, 0.5)])  # wrong count
    assert [ (w.start, w.end) for w in merged ] == [(0.0, 0.3), (0.3, 0.7)]
    assert all(isinstance(w, Word) for w in merged)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/transcription && uv run --with pytest python -m pytest tests/test_aligner.py -v`
Expected: FAIL — `aligner` module does not exist.

- [ ] **Step 3: Implement `aligner.py` (merge logic + lazy real aligner)**

```python
"""Optional wav2vec2 forced-alignment pass to sharpen whisper word timestamps.

Enabled per project via `align_words`. Refines ONLY word start/end times; the
word token text and whisper's per-word probability (which drive confidence and
auto-approval) are preserved. Alignment affects sentence-aligned
re-segmentation boundaries only — it is a no-op for non-resegmented segments.
"""

from dataclasses import dataclass


@dataclass
class Word:
    word: str
    start: float
    end: float
    probability: float


def merge_alignment(whisper_words, aligned) -> list[Word]:
    """Positionally overwrite word start/end with aligned spans.

    On any length mismatch the alignment is untrustworthy, so the original
    timestamps are returned unchanged (never corrupt timestamps).
    """
    if len(aligned) != len(whisper_words):
        return [Word(w.word, w.start, w.end, w.probability) for w in whisper_words]
    return [
        Word(w.word, a_start, a_end, w.probability)
        for w, (a_start, a_end) in zip(whisper_words, aligned)
    ]


def align_words(wav_path: str, whisper_words, language):
    """Refine word timestamps via forced alignment. Lazy-imports the backend
    chosen in the dep-solve spike; falls back to the input words unchanged on
    any failure or token-count mismatch. `language` may be None (skip: no
    alignment model without a language)."""
    if not whisper_words or language is None:
        return merge_alignment(whisper_words, [])  # normalises to Word list unchanged
    try:
        import whisperx  # backend per Task C0 (or torchaudio fallback)
        from ctranslate2 import get_cuda_device_count
        device = "cuda" if get_cuda_device_count() > 0 else "cpu"
        model_a, meta = whisperx.load_align_model(language_code=language, device=device)
        transcript = "".join(w.word for w in whisper_words)
        seg = [{"start": whisper_words[0].start, "end": whisper_words[-1].end, "text": transcript}]
        result = whisperx.align(seg, model_a, meta, wav_path, device, return_char_alignments=False)
        aligned = [
            (w["start"], w["end"])
            for s in result["segments"] for w in s.get("words", [])
            if "start" in w and "end" in w
        ]
        return merge_alignment(whisper_words, aligned)
    except Exception:
        return merge_alignment(whisper_words, [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/transcription && uv run --with pytest python -m pytest tests/test_aligner.py -v`
Expected: PASS (both tests exercise `merge_alignment`; the lazy `whisperx` import is not hit).

- [ ] **Step 5: Commit**

```bash
git add services/transcription/aligner.py services/transcription/tests/test_aligner.py
git commit -m "feat(transcription): alignment merge logic + lazy aligner backend"
```

### Task C3: Thread `align` through the transcriber (TDD, injectable aligner)

**Files:**
- Modify: `services/transcription/transcriber.py` (`transcribe_segment`, `_transcribe_one`, `process_batch`)
- Test: `services/transcription/tests/test_transcriber.py` (or the existing transcriber test module)

**Interfaces:**
- Consumes: `aligner.align_words(wav_path, words, language) -> list[Word]`.
- Produces: `transcribe_segment(..., align: bool = False, aligner_fn=aligner.align_words)`; `align` flows through `_transcribe_one` and `process_batch`. Alignment runs **only** when `align and resegment and all_words`.

- [ ] **Step 1: Write the failing test**

Add to the transcriber test module (create `tests/test_transcriber_align.py` if cleaner):
```python
import transcriber
from aligner import Word


class _FakeModel:
    """Yields one whisper 'segment' with two words spanning a long clip."""
    def transcribe(self, wav_path, **kw):
        class _Seg:
            text = " one two."
            class _wtype:
                def __init__(s, word, start, end, p): s.word=word; s.start=start; s.end=end; s.probability=p
            words = [
                _wtype(" one", 0.0, 3.0, 0.9),
                _wtype(" two.", 3.5, 7.0, 0.9),
            ]
        return iter([_Seg()]), object()


def test_align_invoked_only_when_align_and_resegment(monkeypatch, tmp_path):
    wav = _write_silent_wav(tmp_path, seconds=8.0)  # helper in the existing suite
    calls = {"n": 0}

    def fake_align(wav_path, words, language):
        calls["n"] += 1
        # shift timestamps so we can detect the effect if we want
        return [Word(w.word, w.start, w.end, w.probability) for w in words]

    # resegment True + align True -> aligner called once
    transcriber.transcribe_segment(_FakeModel(), "seg1", wav, None,
                                   start_secs=0.0, resegment=True, align=True,
                                   aligner_fn=fake_align)
    assert calls["n"] == 1

    # align True but resegment False -> aligner NOT called
    calls["n"] = 0
    transcriber.transcribe_segment(_FakeModel(), "seg2", wav, None,
                                   start_secs=0.0, resegment=False, align=True,
                                   aligner_fn=fake_align)
    assert calls["n"] == 0
```
(Reuse the suite's existing silent-WAV helper; if none exists, write a 3-line `wave` writer in the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/transcription && uv run --with faster-whisper --with pytest python -m pytest tests/test_transcriber_align.py -v`
Expected: FAIL — `transcribe_segment` has no `align`/`aligner_fn` parameters.

- [ ] **Step 3: Implement threading in `transcriber.py`**

Add the import and thread the flag. In `transcribe_segment`, add params and invoke alignment inside the existing resegment guard so it only runs when splitting:
```python
import aligner

def transcribe_segment(model, segment_id, wav_path, language,
                       start_secs=0.0, resegment=False, beam_size=5,
                       vad_filter=False, align=False, aligner_fn=aligner.align_words):
    ...
    if resegment and all_words:
        if align:
            all_words = aligner_fn(wav_path, all_words, language)
        utterances = normalise_utterances(split_into_utterances(all_words))
        ...
```
Thread `align`/`aligner_fn` through `_transcribe_one` and `process_batch` (add the params, pass them down; default `align=False`). `_transcribe_one` reads `seg` dict for id/paths but takes `align` as a call arg like `beam_size`:
```python
def _transcribe_one(model, seg, language, beam_size=5, vad_filter=False, align=False):
    ...
    return transcribe_segment(model, seg["id"], seg["wav_path"], language,
                              start_secs=seg.get("start_secs") or 0.0,
                              resegment=seg.get("resegment", False),
                              beam_size=beam_size, vad_filter=vad_filter, align=align)
```
```python
def process_batch(model, batch, language, max_workers=1, beam_size=5,
                  vad_filter=False, align=False):
    if max_workers <= 1 or len(batch) <= 1:
        return [_transcribe_one(model, s, language, beam_size, vad_filter, align) for s in batch]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batch))) as pool:
        return list(pool.map(lambda s: _transcribe_one(model, s, language, beam_size, vad_filter, align), batch))
```
(Note: `merge_alignment` preserves `.probability`, and `compute_confidence` runs on the original/aligned words afterward — confidence is unchanged by alignment. The `aligner.Word` dataclass exposes `.word`/`.start`/`.end`/`.probability`, so `resegment.py` and `compute_confidence` consume it identically to faster-whisper words.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/transcription && uv run --with faster-whisper --with pytest python -m pytest tests/ -v`
Expected: PASS (whole suite; existing transcription tests unaffected since `align` defaults False).

- [ ] **Step 5: Commit**

```bash
git add services/transcription/transcriber.py services/transcription/tests/
git commit -m "feat(transcription): thread align flag into resegment path"
```

### Task C4: Accept and honour `align` in the transcription service API

**Files:**
- Modify: `services/transcription/main.py` (`JobRequest`, `_run_transcription`, `submit_job`, `process_batch` call)
- Test: `services/transcription/tests/` (API test module)

**Interfaces:**
- Consumes: `process_batch(..., align=...)`.
- Produces: `POST /jobs` accepts `align: bool = False`; it is passed to every `process_batch` call for the job.

- [ ] **Step 1: Write the failing test**

Add an API test asserting the field is accepted and defaults False (use the existing httpx/ASGI test client pattern in that module):
```python
def test_jobs_accepts_align_flag(client):
    body = {"job_id": "j-align", "segments": [{"id": "s1", "wav_path": "/nonexistent.wav"}],
            "model": "large-v2", "align": True}
    r = client.post("/jobs", json=body)
    assert r.status_code == 202
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/transcription && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio python -m pytest tests/ -k align -v`
Expected: FAIL — `align` is an unknown field (422) or ignored.

- [ ] **Step 3: Implement**

In `main.py`, add to `JobRequest`:
```python
    align: bool = False
```
Pass it into the background task and through to `process_batch`. Add `align` as a param on `_run_transcription`, store nothing extra in job state, and in the `process_batch` call add `align=align`. Wire it at the `asyncio.create_task(_run_transcription(...))` site (add `req.align`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/transcription && uv run --with fastapi --with uvicorn --with httpx --with pytest --with pytest-asyncio python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/transcription/main.py services/transcription/tests/
git commit -m "feat(transcription): accept align flag on POST /jobs"
```

### Task C5: Send `align` from the orchestrator transcription jobs

**Files:**
- Modify: `services/orchestrator/jobs.py` (`_handle_transcription_bulk` ~line 933/973, `_handle_transcription_segment` ~line 1040/1046)
- Test: `services/orchestrator/tests/test_wave3_pipeline.py` or `test_rf2_transcription.py`

**Interfaces:**
- Consumes: project column `align_words` (migration 012).
- Produces: both transcription payloads include `"align": bool(project["align_words"])`.

- [ ] **Step 1: Write the failing test**

Add a test that patches the transcription submit to capture the payload and asserts `align` is present and reflects config. Follow the existing mock-service pattern in `test_wave3_pipeline.py` (there is already a captured-payload style test for transcription batch/compute_type — mirror it):
```python
async def test_transcription_payload_includes_align(monkeypatch, ...):
    # set project align_words = 1 via PATCH, enqueue transcription_bulk,
    # capture the payload passed to _submit_with_retry, assert payload["align"] is True
    ...
```
(Match the module's existing async harness and capture hook precisely.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/test_wave3_pipeline.py -k align -v`
Expected: FAIL — payload has no `align` key.

- [ ] **Step 3: Implement**

In `_handle_transcription_bulk`, add `align_words` to the project SELECT column list and add to the payload:
```python
        "align": bool(project["align_words"]),
```
Do the same in `_handle_transcription_segment` (add `align_words` to its SELECT and `"align": bool(project["align_words"])` to its payload). Per-segment re-transcription benefits from alignment too, and it is safe: it only acts when `resegment` is true, which `transcription_segment` never sets — so alignment is effectively inert there but harmless and keeps the two payloads symmetric.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/orchestrator && uv run --with fastapi --with uvicorn --with python-multipart --with aiofiles --with httpx --with pytest --with pytest-asyncio --with anyio python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/jobs.py services/orchestrator/tests/
git commit -m "feat(orchestrator): send align flag to transcription jobs"
```

### Task C6: Install the alignment backend in the transcription image

**Files:**
- Modify: `services/transcription/requirements.txt`, `services/transcription/Dockerfile`

- [ ] **Step 1: Add the pinned deps from Task C0**

Append to `requirements.txt` (illustrative — use the spike's confirmed backend and pins; if C0 chose the torchaudio fallback, add `torch`/`torchaudio` instead of `whisperx`):
```
# Optional forced-alignment pass (align_words). Backend + pins per
# docs/superpowers/plans/notes/2026-07-13-alignment-dep-solve.md. MUST NOT
# perturb ctranslate2==4.8.1 CUDA linkage.
whisperx==<pinned>
```

- [ ] **Step 2: Note the alignment-model cache**

Add a Dockerfile comment (near the model-cache convention) that wav2vec2 alignment weights download on first use and should live under the `MODELS_ROOT` bind mount; set the relevant cache env if the chosen backend needs one.

- [ ] **Step 3: Verify the image still builds and imports cleanly**

Run: `docker compose build transcription` (or the repo's build path) and, once built, `docker compose run --rm transcription python3 -c "import ctranslate2; print(ctranslate2.get_cuda_device_count()); import aligner"`.
Expected: ctranslate2 CUDA count prints (0 on CPU-only build host is fine); `aligner` imports.

- [ ] **Step 4: Commit**

```bash
git add services/transcription/requirements.txt services/transcription/Dockerfile
git commit -m "build(transcription): install alignment backend"
```

### Task C7: Frontend — expose separation model + align toggle

**Files:**
- Modify: the settings panel component under `frontend/src/` that renders project config (the panel already renders `demucs_model`, `whisper_*` knobs — extend it).
- Test: the component test alongside it.

**Interfaces:**
- Consumes: `config.demucs_model`, `config.align_words` from `GET /projects/{id}`.
- Produces: PATCH bodies with `demucs_model` (now including `htdemucs_ft`, `bs_roformer`) and `align_words`.

- [ ] **Step 1: Write/extend the failing component test**

Assert the separation-model select offers `htdemucs`, `htdemucs_ft`, `mdx_extra`, `bs_roformer`, and that an "Align word timestamps" toggle renders and PATCHes `align_words`. (Match the existing settings-panel test's render/mock-fetch style.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && pnpm test -- <settings panel test>`
Expected: FAIL — new option/toggle absent.

- [ ] **Step 3: Implement**

Add `htdemucs_ft` and `bs_roformer` to the separation-model option list (with plain labels, e.g. "htdemucs_ft (fine-tuned, cleaner)", "BS-RoFormer (best vocals, more VRAM)"). Add a boolean toggle bound to `config.align_words` that PATCHes `{align_words: <bool>}`. Keep copy plain (no hedging).

- [ ] **Step 4: Run it to verify it passes**

Run: `cd frontend && pnpm test -- <settings panel test>`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/
git commit -m "feat(frontend): separation model options + align toggle in settings"
```

### Task C8: Spec the alignment pass

**Files:**
- Modify: `spec/pipeline.md` §Step 3, `spec/api-contracts.md` §Transcription Service, `spec/data-models.md` (project config)

- [ ] **Step 1: `spec/pipeline.md` §Step 3**

Add a subsection after §Sentence-aligned re-segmentation:
```
### Optional word alignment

When a project has `align_words` enabled, the service runs a wav2vec2 forced-alignment pass over each segment's whisper words before re-segmentation, replacing whisper's heuristic word start/end times with alignment-derived ones. It refines only timestamps — word text and per-word probability (and thus `transcript_confidence` and auto-approval) are unchanged. Alignment affects re-segmentation boundaries only; for a segment that is not re-segmented it is a no-op. Off by default. Changing it does not retro-apply — re-transcribe to adopt.
```

- [ ] **Step 2: `spec/api-contracts.md` §Transcription Service**

Document the `align: bool` field (default false) on the `POST /jobs` request body.

- [ ] **Step 3: `spec/data-models.md`**

Add `align_words INTEGER NOT NULL DEFAULT 0` to the projects-table config columns and note it in the project-config narrative.

- [ ] **Step 4: Commit**

```bash
git add spec/pipeline.md spec/api-contracts.md spec/data-models.md
git commit -m "docs: spec the optional alignment pass"
```

---

## Self-Review

**Spec coverage:**
- htdemucs_ft default → A1 (service), A2 (orchestrator + spec). ✔
- RoFormer "libs in same container / api option" → B0 (dep gate), B1 (branch + api via existing `model`), B2 (lifecycle), B3 (config), B4 (spec). ✔
- Alignment optional pass → C0 (dep gate), C1 (config), C2 (merge logic), C3 (transcriber), C4 (service API), C5 (orchestrator payload), C6 (image), C7 (frontend), C8 (spec). ✔
- Dep-solve as first checkpoint → B0 and C0 are explicit go/no-go gates with fallback (second image / torchaudio). ✔

**Placeholder scan:** The only intentional `<pinned>` / `<settings panel test>` tokens are outputs of the B0/C0 spikes and the existing frontend test name — resolved during execution, not plan-authoring gaps. All logic steps carry complete code.

**Type consistency:** `separator.VALID_MODELS`/`DEMUCS_MODELS` kept as supersets; `_is_roformer`/`_ROFORMER_CKPT`/`_separate_roformer` consistent across B1/B2. `aligner.Word` + `merge_alignment(whisper_words, aligned)` + `align_words(wav_path, whisper_words, language)` consistent across C2/C3; `align`/`aligner_fn` threaded uniformly through `transcribe_segment`→`_transcribe_one`→`process_batch`→`main._run_transcription`. `align_words` DB column name consistent across C1/C5/C8.

**Ordering note:** Slice A is independent and can ship first. Slices B and C are independent of each other; within each, the C0/B0 gate blocks the rest.
