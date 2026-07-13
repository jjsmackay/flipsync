# CUDA 12 + cuDNN 9 come from the base image. ctranslate2 4.8.x links against
# these system libraries (its wheel bundles no cuDNN), so the loader resolves
# them via ldconfig — no pip CUDA wheels, no LD_LIBRARY_PATH, no execstack patch.
# NOTE: 12.9 needs a recent NVIDIA driver (~575+) on the host; drop to an older
# 12.x-cudnn-runtime tag if the GPU host's driver is older.
FROM nvidia/cuda:12.9.2-cudnn-runtime-ubuntu24.04

# The CUDA base image has no Python. Ubuntu 24.04 ships CPython 3.12, which the
# faster-whisper / ctranslate2 wheels support.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Alignment weights (torchaudio MMS_FA, for the optional align_words pass)
# download on first use via torch.hub, cached under TORCH_HOME (default
# /root/.cache/torch). Bind-mount that path under the MODELS_ROOT model-cache
# convention (see docker-compose.yml / spec/deployment.md) so weights survive
# `compose down` — not yet wired into docker-compose.yml; owed alongside the
# GPU-host CUDA-coexistence check below.
COPY requirements.txt .
# Ubuntu 24.04's system Python is PEP 668 "externally managed"; this is a
# single-purpose container, so install straight into it.
# --extra-index-url pulls the CUDA 12.6 (cu126) torch/torchaudio wheels
# (see requirements.txt for the coexistence caveat with ctranslate2's
# system-CUDA linkage); everything else resolves from PyPI as before.
RUN pip3 install --no-cache-dir --break-system-packages \
        --extra-index-url https://download.pytorch.org/whl/cu126 \
        -r requirements.txt

COPY . .

EXPOSE 8003
CMD ["python3", "main.py"]
