# Dockerfile for CPU FFmpeg Worker
FROM python:3.10-slim-buster

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install FFmpeg and a good set of fonts for captioning
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /rp_handler

COPY requirements.txt .
COPY ffmpeg_handler.py .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-u", "ffmpeg_handler.py"]