FROM python:3.11-slim

WORKDIR /app

# build-essential: jieba_fast (imported by the vendored zh text pipeline even
# for English-only use) ships no py3.11 wheels and compiles a small C extension.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install the heavy accelerator stack (torch + CUDA wheels, ~4 GB) in its own
# layer FIRST — it changes rarely, so pin bumps to the lighter deps below (and
# code-only changes) reuse this cached layer instead of re-downloading torch on
# every build. Same layering as the xtts image; the registry build cache in
# .github/workflows/build-push.yml keeps it warm across CI runs.
COPY requirements-accel.txt .
RUN pip install --no-cache-dir -r requirements-accel.txt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# NLTK data for the English g2p path (text/english.py pos_tag + g2p_en),
# baked into the image so the first job doesn't download it into the
# ephemeral container filesystem.
RUN python3 -m nltk.downloader averaged_perceptron_tagger averaged_perceptron_tagger_eng cmudict

# Vendored GPT-SoVITS checkout at the exact pinned commit (v2Pro line, MIT).
# engine.VENDORED_COMMIT must match this pin — it is recorded in every trained
# bundle's config.json.
ARG GPT_SOVITS_COMMIT=be6a4f1e9d8a22d41b7d42c22df9d7ef36f225d2
RUN git init -q /opt/GPT-SoVITS \
    && git -C /opt/GPT-SoVITS remote add origin https://github.com/RVC-Boss/GPT-SoVITS.git \
    && git -C /opt/GPT-SoVITS fetch -q --depth 1 origin ${GPT_SOVITS_COMMIT} \
    && git -C /opt/GPT-SoVITS checkout -q FETCH_HEAD \
    && rm -rf /opt/GPT-SoVITS/.git

# Point the repo's pretrained_models dir at the bind-mounted weight cache: the
# speaker-verification model path is hardcoded upstream relative to the repo
# root, so it must resolve through here. engine re-points the link at runtime
# if GPT_SOVITS_PRETRAINED_DIR is overridden.
RUN rm -rf /opt/GPT-SoVITS/GPT_SoVITS/pretrained_models \
    && ln -s /models/gpt-sovits /opt/GPT-SoVITS/GPT_SoVITS/pretrained_models

ENV GPT_SOVITS_REPO_DIR=/opt/GPT-SoVITS \
    GPT_SOVITS_PRETRAINED_DIR=/models/gpt-sovits

COPY . .

EXPOSE 8006
CMD ["python3", "main.py"]
