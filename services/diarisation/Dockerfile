FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF_TOKEN must be set at runtime for pyannote model download.
# Not set here to avoid baking credentials into the image.
ENV HF_TOKEN=""

EXPOSE 8002
CMD ["python", "main.py"]
