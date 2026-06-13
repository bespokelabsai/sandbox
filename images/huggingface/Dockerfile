# Hugging Face stack (transformers, datasets, tokenizers, accelerate, hub, safetensors, sentencepiece).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV HF_HUB_DISABLE_TELEMETRY=1

RUN pip install --no-cache-dir \
    transformers \
    datasets \
    tokenizers \
    accelerate \
    huggingface-hub \
    safetensors \
    sentencepiece
