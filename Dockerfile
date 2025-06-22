# Use slim Python base image (Debian 12 "bookworm")
FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# Install system dependencies (FFmpeg and fonts), clean up afterwards
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy only requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install -r requirements.txt

# Copy application code
COPY ffmpeg_handler.py .

COPY fonts/Anton-Regular.ttf /usr/share/fonts/truetype/

# Use exec form to avoid shell form issues (signals, etc.)
CMD ["python", "-u", "ffmpeg_handler.py"]
