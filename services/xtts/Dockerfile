FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install the heavy accelerator stack (torch + CUDA wheels, ~4 GB) in its own
# layer FIRST. It changes rarely, so pin bumps to the lighter deps below — and
# code-only changes — reuse this cached layer instead of re-downloading torch on
# every build. This layer is what made builds ~8 min; keeping it separate (plus
# the registry build cache, see .github/workflows/build-push.yml) drops
# incremental builds to a couple of minutes.
COPY requirements-accel.txt .
RUN pip install --no-cache-dir -r requirements-accel.txt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8005
CMD ["python3", "main.py"]
