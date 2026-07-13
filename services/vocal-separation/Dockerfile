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
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001
CMD ["python3", "main.py"]
