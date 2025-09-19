# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Prevents Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000 \
    FLASK_DEBUG=0

WORKDIR /app

# Install system deps if needed (kept minimal)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py ./
COPY plexsync.py ./
COPY templates ./templates
COPY static ./static

# Create uploads directory
RUN mkdir -p /app/uploads

EXPOSE 5000

# Default command to run the Flask app
CMD ["python", "app.py"]
