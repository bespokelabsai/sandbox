# Image processing stack (pillow, opencv-python-headless, scikit-image, numpy).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && pip install --no-cache-dir pillow opencv-python-headless scikit-image numpy \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
