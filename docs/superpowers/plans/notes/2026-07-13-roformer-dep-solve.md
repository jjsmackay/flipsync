# RoFormer dependency-solve spike (Task B0)

**Date:** 2026-07-13
**Verdict: GO (same container).**

## What was tested

Joint resolution (no GPU on the spike host; resolution + version compatibility is
the "same container?" gate — RoFormer inference itself is owed on the deploy GPU host):

```
torch<2.6
torchaudio<2.6
demucs==4.0.1
audio-separator
onnxruntime-gpu
```

`uv pip compile --python-version 3.11` → **EXIT 0**, one consistent set.

## Resolved crux versions

| package | version | note |
|---|---|---|
| torch | **2.5.1** | satisfies `torch<2.6` — Demucs 4.0.1 `torch.load` unpickling safe |
| torchaudio | 2.5.1 | |
| audio-separator | 0.44.3 | |
| onnxruntime-gpu | 1.27.0 | |
| numpy | 2.4.6 | |
| total | 81 pkgs | moderate bloat over the current lean Demucs image |

## Decision → proceed to B1

A single `torch` (2.5.1) satisfies Demucs 4.0.1 **and** audio-separator; Demucs
checkpoints load under it. No second image needed.

### Caveat to handle in B1
The resolver pulled `onnx-weekly==1.23.0.dev*` and `onnx2torch-py313` transitively
via audio-separator. **Pin `onnx` to a stable release explicitly** in
`requirements.txt` so the image does not float onto weekly/dev builds.

### Still owed (deploy GPU host)
- RoFormer actually separates the fixture and emits a `(Vocals)` stem.
- Demucs checkpoint load confirmed under torch 2.5.1 on GPU.
- VRAM headroom check running RoFormer under the host-wide GPU semaphore.

### Pins to add in B1 (requirements.txt)
```
audio-separator==0.44.3
onnxruntime-gpu==1.27.0
onnx==<stable, e.g. 1.17.x>   # avoid the onnx-weekly the resolver picked
# torch/torchaudio already effectively pinned <2.6; 2.5.1 is the resolved point
```
