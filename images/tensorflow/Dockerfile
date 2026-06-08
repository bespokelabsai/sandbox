# TensorFlow (CPU build).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && pip install --no-cache-dir tensorflow-cpu \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
