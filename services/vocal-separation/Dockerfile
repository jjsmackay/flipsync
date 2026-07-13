FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# RoFormer (bs_roformer) weights download on first use to ROFORMER_MODEL_DIR
# (defaults under /mnt/models/flipsync/audio-separator). NOTE: docker-compose.yml
# does not currently bind-mount this dir or set ROFORMER_MODEL_DIR, so weights
# re-download on container recreate. To persist across `compose down`, wire a
# bind mount under MODELS_ROOT (e.g. ${MODELS_ROOT}/audio-separator) and set
# ROFORMER_MODEL_DIR accordingly in docker-compose.yml (owed deploy step).
COPY requirements.txt .
# demucs 4.0.1 pulls in diffq, a Cython extension with no cp311 wheel, so pip
# compiles it from source and needs a C toolchain. Install build-essential and
# purge it in the same layer so it doesn't bloat the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8001
CMD ["python3", "main.py"]
