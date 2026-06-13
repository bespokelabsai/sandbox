# LLM application stack (openai, anthropic, langchain, tiktoken, tenacity).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    openai \
    anthropic \
    langchain \
    langchain-core \
    langchain-community \
    tiktoken \
    tenacity
