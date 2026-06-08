# Hugging Face stack (transformers, datasets, tokenizers, accelerate, hub, safetensors, sentencepiece).
FROM python:3.11-slim

ENV HF_HUB_DISABLE_TELEMETRY=1

RUN pip install --no-cache-dir \
    transformers \
    datasets \
    tokenizers \
    accelerate \
    huggingface-hub \
    safetensors \
    sentencepiece
